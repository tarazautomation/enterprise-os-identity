"""Where the Keycloak admin client sends its calls: the issuer by default, or KEYCLOAK_INTERNAL_URL and
TARAZ_AUTH_TOKEN_URL when Keycloak's public address doesn't work from inside the service (no network)."""

import httpx
import pytest

from tenant_service.config import Settings
from tenant_service.keycloak_admin import KeycloakAdmin

INTERNAL = "http://keycloak-service.identity.svc.cluster.local:8080"


@pytest.fixture
def calls(monkeypatch):
    """Every request the admin client makes, answered by a fake Keycloak."""
    seen = []

    def keycloak(request):
        seen.append(f"{request.method} {str(request.url).split('?')[0]}")
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        return httpx.Response(200, json=[])

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(keycloak)}))
    return seen


def settings_from(monkeypatch, issuer, **extra):
    env = {"TARAZ_AUTH_ISSUER": issuer, "TARAZ_AUTH_TENANT_ID": "taraz", "TARAZ_AUTH_CLIENT_ID": "tenant-service",
           "TARAZ_AUTH_CLIENT_SECRET": "x", "DATABASE_URL": "sqlite://", **extra}
    for k in ("KEYCLOAK_INTERNAL_URL", "TARAZ_AUTH_TOKEN_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_calls_go_to_the_issuer_by_default(monkeypatch, calls):
    s = settings_from(monkeypatch, "https://taraz-auth.up.railway.app/realms/taraz/")
    KeycloakAdmin.from_settings(s).list_users()
    assert calls == ["POST https://taraz-auth.up.railway.app/realms/taraz/protocol/openid-connect/token",
                     "GET https://taraz-auth.up.railway.app/admin/realms/taraz/users"]


def test_keycloak_internal_url_for_admin_and_token_calls(monkeypatch, calls):
    s = settings_from(monkeypatch, "http://localhost:8080/realms/taraz", KEYCLOAK_INTERNAL_URL=INTERNAL + "/")
    KeycloakAdmin.from_settings(s).list_users()
    assert calls == [f"POST {INTERNAL}/realms/taraz/protocol/openid-connect/token",
                     f"GET {INTERNAL}/admin/realms/taraz/users"]
    assert s.issuer == "http://localhost:8080/realms/taraz"     # tokens are still checked against the public issuer


def test_token_url_wins_for_the_service_token(monkeypatch, calls):
    token_url = "http://keycloak-token.internal:8080/realms/taraz/protocol/openid-connect/token"
    s = settings_from(monkeypatch, "http://localhost:8080/realms/taraz", KEYCLOAK_INTERNAL_URL=INTERNAL,
                      TARAZ_AUTH_TOKEN_URL=token_url)
    KeycloakAdmin.from_settings(s).list_users()
    assert calls == [f"POST {token_url}", f"GET {INTERNAL}/admin/realms/taraz/users"]
