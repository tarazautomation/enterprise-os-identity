"""Errors raised when a request can't be trusted.

Each error carries the HTTP status a service should answer with and a short error code
(also used in the WWW-Authenticate header, following RFC 6750).
"""


class AuthError(Exception):
    status = 401
    code = "invalid_token"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class MissingToken(AuthError):
    """No bearer token on the request."""

    code = "missing_token"


class InvalidToken(AuthError):
    """Malformed, badly signed, wrong issuer or audience, or not an access token."""

    code = "invalid_token"


class ExpiredToken(InvalidToken):
    """The token's lifetime is over; the caller should get a fresh one."""

    code = "token_expired"


class WrongTenant(AuthError):
    """A valid token, but issued for another company (tenant). Not valid here."""

    code = "wrong_tenant"


class MissingRole(AuthError):
    """A valid token for this tenant, but without a role this action needs."""

    status = 403
    code = "insufficient_role"


class KeysUnavailable(AuthError):
    """Keycloak's signing keys couldn't be fetched, so no token can be checked right now."""

    status = 503
    code = "keys_unavailable"
