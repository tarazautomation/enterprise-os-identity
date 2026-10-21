"""Test keys and a token factory. No network: the verifier's key fetch is replaced by a local JWKS."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from taraz_auth import TokenVerifier

ISSUER = "https://auth.test/realms/taraz"
KID = "test-key-1"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return key, pem


_KEY, _PEM = _keypair()
_OTHER_KEY, _OTHER_PEM = _keypair()   # same kid, different key: a forged signature


def _jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


@pytest.fixture
def make_token():
    def _make(signing_pem=_PEM, kid=KID, algorithm="RS256", drop=(), **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "1119fedd-661c-42ec-869a-a2f9d2240f18",
            "typ": "Bearer",
            "azp": "app-shell",
            "iat": now,
            "exp": now + 300,
            "preferred_username": "junaid.asrar@taraztechnologies.com",
            "email": "junaid.asrar@taraztechnologies.com",
            "name": "Junaid Asrar",
            "tenant_id": "taraz",
            "realm_access": {"roles": ["employee", "default-roles-taraz"]},
        }
        claims.update(overrides)
        for k in drop:
            claims.pop(k, None)
        return jwt.encode(claims, signing_pem, algorithm=algorithm, headers={"kid": kid})
    return _make


@pytest.fixture
def other_pem():
    return _OTHER_PEM


@pytest.fixture
def verifier():
    v = TokenVerifier(ISSUER, "taraz")
    v._keys.fetch_data = _jwks          # serve the test JWKS instead of calling Keycloak
    return v
