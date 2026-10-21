import time

import jwt
import pytest
from jwt.exceptions import PyJWKClientConnectionError

from taraz_auth import (
    ExpiredToken,
    InvalidToken,
    KeysUnavailable,
    MissingRole,
    TokenVerifier,
    WrongTenant,
)

from .conftest import ISSUER


def test_valid_token_gives_the_caller(verifier, make_token):
    p = verifier.verify(make_token())
    assert p.tenant_id == "taraz"
    assert p.username == "junaid.asrar@taraztechnologies.com"
    assert p.client_id == "app-shell"
    assert p.has_role("employee") and not p.has_role("admin")
    assert not p.is_service_account and not p.is_agent


# --- the three cases P2.3 names: expired, wrong tenant, missing role ---

def test_expired_token_is_refused(verifier, make_token):
    now = int(time.time())
    with pytest.raises(ExpiredToken) as e:
        verifier.verify(make_token(iat=now - 900, exp=now - 600))
    assert e.value.status == 401 and e.value.code == "token_expired"


def test_slightly_expired_token_is_allowed_within_leeway(verifier, make_token):
    now = int(time.time())
    assert verifier.verify(make_token(iat=now - 310, exp=now - 10)).tenant_id == "taraz"


def test_wrong_tenant_is_refused(verifier, make_token):
    with pytest.raises(WrongTenant) as e:
        verifier.verify(make_token(tenant_id="other-company"))
    assert e.value.status == 401 and e.value.code == "wrong_tenant"


def test_missing_tenant_is_refused(verifier, make_token):
    with pytest.raises(WrongTenant):
        verifier.verify(make_token(drop=("tenant_id",)))


def test_missing_role_is_refused(verifier, make_token):
    p = verifier.verify(make_token())
    with pytest.raises(MissingRole) as e:
        p.require_roles("admin")
    assert e.value.status == 403 and e.value.code == "insufficient_role"


def test_role_checks_pass_with_the_role(verifier, make_token):
    p = verifier.verify(make_token(realm_access={"roles": ["employee", "admin"]}))
    assert p.require_roles("admin", "employee") is p
    assert p.require_any_role("process_owner", "admin") is p
    with pytest.raises(MissingRole):
        p.require_any_role("process_owner", "agent")


# --- forged or misused tokens ---

def test_wrong_issuer_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(iss="https://evil.example/realms/taraz"))


def test_forged_signature_is_refused(verifier, make_token, other_pem):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(signing_pem=other_pem))


def test_unknown_signing_key_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(kid="not-a-known-key"))


def test_unsigned_token_is_refused(verifier):
    token = jwt.encode({"iss": ISSUER, "sub": "x", "tenant_id": "taraz"}, key=None, algorithm="none")
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_hmac_token_is_refused(verifier, make_token):
    # an attacker signing with HS256 and the public key as the "secret" must not get in
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(signing_pem="a-shared-secret-that-is-at-least-32-bytes-long", algorithm="HS256"))


def test_id_token_is_refused_as_access_token(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(typ="ID"))


@pytest.mark.parametrize("junk", ["", "abc", "a.b", "a.b.c", "Bearer x.y.z"])
def test_garbage_is_refused(verifier, junk):
    with pytest.raises(InvalidToken):
        verifier.verify(junk)


def test_missing_expiry_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(drop=("exp",)))


# --- audience, service accounts, agents, key fetch failures ---

def test_audience_is_checked_only_when_configured(verifier, make_token):
    verifier.audience = "knowledge-base"
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(aud="account"))
    assert verifier.verify(make_token(aud=["account", "knowledge-base"])).tenant_id == "taraz"


def test_service_account_is_recognised(verifier, make_token):
    p = verifier.verify(make_token(preferred_username="service-account-scribe", azp="scribe", drop=("email", "name")))
    assert p.is_service_account and p.client_id == "scribe"


def test_agent_is_recognised(verifier, make_token):
    p = verifier.verify(make_token(azp="external-agents", realm_access={"roles": ["employee", "agent"]}))
    assert p.is_agent


def test_keys_unavailable_gives_503(make_token):
    v = TokenVerifier(ISSUER, "taraz")

    def down():
        raise PyJWKClientConnectionError("connection refused")

    v._keys.fetch_data = down
    with pytest.raises(KeysUnavailable) as e:
        v.verify(make_token())
    assert e.value.status == 503


def test_from_env(monkeypatch):
    monkeypatch.setenv("TARAZ_AUTH_ISSUER", ISSUER + "/")
    monkeypatch.setenv("TARAZ_AUTH_TENANT_ID", "taraz")
    monkeypatch.setenv("TARAZ_AUTH_JWKS_URL", "http://keycloak-service.identity:8080/realms/taraz/protocol/openid-connect/certs")
    v = TokenVerifier.from_env()
    assert v.issuer == ISSUER and v.tenant_id == "taraz" and v.audience is None
    assert v._keys.uri.startswith("http://keycloak-service.identity:8080/")


def test_from_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("TARAZ_AUTH_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="TARAZ_AUTH_ISSUER"):
        TokenVerifier.from_env()
