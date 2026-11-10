import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import build_app
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


# --- removed from the sign-in app ---

def test_people_no_longer_assigned_to_the_sign_in_app_are_disabled(kc, staff, session):
    accounts = {"oid-junaid": True, "oid-ali": True, "oid-sara": True}
    res = disable_leavers(session, kc, "taraz", accounts, max_disable=5, assigned={"oid-junaid", "oid-sara"})
    assert res.disabled == [staff["ali"]] and staff["ali"] in kc.logouts
    reasons = {e.member_id: e.detail["reason"] for e in session.query(MemberEvent).filter_by(action="disabled_by_microsoft")}
    assert reasons == {staff["ali"]: "no longer assigned to the sign-in app in Microsoft"}


def test_nobody_assigned_changes_nothing(kc, staff, session):
    with pytest.raises(EntraError, match="nobody is assigned"):
        disable_leavers(session, kc, "taraz", {"oid-junaid": True, "oid-ali": True}, max_disable=5, assigned=set())
    assert not kc.logouts


def test_assignments_are_read_directly_and_through_groups():
    seen = []
    g = graph([{"id": "sp-1"},
               {"value": [{"principalId": "u1", "principalType": "User"},
                          {"principalId": "g1", "principalType": "Group"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/servicePrincipals/sp-1/appRoleAssignedTo?$skiptoken=y"},
               {"value": [{"principalId": "sp-other", "principalType": "ServicePrincipal"}]},
               {"value": [{"id": "u2"}, {"id": "u3"}]}], seen)
    assert g.assigned("7b315648-cdc2-432c-93e8-2881346f886d") == {"u1", "u2", "u3"}
    paths = [r.url.path for r in seen[1:]]
    assert paths == ["/v1.0/servicePrincipals(appId='7b315648-cdc2-432c-93e8-2881346f886d')",
                     "/v1.0/servicePrincipals/sp-1/appRoleAssignedTo",
                     "/v1.0/servicePrincipals/sp-1/appRoleAssignedTo",
                     "/v1.0/groups/g1/transitiveMembers/microsoft.graph.user"]


# --- guests ---

def test_guests_from_other_organisations_are_disabled(kc, staff, session):
    accounts = {"oid-junaid": True, "oid-ali": True, "oid-sara": True}
    res = disable_leavers(session, kc, "taraz", accounts, max_disable=5, guests={"oid-sara", "oid-someone-else"})
    assert res.disabled == [staff["sara"]] and staff["sara"] in kc.logouts
    reasons = {e.member_id: e.detail["reason"] for e in session.query(MemberEvent).filter_by(action="disabled_by_microsoft")}
    assert reasons == {staff["sara"]: "a guest in Microsoft, not one of the company's own accounts"}


def test_guests_are_read_with_a_filter():
    seen = []
    g = graph([{"value": [{"id": "g1"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=z"},
               {"value": [{"id": "g2"}]}], seen)
    assert g.guests() == {"g1", "g2"}
    assert seen[1].url.params["$filter"] == "userType eq 'Guest'"


def test_guests_are_kept_when_allowed(kc, token):
    sara = kc.add_user("sara")
    admin = kc.add_user("junaid", roles={"admin"})
    calls = []

    class Graph:
        def accounts(self):
            return {"oid-junaid": True, "oid-sara": True}

        def guests(self):
            calls.append("guests")
            return {"oid-sara"}

    for allow in (True, False):
        kc.users[sara]["enabled"] = True
        with TestClient(build_app(kc, graph=Graph(), entra_tenant_id="t", entra_client_id="c",
                                  entra_client_secret="s", entra_allow_guests=allow)) as c:
            assert c.post("/v1/sync", headers=token(admin, roles=["admin", "employee"])).status_code == 200
            c.app.state.run_entra()
        assert kc.users[sara]["enabled"] is allow
    assert calls == ["guests"]                    # Graph isn't even asked while guests are allowed

def test_status_shows_whether_the_leavers_sync_is_on(app_client, token, staff):
    st = app_client.get("/v1/status", headers=token(staff["junaid"], roles=["admin", "employee"])).json()
    assert st["entra_sync_enabled"] is False and st["entra_last_sync_at"] is None
    assert json.dumps(st)
