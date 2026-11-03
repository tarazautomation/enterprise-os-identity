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

from .conftest import _OTHER_KEY, ISSUER, OTHER_ISSUER, _jwks


def test_valid_token_gives_the_caller(verifier, make_token):
    p = verifier.verify(make_token())
    assert p.tenant_id == "taraz"
    assert p.username == "test.user@example.com"
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
    with pytest.raises(WrongTenant):
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


def test_token_without_a_type_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(drop=("typ",)))


def test_retired_signing_key_stops_working(verifier, make_token, other_jwks):
    token = make_token()
    assert verifier.verify(token).tenant_id == "taraz"
    verifier._keys.fetch_data = lambda: other_jwks       # the realm has rotated to a new key
    verifier._keys.jwk_set_cache.put(None)               # and the 5-minute key set cache has run out
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_error_messages_do_not_repeat_what_the_caller_sent(verifier, make_token):
    with pytest.raises(InvalidToken) as e:
        verifier.verify(make_token(kid="caller-chosen-key-id"))
    assert "caller-chosen-key-id" not in e.value.message


@pytest.mark.parametrize("junk", ["", "abc", "a.b", "a.b.c", "Bearer x.y.z"])
def test_garbage_is_refused(verifier, junk):
    with pytest.raises(InvalidToken):
        verifier.verify(junk)


def test_missing_expiry_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(drop=("exp",)))


# --- audience, service accounts, agents, key fetch failures ---

def test_token_for_another_service_is_refused(verifier, make_token):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(aud="account"))
    with pytest.raises(InvalidToken):
        verifier.verify(make_token(drop=("aud",)))
    assert verifier.verify(make_token(aud="knowledge-base")).tenant_id == "taraz"


def test_audience_is_required():
    with pytest.raises(TypeError):
        TokenVerifier(ISSUER, "taraz")


def test_audience_check_can_be_turned_off_explicitly(make_token):
    v = TokenVerifier(ISSUER, "taraz", audience=None)
    v._keys.fetch_data = _jwks
    assert v.verify(make_token(aud="account")).tenant_id == "taraz"


# --- several customers, one realm each ---

def test_a_service_for_several_tenants_says_whose_request_it_is(make_token, other_pem):
    v = TokenVerifier.for_tenants({"taraz": ISSUER, "acme": OTHER_ISSUER}, audience="knowledge-base")
    v._realms[ISSUER].keys.fetch_data = _jwks
    v._realms[OTHER_ISSUER].keys.fetch_data = lambda: _jwks(_OTHER_KEY, "acme-key")
    assert v.verify(make_token()).tenant_id == "taraz"
    acme = make_token(signing_pem=other_pem, kid="acme-key", iss=OTHER_ISSUER, tenant_id="acme")
    assert v.verify(acme).tenant_id == "acme"
    assert v.tenants == {"taraz": ISSUER, "acme": OTHER_ISSUER}


def test_a_realm_cannot_claim_another_tenant(make_token, other_pem):
    v = TokenVerifier.for_tenants({"taraz": ISSUER, "acme": OTHER_ISSUER}, audience="knowledge-base")
    v._realms[OTHER_ISSUER].keys.fetch_data = lambda: _jwks(_OTHER_KEY, "acme-key")
    with pytest.raises(WrongTenant):
        v.verify(make_token(signing_pem=other_pem, kid="acme-key", iss=OTHER_ISSUER, tenant_id="taraz"))


def test_a_realm_signing_with_another_realms_key_is_refused(make_token):
    v = TokenVerifier.for_tenants({"taraz": ISSUER, "acme": OTHER_ISSUER}, audience="knowledge-base")
    v._realms[OTHER_ISSUER].keys.fetch_data = lambda: _jwks(_OTHER_KEY, "acme-key")
    with pytest.raises(InvalidToken):
        v.verify(make_token(iss=OTHER_ISSUER, tenant_id="acme"))     # signed with taraz's key, kid test-key-1


def test_service_account_is_recognised(verifier, make_token):
    p = verifier.verify(make_token(preferred_username="service-account-scribe", azp="scribe", client_id="scribe",
                                   drop=("email", "name")))
    assert p.is_service_account and p.client_id == "scribe"


@pytest.mark.parametrize("extra", [{}, {"client_id": "other-client"}])
def test_username_alone_does_not_make_a_service_account(verifier, make_token, extra):
    p = verifier.verify(make_token(preferred_username="service-account-someone@example.com", azp="app-shell", **extra))
    assert not p.is_service_account


def test_agent_is_recognised_by_its_client(verifier, make_token):
    assert verifier.verify(make_token(azp="external-agents")).is_agent
    assert verifier.verify(make_token(azp="mcp-server")).is_agent      # after a token exchange, for the same person


def test_a_role_does_not_make_an_agent(verifier, make_token):
    assert not verifier.verify(make_token(realm_access={"roles": ["employee", "agent"]})).is_agent


def test_mcp_servers_own_service_account_is_not_an_agent(verifier, make_token):
    p = verifier.verify(make_token(preferred_username="service-account-mcp-server", azp="mcp-server",
                                   client_id="mcp-server"))
    assert p.is_service_account and not p.is_agent


def test_keys_unavailable_gives_503(make_token):
    v = TokenVerifier(ISSUER, "taraz", audience="knowledge-base")

    def down():
        raise PyJWKClientConnectionError("connection refused")

    v._keys.fetch_data = down
    with pytest.raises(KeysUnavailable) as e:
        v.verify(make_token())
    assert e.value.status == 503
    assert "connection refused" not in e.value.message


def test_from_env(monkeypatch):
    monkeypatch.setenv("TARAZ_AUTH_ISSUER", ISSUER + "/")
    monkeypatch.setenv("TARAZ_AUTH_TENANT_ID", "taraz")
    monkeypatch.setenv("TARAZ_AUTH_JWKS_URL", "http://keycloak-service.identity:8080/realms/taraz/protocol/openid-connect/certs")
    monkeypatch.setenv("TARAZ_AUTH_AUDIENCE", "knowledge-base")
    v = TokenVerifier.from_env()
    assert v.issuer == ISSUER and v.tenant_id == "taraz" and v.audience == "knowledge-base"
    assert v._keys.uri.startswith("http://keycloak-service.identity:8080/")


def test_from_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("TARAZ_AUTH_ISSUER", raising=False)
    monkeypatch.delenv("TARAZ_AUTH_TENANTS", raising=False)
    monkeypatch.setenv("TARAZ_AUTH_AUDIENCE", "knowledge-base")
    with pytest.raises(RuntimeError, match="TARAZ_AUTH_ISSUER"):
        TokenVerifier.from_env()


def test_from_env_needs_an_audience_or_an_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("TARAZ_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("TARAZ_AUTH_TENANT_ID", "taraz")
    monkeypatch.delenv("TARAZ_AUTH_AUDIENCE", raising=False)
    monkeypatch.delenv("TARAZ_AUTH_SKIP_AUDIENCE_CHECK", raising=False)
    with pytest.raises(RuntimeError, match="TARAZ_AUTH_AUDIENCE"):
        TokenVerifier.from_env()
    monkeypatch.setenv("TARAZ_AUTH_SKIP_AUDIENCE_CHECK", "true")
    assert TokenVerifier.from_env().audience is None


def test_from_env_for_several_tenants(monkeypatch):
    monkeypatch.setenv("TARAZ_AUTH_TENANTS", "taraz, acme")
    monkeypatch.setenv("TARAZ_AUTH_ISSUER_BASE", "https://auth.test/")
    monkeypatch.setenv("TARAZ_AUTH_JWKS_BASE", "http://keycloak-service.identity:8080")
    monkeypatch.setenv("TARAZ_AUTH_AUDIENCE", "knowledge-base")
    v = TokenVerifier.from_env()
    assert v.tenants == {"taraz": "https://auth.test/realms/taraz", "acme": "https://auth.test/realms/acme"}
    assert v._realms["https://auth.test/realms/acme"].keys.uri == \
        "http://keycloak-service.identity:8080/realms/acme/protocol/openid-connect/certs"
