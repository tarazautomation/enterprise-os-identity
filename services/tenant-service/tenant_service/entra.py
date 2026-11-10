"""Turn off people Microsoft has turned off: the leavers' sync.

Every ENTRA_SYNC_INTERVAL_SECONDS (15 minutes), read the company's Microsoft users through Microsoft Graph, then
disable in Keycloak, and sign out, every active member whose Microsoft account is disabled or deleted. Keycloak
never hears about this from Microsoft by itself, so without this sync a leaver keeps their sessions.

With ENTRA_SIGNIN_APP_ID (the client id of the Entra app people sign in through, which has "assignment required"
on), a member who is no longer assigned to that app, directly or through a group, is disabled too: removing
someone from the app in Entra stops their Enterprise OS access, not only disabling their account.

It never turns anyone back on: an admin does that in the admin portal. It changes nothing when Graph returns no
users, no one assigned to the sign-in app, or when more than ENTRA_SYNC_MAX_DISABLE members would be disabled at
once (a wrong tenant or app, say).

Needs an Entra app in the customer's tenant, separate from the sign-in app, with the Microsoft Graph application
permission User.Read.All (admin consent): ENTRA_TENANT_ID, ENTRA_SYNC_CLIENT_ID, ENTRA_SYNC_CLIENT_SECRET. For the
assignment check also Application.Read.All and GroupMember.Read.All.

Guests in the Microsoft tenant (userType Guest: people from other companies invited into it) are disabled too,
because they aren't the company's staff. ENTRA_ALLOW_GUESTS=true keeps them.
"""

from dataclasses import dataclass, field
from datetime import datetime

from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .history import recorded
from .models import Member, utcnow
from .sync import sync_one

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"


class EntraError(RuntimeError):
    pass


class GraphUsers:
    """Reads a Microsoft tenant's users with the app's own token (client credentials)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *, http: httpx.Client | None = None,
                 login_base: str = LOGIN, graph_base: str = GRAPH):
        self._token_url = f"{login_base}/{tenant_id}/oauth2/v2.0/token"
        self._client_id, self._secret = client_id, client_secret
        self._graph = graph_base.rstrip("/")
        self._http = http or httpx.Client(timeout=30)

    def _token(self) -> str:
        r = self._http.post(self._token_url, data={
            "grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._secret,
            "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise EntraError(f"Microsoft refused the sync app's token: HTTP {r.status_code}")
        return r.json()["access_token"]

    def _get(self, url: str, headers: dict) -> dict:
        if not url.startswith(self._graph + "/"):               # never send the token anywhere else
            raise EntraError("Microsoft Graph returned a next page on another address")
        r = self._http.get(url, headers=headers)
        if r.status_code != 200:
            raise EntraError(f"Microsoft Graph: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _all(self, url: str, headers: dict) -> list[dict]:
        """Every item of a Graph listing, page by page."""
        items = []
        while url:
            body = self._get(url, headers)
            items.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
        return items

    def accounts(self) -> dict[str, bool]:
        """Every user in the tenant: Microsoft user id (oid) -> whether the account is enabled."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        users = self._all(f"{self._graph}/users?$select=id,accountEnabled&$top=999", headers)
        return {u["id"]: bool(u.get("accountEnabled")) for u in users}

    def guests(self) -> set[str]:
        """The tenant's guest users: people from other organisations invited into it."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        users = self._all(f"{self._graph}/users?$filter=userType%20eq%20'Guest'&$select=id&$top=999", headers)
        return {u["id"] for u in users}

    def assigned(self, app_client_id: str) -> set[str]:
        """The users assigned to an app (its enterprise application), directly or as members of assigned groups."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        sp = self._get(f"{self._graph}/servicePrincipals(appId='{quote(app_client_id, safe='')}')?$select=id", headers)
        assignments = self._all(f"{self._graph}/servicePrincipals/{quote(sp['id'], safe='')}/appRoleAssignedTo"
                                "?$select=principalId,principalType&$top=999", headers)
        users = {a["principalId"] for a in assignments if a.get("principalType") == "User"}
        for group in {a["principalId"] for a in assignments if a.get("principalType") == "Group"}:
            members = self._all(f"{self._graph}/groups/{quote(group, safe='')}/transitiveMembers/microsoft.graph.user"
                                "?$select=id&$top=999", headers)
            users |= {m["id"] for m in members}
        return users


@dataclass
class LeaverResult:
    at: datetime = field(default_factory=utcnow)
    checked: int = 0
    disabled: list[str] = field(default_factory=list)


def _why_leaving(oid: str, accounts: dict[str, bool], assigned: set[str] | None, guests: set[str]) -> str | None:
    if oid not in accounts:
        return "deleted in Microsoft"
    if not accounts[oid]:
        return "disabled in Microsoft"
    if oid in guests:
        return "a guest in Microsoft, not one of the company's own accounts"
    if assigned is not None and oid not in assigned:
        return "no longer assigned to the sign-in app in Microsoft"
    return None


def disable_leavers(session: Session, kc, tenant_id: str, accounts: dict[str, bool], max_disable: int,
                    assigned: set[str] | None = None, guests: set[str] | None = None) -> LeaverResult:
    """Disable, and sign out, every active member whose Microsoft account is disabled or gone, who is a guest
    (`guests`), or (with `assigned`, the users assigned to the sign-in app) who is no longer assigned to it."""
    if not accounts:
        raise EntraError("Microsoft Graph returned no users; nothing changed")
    if assigned is not None and not assigned:
        raise EntraError("nobody is assigned to the sign-in app in Microsoft (wrong ENTRA_SIGNIN_APP_ID?); "
                         "nothing changed")
    members = session.scalars(select(Member).where(Member.tenant_id == tenant_id, Member.status == "active",
                                                   Member.entra_oid.is_not(None))).all()
    leavers = [(m, why) for m in members if (why := _why_leaving(m.entra_oid, accounts, assigned, guests or set()))]
    if len(leavers) > max_disable:
        raise EntraError(f"{len(leavers)} members would be disabled, more than ENTRA_SYNC_MAX_DISABLE={max_disable}; "
                         "nothing changed")
    res = LeaverResult(checked=len(members))
    for m, reason in leavers:
        member_id = m.id
        with recorded(session, tenant_id, member_id, "disabled_by_microsoft", {"reason": reason}):
            kc.set_enabled(member_id, False)
            kc.logout(member_id)
            sync_one(session, kc, tenant_id, member_id)
        res.disabled.append(member_id)
    return res
