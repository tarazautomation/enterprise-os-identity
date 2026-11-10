"""Builds the FastAPI app: database, Keycloak client, token checks, background syncs (Keycloak, and leavers from
Microsoft when configured).

Several replicas can run: each sync run takes a lock and is skipped where another replica holds it (history.py).
Errors from Keycloak and Microsoft are logged in full and answered with a fixed message, so callers never see
another system's response text.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.concurrency import run_in_threadpool
from taraz_auth import ServiceTokenError, TokenVerifier
from taraz_auth.fastapi import FastAPIAuth

from . import __version__
from .api import build_router
from .config import Settings
from .entra import GraphUsers, disable_leavers
from .history import Locks
from .keycloak_admin import KeycloakAdmin, KeycloakError
from .sync import seed, sync_all

log = logging.getLogger("tenant_service")


def make_engine(url: str):
    if url.startswith("sqlite"):
        kw = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:   # one shared in-memory database
            kw["poolclass"] = StaticPool
        return create_engine(url, **kw)
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def create_app(settings: Settings, *, kc=None, verifier: TokenVerifier | None = None, engine=None,
               run_sync_loop: bool = True, graph: GraphUsers | None = None) -> FastAPI:
    engine = engine or make_engine(settings.database_url)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    locks = Locks(engine)
    holder = {"kc": kc}
    state: dict = {"at": None, "ok": None, "error": None,
                   "entra_enabled": settings.entra_sync_enabled, "entra_at": None, "entra_ok": None, "entra_error": None}
    if graph is None and settings.entra_sync_enabled:
        graph = GraphUsers(settings.entra_tenant_id, settings.entra_client_id, settings.entra_client_secret)

    def get_kc():
        if holder["kc"] is None:
            holder["kc"] = KeycloakAdmin.from_settings(settings)
        return holder["kc"]

    def get_session():
        with SessionLocal() as s:
            yield s

    def run_sync():
        with locks.try_hold("sync") as mine:
            if not mine:                               # another replica is syncing
                return None
            with SessionLocal() as s:
                res = sync_all(s, get_kc(), settings.tenant_id, settings.sync_max_remove)
        state.update(at=res.at, ok=True, error=None)
        return res

    async def sync_loop():
        while True:
            try:
                res = await run_in_threadpool(run_sync)
                if res and (res.created or res.removed):
                    log.info("sync: %d users, %d new, %d updated, %d removed", res.total, res.created, res.updated, res.removed)
            except Exception as e:                     # keep running; report through /v1/status
                state.update(ok=False, error=str(e)[:300])
                log.warning("sync failed: %s", e)
            await asyncio.sleep(settings.sync_interval_seconds)

    def run_entra():
        with locks.try_hold("entra") as mine:
            if not mine:
                return None
            with SessionLocal() as s:
                assigned = graph.assigned(settings.entra_signin_app_id) if settings.entra_signin_app_id else None
                guests = None if settings.entra_allow_guests else graph.guests()
                res = disable_leavers(s, get_kc(), settings.tenant_id, graph.accounts(), settings.entra_max_disable,
                                      assigned, guests)
        state.update(entra_at=res.at, entra_ok=True, entra_error=None)
        return res

    async def entra_loop():
        while True:
            try:
                res = await run_in_threadpool(run_entra)
                if res and res.disabled:
                    log.info("leavers: %d disabled because Microsoft disabled or deleted them", len(res.disabled))
            except Exception as e:                     # keep running; report through /v1/status
                state.update(entra_ok=False, entra_error=str(e)[:300])
                log.warning("leavers' sync failed: %s", e)
            await asyncio.sleep(settings.entra_sync_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with SessionLocal() as s:
            seed(s, settings.tenant_id, settings.tenant_name)
        tasks = []
        if run_sync_loop:
            tasks.append(asyncio.create_task(sync_loop()))
            if graph is not None:
                tasks.append(asyncio.create_task(entra_loop()))
        yield
        for task in tasks:
            task.cancel()

    docs = {} if settings.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Enterprise OS tenant service", version=__version__, lifespan=lifespan, **docs,
                  description="The member directory and admin-portal back end for one tenant (P2.2). "
                              "Keycloak holds accounts and roles; this service mirrors them and adds app access, "
                              "agents, invites and change history.")
    # Tokens must be addressed to this service: its client id is its audience
    audience = settings.client_id if settings.check_audience else None
    auth = FastAPIAuth(verifier or TokenVerifier(settings.issuer, settings.tenant_id, audience=audience,
                                                 jwks_url=settings.jwks_url))
    app.include_router(build_router(auth, get_session, get_kc, settings.tenant_id, state, settings.admin_clients,
                                    locks, settings.sync_max_remove))
    app.state.session_factory = SessionLocal
    app.state.run_entra = run_entra           # one leavers' sync run, for tests and operators

    async def keycloak_error(_, exc: Exception):
        # The detail (Keycloak's answer, an address) goes to the log, not to the caller
        log.warning("Keycloak call failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"detail": {
            "error": "keycloak_error", "message": "Keycloak didn't answer as expected; try again shortly"}})

    for error in (KeycloakError, ServiceTokenError, httpx.HTTPError):
        app.add_exception_handler(error, keycloak_error)

    @app.get("/healthz", tags=["ops"], include_in_schema=False)
    def healthz():
        with engine.connect() as c:
            c.execute(text("select 1"))
        return {"ok": True, "version": __version__}

    return app
