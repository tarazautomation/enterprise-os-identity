"""Fixtures: an in-memory database, a fake Keycloak, and real signed tokens (no network)."""

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from taraz_auth import TokenVerifier

from tenant_service.app import create_app, make_engine
from tenant_service.config import Settings
from tenant_service.models import Base

ISSUER = "https://auth.test/realms/taraz"
KID = "k1"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


class FakeKeycloak:
    """Just enough of KeycloakAdmin: users, direct realm roles, enabled flags, logouts.
    fail["method"] = an exception makes that call raise it; slow["method"] = seconds makes it wait first."""

    realm = "taraz"

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.roles: dict[str, set[str]] = {}
        self.logouts: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.slow: dict[str, float] = {}

    def _call(self, method):
        if method in self.slow:
            time.sleep(self.slow[method])
        if method in self.fail:
            raise self.fail[method]

    def add_user(self, username, roles=(), enabled=True, uid=None, first="Test", last=None):
        uid = uid or str(uuid.uuid4())
        self.users[uid] = {"id": uid, "username": username, "email": f"{username}@example.com", "firstName": first,
                           "lastName": last or username.split("@")[0].title(), "enabled": enabled,
                           "attributes": {"entra_oid": [f"oid-{username}"], "tenant_id": ["taraz"]}}
        self.roles[uid] = set(roles)
        return uid

    def list_users(self):
        self._call("list_users")
        return [dict(u) for u in self.users.values()]

    def get_user(self, uid):
        self._call("get_user")
        return dict(self.users[uid]) if uid in self.users else None

    def role_member_ids(self, role):
        self._call("role_member_ids")
        if role == "default-roles-taraz":
            return set(self.users)
        return {u for u, rs in self.roles.items() if role in rs}

    def user_realm_roles(self, uid):
        self._call("user_realm_roles")
        return set(self.roles.get(uid, set())) | {"employee", "default-roles-taraz"}

    def add_roles(self, uid, roles):
        self._call("add_roles")
        self.roles[uid] |= set(roles)

    def remove_roles(self, uid, roles):
        self._call("remove_roles")
        self.roles[uid] -= set(roles)

    def set_enabled(self, uid, enabled):
        self._call("set_enabled")
        self.users[uid]["enabled"] = enabled

    def logout(self, uid):
        self._call("logout")
        self.logouts.append(uid)


@pytest.fixture
def kc():
    return FakeKeycloak()


def build_app(kc, database_url="sqlite://", **settings):
    """The real app on a test database, with the fake Keycloak and keys of its own."""
    settings = Settings(issuer=ISSUER, tenant_id="taraz", tenant_name="Taraz Technologies", client_id="tenant-service",
                        client_secret="x", database_url=database_url, **settings)
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    verifier = TokenVerifier(ISSUER, "taraz", audience="tenant-service")
    verifier._keys.fetch_data = _jwks
    return create_app(settings, kc=kc, verifier=verifier, engine=engine, run_sync_loop=False)


@pytest.fixture
def app_client(kc):
    with TestClient(build_app(kc)) as c:       # runs startup: seeds tenant + apps
        yield c


@pytest.fixture
def token():
    def _make(sub, roles=("employee",), username=None, tenant="taraz", **extra):
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": sub, "typ": "Bearer", "azp": "app-shell", "aud": ["tenant-service"],
                  "iat": now, "exp": now + 300,
                  "preferred_username": username or f"user-{sub[:6]}", "tenant_id": tenant,
                  "realm_access": {"roles": list(roles)}, **extra}
        return {"Authorization": "Bearer " + jwt.encode(claims, _PEM, algorithm="RS256", headers={"kid": KID})}
    return _make
