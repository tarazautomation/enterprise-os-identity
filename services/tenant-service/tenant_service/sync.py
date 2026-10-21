"""Keep the member directory in step with Keycloak.

sync_all runs every SYNC_INTERVAL_SECONDS (30 s by default) and is cheap at company size:
one paged user listing plus one paged listing per role. sync_one refreshes a single member right
after this service changes them, so its answers never lag behind its own writes.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from .models import APPS, ROLES, App, Member, MemberApp, MemberEvent, MemberRole, utcnow

DIRECT_ROLES = ("admin", "process_owner", "agent")


@dataclass
class SyncResult:
    at: datetime = field(default_factory=utcnow)
    total: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0


def set_member_roles(m: Member, roles: set[str]) -> None:
    """Change the member's role rows to exactly `roles`, touching only what differs."""
    for row in [r for r in m.roles if r.role not in roles]:
        m.roles.remove(row)
    have = {r.role for r in m.roles}
    m.roles.extend(MemberRole(member_id=m.id, role=r) for r in sorted(roles - have))


def set_member_apps(m: Member, apps: set[str]) -> None:
    """Change the member's app rows to exactly `apps`, touching only what differs."""
    for row in [a for a in m.apps if a.app_id not in apps]:
        m.apps.remove(row)
    have = {a.app_id for a in m.apps}
    m.apps.extend(MemberApp(member_id=m.id, app_id=a) for a in sorted(apps - have))


def _attr(user: dict, name: str) -> str | None:
    v = (user.get("attributes") or {}).get(name)
    return v[0] if isinstance(v, list) and v else v


def _apply(session: Session, tenant_id: str, user: dict, roles: set[str], now: datetime) -> tuple[Member, bool, bool]:
    """Create or update one member from a Keycloak user and its roles. Returns (member, created, changed)."""
    m = session.get(Member, user["id"])
    created = m is None
    fields = {
        "username": user.get("username") or user["id"],
        "email": user.get("email"),
        "first_name": user.get("firstName"),
        "last_name": user.get("lastName"),
        "entra_oid": _attr(user, "entra_oid"),
        "status": "active" if user.get("enabled", True) else "disabled",
    }
    if created:
        defaults = session.scalars(select(App.id).where(App.default_enabled.is_(True))).all()
        m = Member(id=user["id"], tenant_id=tenant_id, created_at=now, **fields)
        m.apps = [MemberApp(member_id=m.id, app_id=a) for a in defaults]
        session.add(m)
    changed = created
    for k, v in fields.items():
        if getattr(m, k) != v:
            setattr(m, k, v)
            changed = True

    roles = {r for r in roles if r in ROLES}
    if set(m.role_names) != roles:
        before = m.role_names
        set_member_roles(m, roles)
        changed = True
        if not created:
            session.add(MemberEvent(tenant_id=tenant_id, member_id=m.id, action="roles_synced",
                                    detail={"before": before, "after": sorted(roles)}))
    if created:
        session.add(MemberEvent(tenant_id=tenant_id, member_id=m.id, action="joined",
                                detail={"roles": sorted(roles), "apps": m.app_ids}))
    if changed:
        m.updated_at = now
    m.last_synced_at = now
    return m, created, changed


def sync_all(session: Session, kc, tenant_id: str) -> SyncResult:
    now = utcnow()
    res = SyncResult(at=now)
    users = kc.list_users()
    holders = {r: kc.role_member_ids(r) for r in DIRECT_ROLES}
    employees = kc.role_member_ids("employee") | kc.role_member_ids(f"default-roles-{kc.realm}")

    seen = set()
    for u in users:
        roles = {r for r, ids in holders.items() if u["id"] in ids}
        if u["id"] in employees:
            roles.add("employee")
        _, created, changed = _apply(session, tenant_id, u, roles, now)
        seen.add(u["id"])
        res.created += created
        res.updated += changed and not created

    gone = session.scalars(select(Member).where(Member.tenant_id == tenant_id, Member.status != "removed",
                                                Member.id.not_in(seen) if seen else true())).all()
    for m in gone:
        m.status, m.updated_at = "removed", now
        session.add(MemberEvent(tenant_id=tenant_id, member_id=m.id, action="removed_from_keycloak", detail={}))
        res.removed += 1

    res.total = len(users)
    session.commit()
    return res


def sync_one(session: Session, kc, tenant_id: str, user_id: str) -> Member | None:
    """Refresh one member straight from Keycloak (after a change, or for a brand-new sign-in)."""
    user = kc.get_user(user_id)
    if user is None or user.get("serviceAccountClientId"):
        return None
    m, _, _ = _apply(session, tenant_id, user, kc.user_realm_roles(user_id), utcnow())
    session.commit()
    return m


def seed(session: Session, tenant_id: str, tenant_name: str) -> None:
    """Make sure the tenant row and the app catalogue exist (idempotent)."""
    from .models import Tenant
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=tenant_name))
    for app_id, name in APPS.items():
        if session.get(App, app_id) is None:
            session.add(App(id=app_id, name=name, default_enabled=True))
    session.commit()
