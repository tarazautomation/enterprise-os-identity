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
    jwks_url: str | None = None     # optional internal address for Keycloak's public keys

    @property
    def keycloak_base(self) -> str:
        return self.issuer.rstrip("/").rsplit("/realms/", 1)[0]

    @property
    def realm(self) -> str:
        return self.issuer.rstrip("/").rsplit("/realms/", 1)[1]

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
            jwks_url=env.get("TARAZ_AUTH_JWKS_URL") or None,
        )
