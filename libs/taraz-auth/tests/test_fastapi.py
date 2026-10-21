import time

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from taraz_auth.fastapi import FastAPIAuth


@pytest.fixture
def client(verifier):
    auth = FastAPIAuth(verifier)
    app = FastAPI()

    @app.get("/me")
    def me(user=Depends(auth.principal)):
        return {"user": user.username, "tenant": user.tenant_id, "roles": sorted(user.roles)}

    @app.get("/admin")
    def admin(user=Depends(auth.require("admin"))):
        return {"ok": True}

    @app.get("/approve")
    def approve(request: Request, user=Depends(auth.require_any("admin", "process_owner"))):
        return {"seen_by_audit": request.state.principal.subject}

    return TestClient(app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_valid_token(client, make_token):
    r = client.get("/me", headers=bearer(make_token()))
    assert r.status_code == 200 and r.json()["tenant"] == "taraz"


def test_no_token_is_401(client):
    r = client.get("/me")
    assert r.status_code == 401 and r.json()["detail"]["error"] == "missing_token"
    assert r.headers["www-authenticate"] == 'Bearer error="missing_token"'


def test_expired_token_is_401(client, make_token):
    now = int(time.time())
    r = client.get("/me", headers=bearer(make_token(iat=now - 900, exp=now - 600)))
    assert r.status_code == 401 and r.json()["detail"]["error"] == "token_expired"
    assert 'error="token_expired"' in r.headers["www-authenticate"]


def test_wrong_tenant_is_401(client, make_token):
    r = client.get("/me", headers=bearer(make_token(tenant_id="other-company")))
    assert r.status_code == 401 and r.json()["detail"]["error"] == "wrong_tenant"


def test_missing_role_is_403(client, make_token):
    r = client.get("/admin", headers=bearer(make_token()))
    assert r.status_code == 403 and r.json()["detail"]["error"] == "insufficient_role"
    assert "www-authenticate" not in r.headers


def test_role_present_is_200(client, make_token):
    assert client.get("/admin", headers=bearer(make_token(realm_access={"roles": ["admin"]}))).status_code == 200


def test_require_any_and_principal_on_request_state(client, make_token):
    r = client.get("/approve", headers=bearer(make_token(realm_access={"roles": ["process_owner"]})))
    assert r.status_code == 200 and r.json()["seen_by_audit"] == "1119fedd-661c-42ec-869a-a2f9d2240f18"


def test_wrong_scheme_is_401(client, make_token):
    assert client.get("/me", headers={"Authorization": f"Basic {make_token()}"}).status_code == 401
