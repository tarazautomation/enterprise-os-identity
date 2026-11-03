import json

import httpx
import pytest

from tenant_service.entra import EntraError, GraphUsers, disable_leavers
from tenant_service.models import Member, MemberEvent
from tenant_service.sync import sync_all


@pytest.fixture
def staff(kc, app_client, token):
    """Junaid (admin), Ali and Sara, synced into the directory. Their Microsoft ids are oid-<username>."""
    ids = {"junaid": kc.add_user("junaid", roles={"admin"}), "ali": kc.add_user("ali"), "sara": kc.add_user("sara")}
    assert app_client.post("/v1/sync", headers=token(ids["junaid"], roles=["admin", "employee"])).status_code == 200
    return ids


@pytest.fixture
def session(app_client):
    s = app_client.app.state.session_factory()
    yield s
    s.close()


def test_people_microsoft_disabled_or_deleted_are_disabled_and_signed_out(kc, staff, session):
    accounts = {"oid-junaid": True, "oid-ali": False}                     # Ali disabled, Sara deleted
    res = disable_leavers(session, kc, "taraz", accounts, max_disable=5)
    assert sorted(res.disabled) == sorted([staff["ali"], staff["sara"]]) and res.checked == 3
    assert kc.users[staff["ali"]]["enabled"] is False and kc.users[staff["sara"]]["enabled"] is False
    assert staff["ali"] in kc.logouts and staff["sara"] in kc.logouts
    assert kc.users[staff["junaid"]]["enabled"] is True
    assert session.get(Member, staff["ali"]).status == "disabled"
    reasons = {e.member_id: e.detail["reason"] for e in session.query(MemberEvent).filter_by(action="disabled_by_microsoft")}
    assert reasons == {staff["ali"]: "disabled in Microsoft", staff["sara"]: "deleted in Microsoft"}


def test_nobody_is_turned_back_on(kc, staff, session):
    kc.users[staff["ali"]]["enabled"] = False
    session.get(Member, staff["ali"]).status = "disabled"
    session.commit()
    disable_leavers(session, kc, "taraz", {"oid-junaid": True, "oid-ali": True, "oid-sara": True}, max_disable=5)
    assert kc.users[staff["ali"]]["enabled"] is False


def test_too_many_at_once_changes_nothing(kc, staff, session):
    with pytest.raises(EntraError, match="ENTRA_SYNC_MAX_DISABLE"):
        disable_leavers(session, kc, "taraz", {"oid-somebody-else": True}, max_disable=2)
    assert all(u["enabled"] for u in kc.users.values()) and not kc.logouts


def test_no_users_from_microsoft_changes_nothing(kc, staff, session):
    with pytest.raises(EntraError, match="no users"):
        disable_leavers(session, kc, "taraz", {}, max_disable=5)
    assert all(u["enabled"] for u in kc.users.values())


def test_members_without_a_microsoft_id_are_left_alone(kc, staff, session):
    local = kc.add_user("local.admin")
    kc.users[local]["attributes"] = {}
    sync_all(session, kc, "taraz")
    assert session.get(Member, local).entra_oid is None
    disable_leavers(session, kc, "taraz", {"oid-junaid": True, "oid-ali": True, "oid-sara": True}, max_disable=5)
    assert kc.users[local]["enabled"] is True


# --- reading Microsoft Graph ---

def graph(pages: list[dict], seen: list[httpx.Request]) -> GraphUsers:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})
        return httpx.Response(200, json=pages.pop(0))
    return GraphUsers("tenant-guid", "sync-app", "s3cret", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_graph_users_are_read_page_by_page():
    seen = []
    g = graph([{"value": [{"id": "a", "accountEnabled": True}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=x"},
               {"value": [{"id": "b", "accountEnabled": False}]}], seen)
    assert g.accounts() == {"a": True, "b": False}
    token_request = dict(httpx.QueryParams(seen[0].content.decode()))
    assert token_request["scope"] == "https://graph.microsoft.com/.default"
    assert seen[0].url.path == "/tenant-guid/oauth2/v2.0/token"
    assert all(r.headers["authorization"] == "Bearer graph-token" for r in seen[1:])


def test_a_next_page_on_another_address_is_refused():
    g = graph([{"value": [], "@odata.nextLink": "https://evil.example/users"}], [])
    with pytest.raises(EntraError, match="another address"):
        g.accounts()


def test_status_shows_whether_the_leavers_sync_is_on(app_client, token, staff):
    st = app_client.get("/v1/status", headers=token(staff["junaid"], roles=["admin", "employee"])).json()
    assert st["entra_sync_enabled"] is False and st["entra_last_sync_at"] is None
    assert json.dumps(st)
