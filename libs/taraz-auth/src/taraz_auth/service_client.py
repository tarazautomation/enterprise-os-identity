"""Call other services as this service (Keycloak client-credentials / service account).

    tokens = ServiceTokenProvider.from_env()          # TARAZ_AUTH_ISSUER, _CLIENT_ID, _CLIENT_SECRET
    with httpx.Client(auth=tokens.httpx_auth()) as http:
        http.get("http://knowledge-base/api/...")       # Authorization: Bearer <service token>

The token is cached and renewed shortly before it expires.
"""

import os
import threading
import time
from typing import Generator

import httpx


class ServiceTokenError(RuntimeError):
    pass


class ServiceTokenProvider:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        refresh_margin_seconds: int = 30,
        http_timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self._secret = client_secret
        self._margin = refresh_margin_seconds
        self._http = httpx.Client(timeout=http_timeout_seconds, transport=transport)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_issuer(cls, issuer: str, client_id: str, client_secret: str, **kw) -> "ServiceTokenProvider":
        return cls(f"{issuer.rstrip('/')}/protocol/openid-connect/token", client_id, client_secret, **kw)

    @classmethod
    def from_env(cls, prefix: str = "TARAZ_AUTH_") -> "ServiceTokenProvider":
        """Build from TARAZ_AUTH_CLIENT_ID, TARAZ_AUTH_CLIENT_SECRET and TARAZ_AUTH_TOKEN_URL
        (or TARAZ_AUTH_ISSUER, from which the token URL is derived)."""
        env = os.environ
        try:
            client_id, secret = env[prefix + "CLIENT_ID"], env[prefix + "CLIENT_SECRET"]
        except KeyError as missing:
            raise RuntimeError(f"environment variable {missing.args[0]} is not set") from None
        token_url = env.get(prefix + "TOKEN_URL")
        if token_url:
            return cls(token_url, client_id, secret)
        if prefix + "ISSUER" not in env:
            raise RuntimeError(f"set {prefix}TOKEN_URL or {prefix}ISSUER")
        return cls.from_issuer(env[prefix + "ISSUER"], client_id, secret)

    def get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self._token or time.time() >= self._expires_at - self._margin:
                self._fetch()
            return self._token  # type: ignore[return-value]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def httpx_auth(self) -> httpx.Auth:
        return _ServiceAuth(self)

    def _fetch(self) -> None:
        try:
            r = self._http.post(
                self.token_url,
                data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret},
            )
        except httpx.HTTPError as e:
            raise ServiceTokenError(f"couldn't reach Keycloak at {self.token_url}: {e}") from None
        if r.status_code != 200:
            raise ServiceTokenError(f"Keycloak refused the service token for {self.client_id!r}: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 60))


class _ServiceAuth(httpx.Auth):
    """Adds the service token to each request; on a 401, renews it once and retries."""

    def __init__(self, provider: ServiceTokenProvider):
        self._provider = provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._provider.get_token(force_refresh=True)}"
            yield request
