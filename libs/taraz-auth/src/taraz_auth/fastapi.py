"""FastAPI integration (install with the "fastapi" extra).

    from taraz_auth import TokenVerifier
    from taraz_auth.fastapi import FastAPIAuth

    auth = FastAPIAuth(TokenVerifier.from_env())

    @app.get("/me")
    def me(user = Depends(auth.principal)): ...

    @app.post("/ontology/approve")
    def approve(user = Depends(auth.require_any("admin", "process_owner"))): ...

Errors become HTTP answers: 401 for no, invalid, expired or wrong-tenant tokens (with a
WWW-Authenticate header), 403 for a missing role, 503 if Keycloak's keys can't be fetched.
The caller is also put on request.state.principal, e.g. for the audit log.
"""

from typing import Callable

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .errors import AuthError, MissingToken
from .principal import Principal
from .verifier import TokenVerifier


def _http_error(err: AuthError) -> HTTPException:
    headers = {"WWW-Authenticate": f'Bearer error="{err.code}"'} if err.status == 401 else None
    return HTTPException(status_code=err.status, detail={"error": err.code, "message": err.message}, headers=headers)


class FastAPIAuth:
    def __init__(self, verifier: TokenVerifier):
        self.verifier = verifier

    async def principal(self, request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        try:
            if scheme.lower() != "bearer" or not token.strip():
                raise MissingToken("send Authorization: Bearer <token>")
            # verify() may fetch Keycloak's keys over the network the first time, so keep it off the event loop
            p = await run_in_threadpool(self.verifier.verify, token.strip())
        except AuthError as e:
            raise _http_error(e) from None
        request.state.principal = p
        return p

    def require(self, *roles: str) -> Callable:
        """Dependency: the caller must have every one of these roles."""
        async def dependency(request: Request) -> Principal:
            p = await self.principal(request)
            try:
                return p.require_roles(*roles)
            except AuthError as e:
                raise _http_error(e) from None
        return dependency

    def require_any(self, *roles: str) -> Callable:
        """Dependency: the caller must have at least one of these roles."""
        async def dependency(request: Request) -> Principal:
            p = await self.principal(request)
            try:
                return p.require_any_role(*roles)
            except AuthError as e:
                raise _http_error(e) from None
        return dependency
