"""Check a Keycloak access token and turn it into a Principal.

Every check a service needs, in one place:
  1. the token comes from a realm this service serves (one realm per customer)
  2. the signature, using that realm's public keys (cached for 5 minutes)
  3. the issuer and expiry
  4. the audience includes this service
  5. it is an access token (typ "Bearer"), not an ID token
  6. tenant_id matches the realm the token came from
Role checks come afterwards, on the Principal (require_roles / require_any_role).
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from .errors import ExpiredToken, InvalidToken, KeysUnavailable, WrongTenant
from .principal import DEFAULT_AGENT_CLIENTS, Principal

log = logging.getLogger("taraz_auth")


@dataclass(frozen=True)
class _Realm:
    tenant_id: str
    issuer: str
    keys: PyJWKClient


class TokenVerifier:
    def __init__(self, issuer: str, tenant_id: str, *, audience: str | None, jwks_url: str | None = None, **options):
        """A service for one customer. A service for several customers uses TokenVerifier.for_tenants().

        issuer     the realm URL exactly as it appears in tokens, e.g. https://auth.<domain>/realms/taraz
        tenant_id  that realm's tenant, e.g. "taraz"
        audience   this service's client id, e.g. "knowledge-base": tokens must list it in "aud". Required.
                   None turns the check off, only for a service that has no client of its own yet.
        jwks_url   where to fetch the realm's public keys; defaults to <issuer>/protocol/openid-connect/certs.
                   Set it when services reach Keycloak at an internal address that differs from the issuer.
        options    leeway_seconds (30), algorithms (RS256), keys_cache_seconds (300), http_timeout_seconds (5),
                   agent_clients (the clients agents act through, see Principal.is_agent)
        """
        self._configure({tenant_id: issuer}, audience=audience, jwks_urls={tenant_id: jwks_url} if jwks_url else {},
                        **options)

    @classmethod
    def for_tenants(cls, tenants: Mapping[str, str], *, audience: str | None,
                    jwks_urls: Mapping[str, str] | None = None, **options) -> "TokenVerifier":
        """A service for several customers. tenants maps each tenant id to its realm's issuer, and jwks_urls
        optionally maps tenant ids to internal key addresses. Principal.tenant_id says whose request it is."""
        self = cls.__new__(cls)
        self._configure(tenants, audience=audience, jwks_urls=jwks_urls or {}, **options)
        return self

    def _configure(self, tenants: Mapping[str, str], *, audience: str | None, jwks_urls: Mapping[str, str],
                   leeway_seconds: int = 30, algorithms: Iterable[str] = ("RS256",), keys_cache_seconds: int = 300,
                   http_timeout_seconds: float = 5.0, agent_clients: Iterable[str] = DEFAULT_AGENT_CLIENTS) -> None:
        if not tenants:
            raise ValueError("at least one tenant is needed")
        if set(jwks_urls) - set(tenants):
            raise ValueError(f"jwks_urls names unknown tenants: {sorted(set(jwks_urls) - set(tenants))}")
        self.audience = audience
        self.leeway = leeway_seconds
        self.algorithms = list(algorithms)
        self.agent_clients = frozenset(agent_clients)
        self._realms: dict[str, _Realm] = {}
        for tenant_id, issuer in tenants.items():
            issuer = issuer.rstrip("/")
            if issuer in self._realms:
                raise ValueError(f"tenants {self._realms[issuer].tenant_id!r} and {tenant_id!r} share an issuer")
            keys = PyJWKClient(jwks_urls.get(tenant_id) or f"{issuer}/protocol/openid-connect/certs",
                               lifespan=keys_cache_seconds, timeout=http_timeout_seconds)
            self._realms[issuer] = _Realm(tenant_id, issuer, keys)
        # With one realm, its issuer, tenant and key client stay available as attributes
        only = next(iter(self._realms.values())) if len(self._realms) == 1 else None
        self.issuer = only.issuer if only else None
        self.tenant_id = only.tenant_id if only else None
        self._keys = only.keys if only else None

    @property
    def tenants(self) -> dict[str, str]:
        """Tenant id -> issuer, for every realm this verifier accepts."""
        return {r.tenant_id: r.issuer for r in self._realms.values()}

    @classmethod
    def from_env(cls, prefix: str = "TARAZ_AUTH_") -> "TokenVerifier":
        """From environment variables.

        Always: TARAZ_AUTH_AUDIENCE (this service's client id), or TARAZ_AUTH_SKIP_AUDIENCE_CHECK=true.
        One customer: TARAZ_AUTH_ISSUER and TARAZ_AUTH_TENANT_ID, optional TARAZ_AUTH_JWKS_URL.
        Several: TARAZ_AUTH_TENANTS (comma-separated tenant ids) and TARAZ_AUTH_ISSUER_BASE (e.g. https://auth.<domain>);
        each issuer is <base>/realms/<tenant>. Optional TARAZ_AUTH_JWKS_BASE for Keycloak's internal address.
        """
        env = os.environ

        def required(name: str) -> str:
            if not env.get(prefix + name):
                raise RuntimeError(f"environment variable {prefix}{name} is not set")
            return env[prefix + name]

        audience = env.get(prefix + "AUDIENCE") or None
        if audience is None and env.get(prefix + "SKIP_AUDIENCE_CHECK", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(f"environment variable {prefix}AUDIENCE is not set: set it to this service's client id "
                               f"(or {prefix}SKIP_AUDIENCE_CHECK=true until the service has one)")
        if env.get(prefix + "TENANTS"):
            base = required("ISSUER_BASE").rstrip("/")
            jwks_base = (env.get(prefix + "JWKS_BASE") or "").rstrip("/")
            ids = [t.strip() for t in env[prefix + "TENANTS"].split(",") if t.strip()]
            return cls.for_tenants(
                {t: f"{base}/realms/{t}" for t in ids}, audience=audience,
                jwks_urls={t: f"{jwks_base}/realms/{t}/protocol/openid-connect/certs" for t in ids} if jwks_base else None)
        return cls(required("ISSUER"), required("TENANT_ID"), audience=audience,
                   jwks_url=env.get(prefix + "JWKS_URL") or None)

    def verify(self, token: str) -> Principal:
        """Return the caller, or raise an AuthError subclass saying why the token can't be trusted."""
        if not token or token.count(".") != 2:
            raise InvalidToken("not a JWT")
        realm = self._realm_for(token)
        try:
            signing_key = realm.keys.get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError as e:
            log.warning("couldn't fetch signing keys: %s", e)
            raise KeysUnavailable("couldn't fetch signing keys") from None
        except (PyJWKClientError, InvalidTokenError) as e:
            # Callers get a fixed message: the detail can repeat what the caller sent, or name internal addresses
            log.info("refused a token: %r", str(e))
            raise InvalidToken("unknown or unreadable signing key") from None

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.algorithms,
                issuer=realm.issuer,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": self.audience is not None},
            )
        except ExpiredSignatureError:
            raise ExpiredToken("token has expired") from None
        except InvalidTokenError as e:
            log.info("refused a token: %r", str(e))
            raise InvalidToken("the token failed validation") from None

        if claims.get("typ") != "Bearer":
            raise InvalidToken(f"not an access token (typ={claims.get('typ')})")
        if claims.get("tenant_id") != realm.tenant_id:
            raise WrongTenant(f"token is for tenant {claims.get('tenant_id')!r}, its realm serves {realm.tenant_id!r}")

        roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
        return Principal(
            subject=claims["sub"],
            tenant_id=realm.tenant_id,
            roles=roles,
            username=claims.get("preferred_username"),
            email=claims.get("email"),
            name=claims.get("name"),
            client_id=claims.get("azp"),
            claims=claims,
            agent_clients=self.agent_clients,
        )

    def _realm_for(self, token: str) -> _Realm:
        """The realm the token says it comes from, if this service serves it. This only picks whose keys to
        use: the signature and the issuer are checked afterwards."""
        try:
            issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except InvalidTokenError:
            raise InvalidToken("not a JWT") from None
        realm = self._realms.get(issuer.rstrip("/")) if isinstance(issuer, str) else None
        if realm is None:
            raise WrongTenant("the token comes from a realm this service doesn't serve")
        return realm
