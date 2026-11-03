"""When things go wrong: Keycloak answering 404 or failing half way, two admins at once, a sign-in racing the sync,
and what callers are told."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from taraz_auth import ServiceTokenError

from conftest import build_app
from tenant_service.entra import disable_leavers
from tenant_service.keycloak_admin import KeycloakAdmin, KeycloakError
from tenant_service.models import Member, MemberEvent
from tenant_service.sync import sync_one


@pytest.fixture
def people(kc, app_client, token):
    """Junaid (admin), Ali (employee), Sara (process owner); synced once."""
    ids = {"junaid": kc.add_user("junaid", roles={"admin"}), "ali": kc.add_user("ali"),
           "sara": kc.add_user("sara", roles={"process_owner"})}
    assert app_client.post("/v1/sync", headers=token(ids["junaid"], roles=["admin", "employee"])).status_code == 200
    return ids


@pytest.fixture
def admin(token, people):
    return token(people["junaid"], roles=["admin", "employee"], username="junaid")


def events(app_client, admin, member_id):
    return app_client.get(f"/v1/members/{member_id}/events", headers=admin).json()


# --- a Keycloak 404 doesn't empty the directory (finding 13) ---

def test_a_404_on_a_listing_is_an_error_not_an_empty_realm():
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "Realm not found."})))
    kc = KeycloakAdmin("https://auth.test", "taraz", http)
    with pytest.raises(KeycloakError):
        kc.list_users()
    with pytest.raises(KeycloakError):
        kc.role_member_ids("admin")
    assert kc.get_user("someone") is None                       # one missing user is still just missing


def test_a_sync_that_would_remove_too_many_changes_nothing(kc, app_client, admin, people):
    extra = [kc.add_user(f"person{i}") for i in range(6)]
    app_client.post("/v1/sync", headers=admin)
    for uid in extra:
        del kc.users[uid]
    r = app_client.post("/v1/sync", headers=admin)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "too_many_removals"
    assert app_client.get("/v1/members?status=removed", headers=admin).json()["total"] == 0
    assert app_client.get("/v1/status", headers=admin).json()["last_sync_ok"] is False

    r = app_client.post("/v1/sync?allow_removals=true", headers=admin)        # an admin who expects it
    assert r.status_code == 200 and r.json()["members_removed"] == 6
    assert events(app_client, admin, "sync")[0]["action"] == "removals_allowed"


def test_a_sync_where_keycloak_lists_nobody_changes_nothing(kc, app_client, admin, people):
    kc.users.clear()
    r = app_client.post("/v1/sync", headers=admin)
    assert r.status_code == 409 and "listed no users" in r.json()["detail"]["message"]
    assert app_client.get("/v1/members", headers=admin).json()["total"] == 3


# --- the history (finding 14) ---

def test_a_role_change_that_fails_half_way_is_still_recorded(kc, app_client, admin, people):
    kc.fail["remove_roles"] = KeycloakError("Keycloak DELETE ...: HTTP 500")
    r = app_client.put(f"/v1/members/{people['sara']}/roles", headers=admin, json={"roles": ["admin"]})
    assert r.status_code == 502
    assert "admin" in kc.roles[people["sara"]]                  # the first half happened...
    ev = events(app_client, admin, people["sara"])[0]           # ...and the history says who tried what
    assert ev["action"] == "roles_changed" and ev["actor_name"] == "junaid"
    assert ev["detail"] == {"added": ["admin"], "removed": ["process_owner"], "outcome": "failed",
                            "error": "KeycloakError"}


def test_a_role_change_is_recorded_once_with_who_made_it(app_client, admin, people):
    app_client.put(f"/v1/members/{people['ali']}/roles", headers=admin, json={"roles": ["process_owner"]})
    ev = events(app_client, admin, people["ali"])
    assert [e["action"] for e in ev] == ["roles_changed", "joined"]          # no actor-less roles_synced as well
    assert ev[0]["detail"]["outcome"] == "done" and ev[0]["actor_name"] == "junaid"


def test_a_failed_disable_is_recorded(kc, app_client, admin, people):
    kc.fail["logout"] = KeycloakError("Keycloak POST ...: HTTP 500")
    assert app_client.post(f"/v1/members/{people['ali']}/disable", headers=admin).status_code == 502
    ev = events(app_client, admin, people["ali"])[0]
    assert ev["action"] == "disabled" and ev["detail"]["outcome"] == "failed"


def test_a_failed_leaver_disable_is_recorded(kc, app_client, people):
    kc.fail["logout"] = KeycloakError("Keycloak POST ...: HTTP 500")
    s = app_client.app.state.session_factory()
    with pytest.raises(KeycloakError):
        disable_leavers(s, kc, "taraz", {"oid-junaid": True, "oid-sara": True, "oid-ali": False}, max_disable=5)
    ev = s.query(MemberEvent).filter_by(member_id=people["ali"], action="disabled_by_microsoft").one()
    assert ev.detail["outcome"] == "failed" and ev.detail["reason"] == "disabled in Microsoft"
    s.close()


def test_invites_are_recorded(app_client, admin, people):
    inv = app_client.post("/v1/invites", headers=admin, json={"email": "new@example.com", "roles": ["admin"]}).json()
    app_client.delete(f"/v1/invites/{inv['id']}", headers=admin)
    ev = events(app_client, admin, f"invite:{inv['id']}")
    assert [e["action"] for e in ev] == ["invite_revoked", "invited"]
    assert ev[1]["detail"] == {"email": "new@example.com", "roles": ["admin"]} and ev[1]["actor_name"] == "junaid"


# --- two at once (finding 16) ---

def test_two_admins_removing_each_other_at_once_leave_one_admin(kc, token, tmp_path):
    # A file database, so each request has its own connection, as on Postgres
    with TestClient(build_app(kc, f"sqlite:///{tmp_path / 'race.db'}")) as c:
        j, a = kc.add_user("junaid", roles={"admin"}), kc.add_user("ali", roles={"admin"})
        c.post("/v1/sync", headers=token(j, roles=["admin", "employee"]))
        kc.slow["role_member_ids"] = 0.3            # both checks would see two admins without the lock
        with ThreadPoolExecutor(2) as pool:
            r1 = pool.submit(c.put, f"/v1/members/{a}/roles", headers=token(j, roles=["admin", "employee"]),
                             json={"roles": []})
            r2 = pool.submit(c.put, f"/v1/members/{j}/roles", headers=token(a, roles=["admin", "employee"]),
                             json={"roles": []})
            codes = sorted([r1.result().status_code, r2.result().status_code])
    assert codes == [200, 409]
    assert len([u for u, roles in kc.roles.items() if "admin" in roles]) == 1


def test_a_first_sign_in_racing_the_sync_still_works(kc, app_client):
    uid = kc.add_user("fresh")
    factory = app_client.app.state.session_factory
    with factory() as first:
        sync_one(first, kc, "taraz", uid)                       # the sync adds the new member...
    with factory() as second:
        real_get, stale = second.get, [True]

        def get(cls, ident, *a, **kw):                          # ...after /v1/me looked and found nobody
            if cls is Member and stale:
                stale.clear()
                return None
            return real_get(cls, ident, *a, **kw)

        second.get = get
        assert sync_one(second, kc, "taraz", uid).id == uid
        assert second.query(MemberEvent).filter_by(member_id=uid, action="joined").count() == 1


# --- what callers are told (findings 19 and 21) ---

@pytest.mark.parametrize("error", [
    KeycloakError("Keycloak GET /users: HTTP 500 <stack trace>"),
    ServiceTokenError("couldn't reach Keycloak at http://keycloak.internal:8080: <stack trace>"),
    httpx.ConnectError("[Errno 111] <stack trace>"),
])
def test_keycloak_failures_are_a_502_with_a_fixed_message(kc, app_client, admin, people, error):
    kc.fail["list_users"] = error
    r = app_client.post("/v1/sync", headers=admin)
    assert r.status_code == 502 and r.json()["detail"]["error"] == "keycloak_error"
    assert "stack trace" not in r.text and "internal" not in r.text


def test_api_docs_are_off_unless_turned_on(kc):
    with TestClient(build_app(kc)) as c:
        assert [c.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
    with TestClient(build_app(kc, docs_enabled=True)) as c:
        assert [c.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")] == [200, 200, 200]
