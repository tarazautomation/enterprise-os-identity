"""Who is calling: a person, an AI agent acting for a person, or another service."""

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import MissingRole

SERVICE_ACCOUNT_PREFIX = "service-account-"


@dataclass(frozen=True)
class Principal:
    subject: str                      # Keycloak user id ("sub"), stable for the life of the account
    tenant_id: str
    roles: frozenset[str]
    username: str | None = None
    email: str | None = None
    name: str | None = None
    client_id: str | None = None      # the app that asked for the token ("azp")
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_service_account(self) -> bool:
        """True for service-to-service calls (Keycloak service-account tokens)."""
        return bool(self.username and self.username.startswith(SERVICE_ACCOUNT_PREFIX))

    @property
    def is_agent(self) -> bool:
        """True for an AI agent (e.g. Claude) acting for a person."""
        return "agent" in self.roles

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def require_roles(self, *roles: str) -> "Principal":
        """Raise MissingRole unless the caller has every one of these roles."""
        missing = [r for r in roles if r not in self.roles]
        if missing:
            raise MissingRole(f"needs role(s): {', '.join(missing)}")
        return self

    def require_any_role(self, *roles: str) -> "Principal":
        """Raise MissingRole unless the caller has at least one of these roles."""
        if not any(r in self.roles for r in roles):
            raise MissingRole(f"needs one of: {', '.join(roles)}")
        return self
