"""Database tables. The tenant service owns these; no other service writes them.

Keycloak stays the master copy for accounts and roles; members and their roles here are a mirror
kept fresh by the sync. App access, agents, invites and the change log live only here.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# Roles in the realm. Only admin and process_owner are edited through this service:
# employee comes to everyone through the realm's default role, agent belongs to AI agents.
ROLES: dict[str, str] = {
    "admin": "Runs Enterprise OS for the company: members, roles, app access",
    "process_owner": "Reviews and approves the ontology and processes for their area",
    "employee": "Every staff member; given to everyone automatically",
    "agent": "An AI agent (such as Claude) acting for one person, with only that person's access",
}
EDITABLE_ROLES = frozenset({"admin", "process_owner"})

# Apps a member can be given access to (enforced later through an "apps" claim in the token).
APPS: dict[str, str] = {
    "app-shell": "Enterprise OS app (web)",
    "scribe": "Scribe: meeting notes",
    "lectra": "Lectra: video to document",
    "knowledge-base": "Knowledge base",
    "mcp-server": "MCP server: Claude and other agents",
}


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)          # e.g. "taraz" (= tenant_id in tokens)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class App(Base):
    __tablename__ = "apps"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    default_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Member(Base):
    __tablename__ = "members"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)           # Keycloak user id (= "sub" in tokens)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entra_oid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")       # active | disabled | removed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list["MemberRole"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    apps: Mapped[list["MemberApp"]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip() or self.username

    @property
    def role_names(self) -> list[str]:
        return sorted(r.role for r in self.roles)

    @property
    def app_ids(self) -> list[str]:
        return sorted(a.app_id for a in self.apps)


class MemberRole(Base):
    __tablename__ = "member_roles"
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), primary_key=True)


class MemberApp(Base):
    __tablename__ = "member_apps"
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("apps.id"), primary_key=True)


class Agent(Base):
    """An AI agent acting for one member (filled by the Claude sign-in work, P2.5)."""
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    acting_for: Mapped[str] = mapped_column(ForeignKey("members.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(255))                     # e.g. external-agents
    name: Mapped[str] = mapped_column(String(200))                          # e.g. "Claude"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invite(Base):
    """An invitation for someone to join (stored now; the email flow comes in V2)."""
    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    apps: Mapped[list[str]] = mapped_column(JSON, default=list)
    invited_by: Mapped[str] = mapped_column(String(64))                     # member id
    status: Mapped[str] = mapped_column(String(20), default="pending")      # pending | accepted | revoked | expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemberEvent(Base):
    """Change history per member: who changed what, and when."""
    __tablename__ = "member_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    member_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = the sync with Keycloak
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
