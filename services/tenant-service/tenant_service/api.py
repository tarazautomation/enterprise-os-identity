"""HTTP API, version 1.

Who may call what:
- members of the company (any employee or admin) and other services: read the directory
- admins: change roles and app access, disable/enable, invites, agents, change history, sync
Every request's token is checked by taraz-auth (signature, issuer, expiry, tenant).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from taraz_auth import Principal
from taraz_auth.fastapi import FastAPIAuth

from .models import (EDITABLE_ROLES, ROLES, Agent, App, Invite, Member, MemberApp, MemberEvent, MemberRole,
                     Tenant, utcnow)
from .schemas import (AgentOut, AppOut, AppsIn, EventOut, InviteIn, InviteOut, MemberList, MemberOut, RoleOut,
                      RolesIn, SyncStatus, TenantOut)
from .sync import set_member_apps, sync_all, sync_one

INVITE_DAYS = 14


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_router(auth: FastAPIAuth, get_session: Callable, get_kc: Callable, tenant_id: str, state: dict) -> APIRouter:
    r = APIRouter()

    # --- who may call ---

    async def reader(p: Principal = Depends(auth.principal)) -> Principal:   # Depends, so OpenAPI shows bearerAuth
        if p.is_service_account or p.has_role("employee") or p.has_role("admin"):
            return p
        raise HTTPException(403, {"error": "insufficient_role", "message": "only members of this company and services"})

    admin = auth.require("admin")

    def actor(p: Principal) -> dict:
        return {"actor_id": p.subject, "actor_name": p.name or p.username}

    def member_or_404(s: Session, member_id: str) -> Member:
        m = s.get(Member, member_id)
        if m is None or m.tenant_id != tenant_id or m.status == "removed":
            raise HTTPException(404, {"error": "not_found", "message": "no such member"})
        return m

    def guard_last_admin(member_id: str) -> None:
        kc = get_kc()
        enabled_admins = [i for i in kc.role_member_ids("admin") if (kc.get_user(i) or {}).get("enabled")]
        if member_id in enabled_admins and len(enabled_admins) <= 1:
            raise HTTPException(409, {"error": "last_admin", "message": "this is the only admin; make someone else admin first"})

    # --- directory ---

    @r.get("/v1/me", response_model=MemberOut, tags=["directory"], summary="The calling member")
    def me(p: Principal = Depends(reader), s: Session = Depends(get_session)):
        if p.is_service_account:
            raise HTTPException(404, {"error": "not_a_member", "message": "service accounts aren't members"})
        m = s.get(Member, p.subject) or sync_one(s, get_kc(), tenant_id, p.subject)   # first sign-in: fetch now
        if m is None:
            raise HTTPException(404, {"error": "not_found", "message": "no member record yet"})
        return MemberOut.of(m)

    @r.get("/v1/members", response_model=MemberList, tags=["directory"], summary="Search the member directory")
    def members(p: Principal = Depends(reader), s: Session = Depends(get_session),
                q: str | None = Query(None, description="Matches name, email or username"),
                role: str | None = None, app: str | None = None,
                status: str = Query("active", pattern="^(active|disabled|removed|all)$"),
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        stmt = select(Member).where(Member.tenant_id == tenant_id)
        if status != "all":
            stmt = stmt.where(Member.status == status)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(or_(func.lower(Member.email).like(like), func.lower(Member.username).like(like),
                                  func.lower(Member.first_name).like(like), func.lower(Member.last_name).like(like)))
        if role:
            stmt = stmt.where(Member.id.in_(select(MemberRole.member_id).where(MemberRole.role == role)))
        if app:
            stmt = stmt.where(Member.id.in_(select(MemberApp.member_id).where(MemberApp.app_id == app)))
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(func.lower(Member.username)).limit(limit).offset(offset)).all()
        return MemberList(total=total, items=[MemberOut.of(m) for m in rows])

    @r.get("/v1/members/{member_id}", response_model=MemberOut, tags=["directory"])
    def member(member_id: str, p: Principal = Depends(reader), s: Session = Depends(get_session)):
        return MemberOut.of(member_or_404(s, member_id))

    @r.get("/v1/roles", response_model=list[RoleOut], tags=["directory"])
    def roles(p: Principal = Depends(reader)):
        return [RoleOut(name=n, description=d, editable=n in EDITABLE_ROLES) for n, d in ROLES.items()]

    @r.get("/v1/apps", response_model=list[AppOut], tags=["directory"])
    def apps(p: Principal = Depends(reader), s: Session = Depends(get_session)):
        return [AppOut(id=a.id, name=a.name, default_enabled=a.default_enabled) for a in s.scalars(select(App).order_by(App.id))]

    @r.get("/v1/tenant", response_model=TenantOut, tags=["directory"])
    def tenant(p: Principal = Depends(reader), s: Session = Depends(get_session)):
        t = s.get(Tenant, tenant_id)
        return TenantOut(id=t.id, name=t.name, status=t.status)

    # --- admin: change members ---

    @r.put("/v1/members/{member_id}/roles", response_model=MemberOut, tags=["admin"],
           summary="Set a member's editable roles (admin, process_owner)")
    def put_roles(member_id: str, body: RolesIn, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        bad = set(body.roles) - EDITABLE_ROLES
        if bad:
            raise HTTPException(422, {"error": "role_not_editable",
                                      "message": f"only {sorted(EDITABLE_ROLES)} can be set here; not {sorted(bad)}"})
        member_or_404(s, member_id)
        kc = get_kc()
        current = kc.user_realm_roles(member_id) & EDITABLE_ROLES     # Keycloak is the master copy
        want = set(body.roles)
        add, remove = want - current, current - want
        if "admin" in remove:
            guard_last_admin(member_id)
        kc.add_roles(member_id, add)
        kc.remove_roles(member_id, remove)
        m = sync_one(s, kc, tenant_id, member_id)
        if m is None:
            raise HTTPException(404, {"error": "not_found", "message": "member no longer exists in Keycloak"})
        if add or remove:
            s.add(MemberEvent(tenant_id=tenant_id, member_id=member_id, action="roles_changed",
                              detail={"added": sorted(add), "removed": sorted(remove)}, **actor(p)))
            s.commit()
        return MemberOut.of(m)

    @r.put("/v1/members/{member_id}/apps", response_model=MemberOut, tags=["admin"], summary="Set the apps a member may use")
    def put_apps(member_id: str, body: AppsIn, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        known = set(s.scalars(select(App.id)).all())
        bad = set(body.apps) - known
        if bad:
            raise HTTPException(422, {"error": "unknown_app", "message": f"unknown app(s): {sorted(bad)}"})
        m = member_or_404(s, member_id)
        before = set(m.app_ids)
        set_member_apps(m, set(body.apps))
        if before != set(body.apps):
            m.updated_at = utcnow()
            s.add(MemberEvent(tenant_id=tenant_id, member_id=member_id, action="apps_changed",
                              detail={"added": sorted(set(body.apps) - before), "removed": sorted(before - set(body.apps))},
                              **actor(p)))
        s.commit()
        return MemberOut.of(m)

    @r.post("/v1/members/{member_id}/disable", response_model=MemberOut, tags=["admin"],
            summary="Stop a member signing in (and end their sessions)")
    def disable(member_id: str, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        if member_id == p.subject:
            raise HTTPException(409, {"error": "self", "message": "you can't disable yourself"})
        member_or_404(s, member_id)
        kc = get_kc()
        if "admin" in kc.user_realm_roles(member_id):
            guard_last_admin(member_id)
        kc.set_enabled(member_id, False)
        kc.logout(member_id)
        m = sync_one(s, kc, tenant_id, member_id)
        s.add(MemberEvent(tenant_id=tenant_id, member_id=member_id, action="disabled", detail={}, **actor(p)))
        s.commit()
        return MemberOut.of(m)

    @r.post("/v1/members/{member_id}/enable", response_model=MemberOut, tags=["admin"])
    def enable(member_id: str, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        member_or_404(s, member_id)
        kc = get_kc()
        kc.set_enabled(member_id, True)
        m = sync_one(s, kc, tenant_id, member_id)
        s.add(MemberEvent(tenant_id=tenant_id, member_id=member_id, action="enabled", detail={}, **actor(p)))
        s.commit()
        return MemberOut.of(m)

    @r.get("/v1/members/{member_id}/events", response_model=list[EventOut], tags=["admin"], summary="A member's change history")
    def events(member_id: str, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        rows = s.scalars(select(MemberEvent).where(MemberEvent.tenant_id == tenant_id, MemberEvent.member_id == member_id)
                         .order_by(MemberEvent.id.desc()).limit(200)).all()
        return [EventOut(action=e.action, actor_id=e.actor_id, actor_name=e.actor_name, detail=e.detail, at=e.at) for e in rows]

    # --- admin: invites (stored now; the email flow comes in V2) ---

    @r.post("/v1/invites", response_model=InviteOut, status_code=201, tags=["admin"])
    def create_invite(body: InviteIn, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        bad_roles = set(body.roles) - EDITABLE_ROLES
        if bad_roles:
            raise HTTPException(422, {"error": "role_not_editable", "message": f"can't invite with {sorted(bad_roles)}"})
        known = set(s.scalars(select(App.id)).all())
        apps = sorted(set(body.apps) if body.apps is not None else
                      set(s.scalars(select(App.id).where(App.default_enabled.is_(True))).all()))
        if set(apps) - known:
            raise HTTPException(422, {"error": "unknown_app", "message": f"unknown app(s): {sorted(set(apps) - known)}"})
        if s.scalar(select(Member.id).where(Member.tenant_id == tenant_id, func.lower(Member.email) == body.email,
                                            Member.status != "removed")):
            raise HTTPException(409, {"error": "already_member", "message": "someone with this email is already a member"})
        if s.scalar(select(Invite.id).where(Invite.tenant_id == tenant_id, Invite.email == body.email,
                                            Invite.status == "pending", Invite.expires_at > utcnow())):
            raise HTTPException(409, {"error": "already_invited", "message": "there's already a pending invite for this email"})
        now = utcnow()
        inv = Invite(id=str(uuid.uuid4()), tenant_id=tenant_id, email=body.email, roles=sorted(set(body.roles)), apps=apps,
                     invited_by=p.subject, status="pending", created_at=now, expires_at=now + timedelta(days=INVITE_DAYS))
        s.add(inv)
        s.commit()
        return InviteOut.model_validate(inv, from_attributes=True)

    @r.get("/v1/invites", response_model=list[InviteOut], tags=["admin"])
    def list_invites(p: Principal = Depends(admin), s: Session = Depends(get_session)):
        rows = s.scalars(select(Invite).where(Invite.tenant_id == tenant_id).order_by(Invite.created_at.desc())).all()
        now = utcnow()
        for inv in rows:
            if inv.status == "pending" and _aware(inv.expires_at) <= now:
                inv.status = "expired"
        s.commit()
        return [InviteOut.model_validate(i, from_attributes=True) for i in rows]

    @r.delete("/v1/invites/{invite_id}", response_model=InviteOut, tags=["admin"], summary="Revoke an invite")
    def revoke_invite(invite_id: str, p: Principal = Depends(admin), s: Session = Depends(get_session)):
        inv = s.get(Invite, invite_id)
        if inv is None or inv.tenant_id != tenant_id:
            raise HTTPException(404, {"error": "not_found", "message": "no such invite"})
        inv.status = "revoked"
        s.commit()
        return InviteOut.model_validate(inv, from_attributes=True)

    # --- admin: agents, sync ---

    @r.get("/v1/agents", response_model=list[AgentOut], tags=["admin"], summary="AI agents acting for members (filled by P2.5)")
    def agents(p: Principal = Depends(admin), s: Session = Depends(get_session)):
        rows = s.scalars(select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at.desc())).all()
        return [AgentOut.model_validate(a, from_attributes=True) for a in rows]

    @r.get("/v1/status", response_model=SyncStatus, tags=["admin"], summary="Last sync with Keycloak and member counts")
    def status_(p: Principal = Depends(admin), s: Session = Depends(get_session)):
        counts = dict(s.execute(select(Member.status, func.count()).where(Member.tenant_id == tenant_id)
                                .group_by(Member.status)).all())
        return SyncStatus(last_sync_at=state.get("at"), last_sync_ok=state.get("ok"), last_error=state.get("error"),
                          members_active=counts.get("active", 0), members_disabled=counts.get("disabled", 0),
                          members_removed=counts.get("removed", 0))

    @r.post("/v1/sync", response_model=SyncStatus, tags=["admin"], summary="Sync with Keycloak now")
    def sync_now(p: Principal = Depends(admin), s: Session = Depends(get_session)):
        res = sync_all(s, get_kc(), tenant_id)
        state.update(at=res.at, ok=True, error=None)
        return status_(p, s)

    return r
