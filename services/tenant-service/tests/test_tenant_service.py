import pytest


@pytest.fixture
def people(kc, app_client, token):
    """One admin (Junaid), one employee (Ali), one process owner (Sara); synced once."""
    j = kc.add_user("junaid", roles={"admin"})
    a = kc.add_user("ali")
    s = kc.add_user("sara", roles={"process_owner"})
    r = app_client.post("/v1/sync", headers=token(j, roles=["admin", "employee"]))
    assert r.status_code == 200, r.text
    return {"junaid": j, "ali": a, "sara": s}


def admin_hdr(token, people):
    return token(people["junaid"], roles=["admin", "employee"], username="junaid")


# --- P2.2 done-when 1: a new Keycloak user shows up (here: on the next sync) ---

def test_new_keycloak_user_appears_after_sync(kc, app_client, token, people):
    hdr = token(people["ali"])
    assert app_client.get("/v1/members", headers=hdr).json()["total"] == 3
    newbie = kc.add_user("newbie")
    app_client.post("/v1/sync", headers=admin_hdr(token, people))
    m = app_client.get(f"/v1/members/{newbie}", headers=hdr).json()
    assert m["username"] == "newbie" and m["roles"] == ["employee"] and m["status"] == "active"
    assert set(m["apps"]) == {"app-shell", "scribe", "lectra", "knowledge-base", "mcp-server"}   # defaults


def test_me_fetches_a_brand_new_member_immediately(kc, app_client, token):
    fresh = kc.add_user("fresh")                                   # not synced yet
    r = app_client.get("/v1/me", headers=token(fresh))
    assert r.status_code == 200 and r.json()["username"] == "fresh"


def test_sync_marks_deleted_and_disabled_users(kc, app_client, token, people):
    kc.users[people["sara"]]["enabled"] = False
    del kc.users[people["ali"]]
    app_client.post("/v1/sync", headers=admin_hdr(token, people))
    hdr = admin_hdr(token, people)
    assert app_client.get(f"/v1/members/{people['sara']}", headers=hdr).json()["status"] == "disabled"
    assert app_client.get(f"/v1/members/{people['ali']}", headers=hdr).status_code == 404     # removed
    assert app_client.get("/v1/members?status=removed", headers=hdr).json()["total"] == 1


# --- P2.2 done-when 3: removing a role removes it in Keycloak (so from the next token) ---

def test_admin_changes_roles_in_keycloak(kc, app_client, token, people):
    hdr = admin_hdr(token, people)
    r = app_client.put(f"/v1/members/{people['sara']}/roles", headers=hdr, json={"roles": []})
    assert r.status_code == 200 and r.json()["roles"] == ["employee"]
    assert "process_owner" not in kc.roles[people["sara"]]                  # Keycloak changed
    r = app_client.put(f"/v1/members/{people['ali']}/roles", headers=hdr, json={"roles": ["admin", "process_owner"]})
    assert r.json()["roles"] == ["admin", "employee", "process_owner"]
    assert kc.roles[people["ali"]] == {"admin", "process_owner"}
    ev = app_client.get(f"/v1/members/{people['ali']}/events", headers=hdr).json()
    assert ev[0]["action"] == "roles_changed" and ev[0]["detail"]["added"] == ["admin", "process_owner"]
    assert ev[0]["actor_name"] == "junaid"


def test_only_admin_and_process_owner_are_editable(app_client, token, people):
    r = app_client.put(f"/v1/members/{people['ali']}/roles", headers=admin_hdr(token, people), json={"roles": ["agent"]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "role_not_editable"


def test_last_admin_cannot_be_removed(kc, app_client, token, people):
    hdr = admin_hdr(token, people)
    r = app_client.put(f"/v1/members/{people['junaid']}/roles", headers=hdr, json={"roles": []})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "last_admin"
    assert "admin" in kc.roles[people["junaid"]]
    # with a second admin it's allowed
    app_client.put(f"/v1/members/{people['ali']}/roles", headers=hdr, json={"roles": ["admin"]})
    assert app_client.put(f"/v1/members/{people['junaid']}/roles", headers=hdr, json={"roles": []}).status_code == 200


def test_employee_cannot_change_roles(app_client, token, people):
    r = app_client.put(f"/v1/members/{people['sara']}/roles", headers=token(people["ali"]), json={"roles": []})
    assert r.status_code == 403


# --- app access ---

def test_admin_sets_apps(app_client, token, people):
    hdr = admin_hdr(token, people)
    r = app_client.put(f"/v1/members/{people['ali']}/apps", headers=hdr, json={"apps": ["scribe"]})
    assert r.status_code == 200 and r.json()["apps"] == ["scribe"]
    assert app_client.get("/v1/members?app=lectra", headers=hdr).json()["total"] == 2
    assert app_client.put(f"/v1/members/{people['ali']}/apps", headers=hdr, json={"apps": ["nope"]}).status_code == 422


# --- disable / enable ---

def test_disable_and_enable(kc, app_client, token, people):
    hdr = admin_hdr(token, people)
    r = app_client.post(f"/v1/members/{people['ali']}/disable", headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert kc.users[people["ali"]]["enabled"] is False and people["ali"] in kc.logouts
    assert app_client.post(f"/v1/members/{people['ali']}/enable", headers=hdr).json()["status"] == "active"


def test_cannot_disable_yourself(app_client, token, people):
    r = app_client.post(f"/v1/members/{people['junaid']}/disable", headers=admin_hdr(token, people))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "self"


# --- directory and permissions ---

def test_directory_search_and_filters(app_client, token, people):
    hdr = token(people["ali"])
    assert [m["username"] for m in app_client.get("/v1/members?q=SAR", headers=hdr).json()["items"]] == ["sara"]
    assert app_client.get("/v1/members?role=admin", headers=hdr).json()["items"][0]["username"] == "junaid"


def test_service_account_can_read_but_has_no_me(app_client, token, people):
    svc = token("svc-id", roles=["default-roles-taraz"], username="service-account-scribe")
    assert app_client.get("/v1/members", headers=svc).status_code == 200
    assert app_client.get("/v1/me", headers=svc).status_code == 404


def test_tokens_without_member_roles_or_for_another_tenant_are_refused(app_client, token, people):
    assert app_client.get("/v1/members", headers=token("x", roles=[])).status_code == 403
    assert app_client.get("/v1/members", headers=token(people["ali"], tenant="other-company")).status_code == 401
    assert app_client.get("/v1/members").status_code == 401


def test_reference_lists(app_client, token, people):
    hdr = token(people["ali"])
    roles = {r["name"]: r["editable"] for r in app_client.get("/v1/roles", headers=hdr).json()}
    assert roles == {"admin": True, "process_owner": True, "employee": False, "agent": False}
    assert len(app_client.get("/v1/apps", headers=hdr).json()) == 5
    assert app_client.get("/v1/tenant", headers=hdr).json() == {"id": "taraz", "name": "Taraz Technologies", "status": "active"}


# --- invites (stored now; flow in V2) ---

def test_invites(app_client, token, people):
    hdr = admin_hdr(token, people)
    r = app_client.post("/v1/invites", headers=hdr, json={"email": "New.Person@Example.com", "roles": ["process_owner"]})
    assert r.status_code == 201 and r.json()["email"] == "new.person@example.com" and r.json()["status"] == "pending"
    assert app_client.post("/v1/invites", headers=hdr, json={"email": "new.person@example.com"}).status_code == 409
    assert app_client.post("/v1/invites", headers=hdr, json={"email": "ali@example.com"}).status_code == 409   # member
    assert app_client.post("/v1/invites", headers=hdr, json={"email": "not-an-email"}).status_code == 422
    inv_id = r.json()["id"]
    assert app_client.delete(f"/v1/invites/{inv_id}", headers=hdr).json()["status"] == "revoked"
    assert app_client.get("/v1/invites", headers=token(people["ali"])).status_code == 403


def test_status_and_health(app_client, token, people):
    st = app_client.get("/v1/status", headers=admin_hdr(token, people)).json()
    assert st["members_active"] == 3 and st["last_sync_ok"] is True
    assert app_client.get("/healthz").json()["ok"] is True
