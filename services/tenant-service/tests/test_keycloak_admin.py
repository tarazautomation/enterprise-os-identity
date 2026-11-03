"""KeycloakAdmin's HTTP layer against a fake admin API: paging, what it sends, and how failures come out."""

import json

import httpx
import pytest

from tenant_service.keycloak_admin import KeycloakAdmin, KeycloakError

BASE = "https://auth.test/admin/realms/taraz"


def admin(handler) -> tuple[KeycloakAdmin, list[httpx.Request]]:
    seen = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return KeycloakAdmin("https://auth.test", "taraz", httpx.Client(transport=httpx.MockTransport(record))), seen


def test_users_are_read_page_by_page_and_service_accounts_left_out():
    people = [{"id": f"u{i}", "username": f"user{i}"} for i in range(150)]
    people.append({"id": "svc", "username": "service-account-scribe", "serviceAccountClientId": "scribe"})
    people.append({"id": "lookalike", "username": "service-account-ish"})        # a person, despite the name

    def handler(request):
        first, size = int(request.url.params["first"]), int(request.url.params["max"])
        return httpx.Response(200, json=people[first:first + size])

    kc, seen = admin(handler)
    ids = [u["id"] for u in kc.list_users()]
    assert len(ids) == 151 and "svc" not in ids and "lookalike" in ids
    assert [r.url.params["first"] for r in seen] == ["0", "100"]


def test_role_changes_send_the_role_representations():
    roles = {"admin": {"id": "r-admin", "name": "admin", "description": "x"},
             "process_owner": {"id": "r-po", "name": "process_owner"}}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=roles[request.url.path.rsplit("/", 1)[1]])
        return httpx.Response(204)

    kc, seen = admin(handler)
    kc.add_roles("u1", {"process_owner", "admin"})
    kc.remove_roles("u1", set())                                # nothing to remove: no call
    post = [r for r in seen if r.method == "POST"]
    assert len(post) == 1 and str(post[0].url) == f"{BASE}/users/u1/role-mappings/realm"
    assert json.loads(post[0].content) == [{"id": "r-admin", "name": "admin"}, {"id": "r-po", "name": "process_owner"}]
    kc.add_roles("u2", {"admin"})
    assert len([r for r in seen if r.method == "GET"]) == 2     # role ids are looked up once


def test_disabling_puts_the_whole_user_back_with_enabled_false():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "u1", "username": "ali", "enabled": True, "email": "a@x"})
        return httpx.Response(204)

    kc, seen = admin(handler)
    kc.set_enabled("u1", False)
    put = seen[-1]
    assert put.method == "PUT" and json.loads(put.content) == {"id": "u1", "username": "ali", "enabled": False,
                                                                "email": "a@x"}


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
def test_an_error_answer_is_a_keycloak_error(status):
    kc, _ = admin(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(KeycloakError, match=f"HTTP {status}"):
        kc.list_users()
    with pytest.raises(KeycloakError):
        kc.logout("u1")


def test_a_missing_user_or_role_is_missing_but_a_missing_listing_is_an_error():
    kc, _ = admin(lambda request: httpx.Response(404, json={"error": "not found"}))
    assert kc.get_user("u1") is None
    with pytest.raises(KeycloakError, match="doesn't exist"):
        kc.add_roles("u1", {"admin"})
    with pytest.raises(KeycloakError):
        kc.user_realm_roles("u1")
    with pytest.raises(KeycloakError):
        kc.set_enabled("u1", False)


def test_a_listing_that_isnt_a_list_is_an_error():
    kc, _ = admin(lambda request: httpx.Response(200, json={"error": "unexpected"}))
    with pytest.raises(KeycloakError, match="expected a list"):
        kc.role_member_ids("admin")


def test_network_failures_reach_the_caller_as_they_are():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    kc, _ = admin(handler)
    with pytest.raises(httpx.TimeoutException):                # the app answers these with a fixed 502
        kc.list_users()
