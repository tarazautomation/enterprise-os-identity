"""Turn off people Microsoft has turned off: the leavers' sync.

Every ENTRA_SYNC_INTERVAL_SECONDS (15 minutes), read the company's Microsoft users through Microsoft Graph, then
disable in Keycloak, and sign out, every active member whose Microsoft account is disabled or deleted. Keycloak
never hears about this from Microsoft by itself, so without this sync a leaver keeps their sessions.

It never turns anyone back on: an admin does that in the admin portal. It changes nothing when Graph returns no
users, or when more than ENTRA_SYNC_MAX_DISABLE members would be disabled at once (a wrong tenant or app, say).

Needs an Entra app in the customer's tenant, separate from the sign-in app, with the Microsoft Graph application
permission User.Read.All (admin consent): ENTRA_TENANT_ID, ENTRA_SYNC_CLIENT_ID, ENTRA_SYNC_CLIENT_SECRET.
"""

from dataclasses import dataclass, field
from datetime import datetime

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

    def accounts(self) -> dict[str, bool]:
        """Every user in the tenant: Microsoft user id (oid) -> whether the account is enabled."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        url, accounts = f"{self._graph}/users?$select=id,accountEnabled&$top=999", {}
        while url:
            if not url.startswith(self._graph + "/"):           # never send the token anywhere else
                raise EntraError("Microsoft Graph returned a next page on another address")
            r = self._http.get(url, headers=headers)
            if r.status_code != 200:
                raise EntraError(f"Microsoft Graph: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            accounts.update({u["id"]: bool(u.get("accountEnabled")) for u in body.get("value", [])})
            url = body.get("@odata.nextLink")
        return accounts


@dataclass
class LeaverResult:
    at: datetime = field(default_factory=utcnow)
    checked: int = 0
    disabled: list[str] = field(default_factory=list)


def disable_leavers(session: Session, kc, tenant_id: str, accounts: dict[str, bool], max_disable: int) -> LeaverResult:
    """Disable, and sign out, every active member whose Microsoft account is disabled or gone."""
    if not accounts:
        raise EntraError("Microsoft Graph returned no users; nothing changed")
    members = session.scalars(select(Member).where(Member.tenant_id == tenant_id, Member.status == "active",
                                                   Member.entra_oid.is_not(None))).all()
    leavers = [(m, "deleted in Microsoft" if m.entra_oid not in accounts else "disabled in Microsoft")
               for m in members if not accounts.get(m.entra_oid, False)]
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
