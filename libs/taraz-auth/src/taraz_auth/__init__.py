"""taraz-auth: shared sign-in checks for Enterprise OS services (P2.3).

    from taraz_auth import TokenVerifier
    verifier = TokenVerifier("https://auth.<domain>/realms/taraz", tenant_id="taraz")
    user = verifier.verify(bearer_token)          # raises an AuthError if it can't be trusted
    user.require_any_role("admin", "process_owner")
"""

from .errors import (
    AuthError,
    ExpiredToken,
    InvalidToken,
    KeysUnavailable,
    MissingRole,
    MissingToken,
    WrongTenant,
)
from .principal import Principal
from .service_client import ServiceTokenError, ServiceTokenProvider
from .verifier import TokenVerifier

__version__ = "1.0.0"

__all__ = [
    "AuthError",
    "ExpiredToken",
    "InvalidToken",
    "KeysUnavailable",
    "MissingRole",
    "MissingToken",
    "Principal",
    "ServiceTokenError",
    "ServiceTokenProvider",
    "TokenVerifier",
    "WrongTenant",
]
