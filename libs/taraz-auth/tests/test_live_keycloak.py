"""Against a real Keycloak. Skipped unless these are set:
TARAZ_AUTH_LIVE_ISSUER         e.g. http://localhost:8080/realms/taraz
TARAZ_AUTH_LIVE_CLIENT_ID      a confidential client with a service account, e.g. scribe
TARAZ_AUTH_LIVE_CLIENT_SECRET  its secret
"""

import os

import pytest

from taraz_auth import ServiceTokenProvider, TokenVerifier, WrongTenant

ISSUER = os.environ.get("TARAZ_AUTH_LIVE_ISSUER")
pytestmark = [pytest.mark.live, pytest.mark.skipif(not ISSUER, reason="TARAZ_AUTH_LIVE_ISSUER not set")]


@pytest.fixture(scope="module")
def service_token():
    p = ServiceTokenProvider.from_issuer(ISSUER, os.environ["TARAZ_AUTH_LIVE_CLIENT_ID"], os.environ["TARAZ_AUTH_LIVE_CLIENT_SECRET"])
    return p.get_token()


def test_real_service_token_is_accepted(service_token):
    p = TokenVerifier(ISSUER, "taraz").verify(service_token)
    assert p.tenant_id == "taraz"
    assert p.is_service_account
    assert p.client_id == os.environ["TARAZ_AUTH_LIVE_CLIENT_ID"]


def test_real_token_is_refused_by_another_tenants_service(service_token):
    with pytest.raises(WrongTenant):
        TokenVerifier(ISSUER, "other-company").verify(service_token)
