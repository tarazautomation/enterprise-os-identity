"""Check a Keycloak access token and turn it into a Principal.

Every check a service needs, in one place:
  1. the signature, using the realm's public keys (fetched once, then cached)
  2. the issuer is our realm, and the token hasn't expired
  3. it is an access token (typ "Bearer"), not an ID token
  4. tenant_id matches this cluster's tenant
  5. optionally, the audience includes this service
Role checks come afterwards, on the Principal (require_roles / require_any_role).
"""

import os
from typing import Any, Iterable

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from .errors import ExpiredToken, InvalidToken, KeysUnavailable, WrongTenant
from .principal import Principal


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        tenant_id: str,
        *,
        audience: str | None = None,
        jwks_url: str | None = None,
        leeway_seconds: int = 30,
        algorithms: Iterable[str] = ("RS256",),
        keys_cache_seconds: int = 300,
        http_timeout_seconds: float = 5.0,
    ):
        """
        issuer     the realm URL exactly as it appears in tokens, e.g. https://auth.<domain>/realms/taraz
        tenant_id  this cluster's tenant, e.g. "taraz"
        audience   if set, tokens must list it in "aud" (off until the P2.3 audience decision)
        jwks_url   where to fetch the realm's public keys; defaults to <issuer>/protocol/openid-connect/certs.
                   Set it when services reach Keycloak at an internal address that differs from the issuer.
        """
        self.issuer = issuer.rstrip("/")
        self.tenant_id = tenant_id
        self.audience = audience
        self.leeway = leeway_seconds
        self.algorithms = list(algorithms)
        self._keys = PyJWKClient(
            jwks_url or f"{self.issuer}/protocol/openid-connect/certs",
            cache_keys=True,
            lifespan=keys_cache_seconds,
            timeout=http_timeout_seconds,
        )

    @classmethod
    def from_env(cls, prefix: str = "TARAZ_AUTH_") -> "TokenVerifier":
        """Build from TARAZ_AUTH_ISSUER, TARAZ_AUTH_TENANT_ID and optional TARAZ_AUTH_AUDIENCE, TARAZ_AUTH_JWKS_URL."""
        try:
            issuer = os.environ[prefix + "ISSUER"]
            tenant_id = os.environ[prefix + "TENANT_ID"]
        except KeyError as missing:
            raise RuntimeError(f"environment variable {missing.args[0]} is not set") from None
        return cls(
            issuer,
            tenant_id,
            audience=os.environ.get(prefix + "AUDIENCE") or None,
            jwks_url=os.environ.get(prefix + "JWKS_URL") or None,
        )

    def verify(self, token: str) -> Principal:
        """Return the caller, or raise an AuthError subclass saying why the token can't be trusted."""
        if not token or token.count(".") != 2:
            raise InvalidToken("not a JWT")
        try:
            signing_key = self._keys.get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError as e:
            raise KeysUnavailable(f"couldn't fetch signing keys: {e}") from None
        except (PyJWKClientError, InvalidTokenError) as e:
            raise InvalidToken(f"unknown or unreadable signing key: {e}") from None

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": self.audience is not None},
            )
        except ExpiredSignatureError:
            raise ExpiredToken("token has expired") from None
        except InvalidTokenError as e:
            raise InvalidToken(str(e)) from None

        if claims.get("typ") not in (None, "Bearer"):
            raise InvalidToken(f"not an access token (typ={claims.get('typ')})")
        if claims.get("tenant_id") != self.tenant_id:
            raise WrongTenant(f"token is for tenant {claims.get('tenant_id')!r}, this service serves {self.tenant_id!r}")

        roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
        return Principal(
            subject=claims["sub"],
            tenant_id=claims["tenant_id"],
            roles=roles,
            username=claims.get("preferred_username"),
            email=claims.get("email"),
            name=claims.get("name"),
            client_id=claims.get("azp"),
            claims=claims,
        )
