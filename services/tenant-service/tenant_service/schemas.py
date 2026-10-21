"""Request and response shapes (these also make up the published API description, openapi.json)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MemberOut(BaseModel):
    id: str = Field(description="Keycloak user id; the same as 'sub' in the member's tokens")
    username: str
    email: str | None
    name: str
    first_name: str | None
    last_name: str | None
    status: str = Field(description="active | disabled | removed")
    roles: list[str]
    apps: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, m) -> "MemberOut":
        return cls(id=m.id, username=m.username, email=m.email, name=m.name, first_name=m.first_name,
                   last_name=m.last_name, status=m.status, roles=m.role_names, apps=m.app_ids,
                   created_at=m.created_at, updated_at=m.updated_at)


class MemberList(BaseModel):
    total: int
    items: list[MemberOut]


class RolesIn(BaseModel):
    roles: list[str] = Field(description="The member's editable roles after the change: any of admin, process_owner. "
                                         "employee (everyone) and agent (agents only) can't be set here.")


class AppsIn(BaseModel):
    apps: list[str] = Field(description="The apps this member may use after the change (ids from GET /v1/apps)")


class RoleOut(BaseModel):
    name: str
    description: str
    editable: bool


class AppOut(BaseModel):
    id: str
    name: str
    default_enabled: bool


class TenantOut(BaseModel):
    id: str
    name: str
    status: str


class InviteIn(BaseModel):
    email: str
    roles: list[str] = []
    apps: list[str] | None = Field(default=None, description="Omit to give the default apps")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        local, _, domain = v.partition("@")
        if not local or "." not in domain or " " in v:
            raise ValueError("not an email address")
        return v


class InviteOut(BaseModel):
    id: str
    email: str
    roles: list[str]
    apps: list[str]
    invited_by: str
    status: str
    created_at: datetime
    expires_at: datetime


class AgentOut(BaseModel):
    id: str
    acting_for: str
    client_id: str
    name: str
    created_at: datetime
    revoked_at: datetime | None


class EventOut(BaseModel):
    action: str
    actor_id: str | None
    actor_name: str | None
    detail: dict[str, Any]
    at: datetime


class SyncStatus(BaseModel):
    last_sync_at: datetime | None
    last_sync_ok: bool | None
    last_error: str | None
    members_active: int
    members_disabled: int
    members_removed: int
