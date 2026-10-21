"""Builds the FastAPI app: database, Keycloak client, token checks, background sync."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.concurrency import run_in_threadpool
from taraz_auth import TokenVerifier
from taraz_auth.fastapi import FastAPIAuth

from . import __version__
from .api import build_router
from .config import Settings
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
               run_sync_loop: bool = True) -> FastAPI:
    engine = engine or make_engine(settings.database_url)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    holder = {"kc": kc}
    state: dict = {"at": None, "ok": None, "error": None}

    def get_kc():
        if holder["kc"] is None:
            holder["kc"] = KeycloakAdmin.from_settings(settings)
        return holder["kc"]

    def get_session():
        with SessionLocal() as s:
            yield s

    def run_sync():
        with SessionLocal() as s:
            res = sync_all(s, get_kc(), settings.tenant_id)
        state.update(at=res.at, ok=True, error=None)
        return res

    async def sync_loop():
        while True:
            try:
                res = await run_in_threadpool(run_sync)
                if res.created or res.removed:
                    log.info("sync: %d users, %d new, %d updated, %d removed", res.total, res.created, res.updated, res.removed)
            except Exception as e:                     # keep running; report through /v1/status
                state.update(ok=False, error=str(e)[:300])
                log.warning("sync failed: %s", e)
            await asyncio.sleep(settings.sync_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with SessionLocal() as s:
            seed(s, settings.tenant_id, settings.tenant_name)
        task = asyncio.create_task(sync_loop()) if run_sync_loop else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Enterprise OS tenant service", version=__version__, lifespan=lifespan,
                  description="The member directory and admin-portal back end for one tenant (P2.2). "
                              "Keycloak holds accounts and roles; this service mirrors them and adds app access, "
                              "agents, invites and change history.")
    auth = FastAPIAuth(verifier or TokenVerifier(settings.issuer, settings.tenant_id, jwks_url=settings.jwks_url))
    app.include_router(build_router(auth, get_session, get_kc, settings.tenant_id, state))

    @app.exception_handler(KeycloakError)
    async def keycloak_error(_, exc: KeycloakError):
        log.warning("%s", exc)
        return JSONResponse(status_code=502, content={"detail": {"error": "keycloak_error", "message": str(exc)[:300]}})

    @app.get("/healthz", tags=["ops"], include_in_schema=False)
    def healthz():
        with engine.connect() as c:
            c.execute(text("select 1"))
        return {"ok": True, "version": __version__}

    return app
