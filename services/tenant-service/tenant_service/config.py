"""Settings, all from environment variables."""

import os
from dataclasses import dataclass


def normalize_db_url(url: str) -> str:
    """Railway hands out postgresql:// URLs; SQLAlchemy needs the psycopg driver named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    issuer: str                     # e.g. https://taraz-auth.up.railway.app/realms/taraz
    tenant_id: str                  # e.g. taraz
    tenant_name: str                # e.g. Taraz Technologies
    client_id: str                  # this service's Keycloak client (tenant-service)
    client_secret: str
    database_url: str
    sync_interval_seconds: int = 30
    sync_max_remove: int = 5        # more members missing from Keycloak than this in one sync: change nothing, report it
    docs_enabled: bool = False      # /docs, /redoc and /openapi.json; off in production (the API is in openapi.json)
    jwks_url: str | None = None     # optional internal address for Keycloak's public keys
    keycloak_internal_url: str | None = None   # optional internal base URL for admin and token calls
    token_url: str | None = None    # optional internal address of the token endpoint
    admin_clients: frozenset[str] = frozenset({"app-shell"})   # apps an admin may change members from
    check_audience: bool = True     # tokens must be addressed to this service (its client id)
    entra_tenant_id: str | None = None        # the leavers' sync (entra.py): the customer's Microsoft tenant
    entra_client_id: str | None = None        # its sync app, with Microsoft Graph User.Read.All
    entra_client_secret: str | None = None
    entra_sync_interval_seconds: int = 900
    entra_max_disable: int = 5                # more leavers than this in one run: change nothing, report it

    @property
    def entra_sync_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_client_id and self.entra_client_secret)

    @property
    def keycloak_base(self) -> str:
        """Where admin API calls go. The issuer stays the public value that tokens are checked against."""
        if self.keycloak_internal_url:
            return self.keycloak_internal_url.rstrip("/")
        return self.issuer.rstrip("/").rsplit("/realms/", 1)[0]

    @property
    def realm(self) -> str:
        return self.issuer.rstrip("/").rsplit("/realms/", 1)[1]

    @property
    def token_endpoint(self) -> str:
        """Where this service gets its own token (client credentials)."""
        return self.token_url or f"{self.keycloak_base}/realms/{self.realm}/protocol/openid-connect/token"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        missing = [k for k in ("TARAZ_AUTH_ISSUER", "TARAZ_AUTH_TENANT_ID", "TARAZ_AUTH_CLIENT_ID",
                               "TARAZ_AUTH_CLIENT_SECRET", "DATABASE_URL") if not env.get(k)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            issuer=env["TARAZ_AUTH_ISSUER"].rstrip("/"),
            tenant_id=env["TARAZ_AUTH_TENANT_ID"],
            tenant_name=env.get("TENANT_NAME", env["TARAZ_AUTH_TENANT_ID"]),
            client_id=env["TARAZ_AUTH_CLIENT_ID"],
            client_secret=env["TARAZ_AUTH_CLIENT_SECRET"],
            database_url=normalize_db_url(env["DATABASE_URL"]),
            sync_interval_seconds=int(env.get("SYNC_INTERVAL_SECONDS", "30")),
            sync_max_remove=int(env.get("SYNC_MAX_REMOVE", "5")),
            docs_enabled=env.get("DOCS_ENABLED", "").lower() in ("1", "true", "yes"),
            jwks_url=env.get("TARAZ_AUTH_JWKS_URL") or None,
            keycloak_internal_url=env.get("KEYCLOAK_INTERNAL_URL") or None,
            token_url=env.get("TARAZ_AUTH_TOKEN_URL") or None,
            admin_clients=frozenset(c.strip() for c in env.get("ADMIN_CLIENTS", "app-shell").split(",") if c.strip()),
            check_audience=env.get("TARAZ_AUTH_SKIP_AUDIENCE_CHECK", "").lower() not in ("1", "true", "yes"),
            entra_tenant_id=env.get("ENTRA_TENANT_ID") or None,
            entra_client_id=env.get("ENTRA_SYNC_CLIENT_ID") or None,
            entra_client_secret=env.get("ENTRA_SYNC_CLIENT_SECRET") or None,
            entra_sync_interval_seconds=int(env.get("ENTRA_SYNC_INTERVAL_SECONDS", "900")),
            entra_max_disable=int(env.get("ENTRA_SYNC_MAX_DISABLE", "5")),
        )
