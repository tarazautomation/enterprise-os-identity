"""Real tokens from a real Keycloak (CI job "keycloak").

The job starts Keycloak, renders the Taraz realm and two test tenants (tests/integration/tenants) and applies them
with keycloak-config-cli. These tests check what services rely on: audiences follow keycloak/access.json, service
tokens are recognised, customers are kept apart, tenant-service can use the admin API, people get their roles in
the admin portal, and an agent signs in for a person and exchanges its token without ever holding admin.

    KC_URL=http://localhost:8080 KC_ADMIN_USER=admin KC_ADMIN_PASSWORD=... pytest tests/integration
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest
from taraz_auth import InvalidToken, ServiceTokenProvider, TokenVerifier, WrongTenant
from tenant_service.keycloak_admin import KeycloakAdmin

KC = os.environ.get("KC_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not KC, reason="KC_URL not set")

ROOT = Path(__file__).resolve().parents[2]
ACCESS = json.loads((ROOT / "keycloak" / "access.json").read_text(encoding="utf-8"))
PEOPLES_APPS = ("app-shell", "scribe-desktop", "external-agents")      # public clients: no service account
SERVICES = [c for c in ACCESS["calls"] if c not in PEOPLES_APPS]
A, B = "ci-alpha", "ci-beta"


def issuer(realm: str) -> str:
    return f"{KC}/realms/{realm}"


@pytest.fixture
def admin():
    """The master-realm admin API. A fresh token per test: master tokens last a minute."""
    r = httpx.post(f"{KC}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.environ["KC_ADMIN_USER"], "password": os.environ["KC_ADMIN_PASSWORD"]})
    r.raise_for_status()
    with httpx.Client(base_url=f"{KC}/admin/realms", timeout=30,
                      headers={"Authorization": f"Bearer {r.json()['access_token']}"}) as c:
        yield c


def secret_of(admin, realm: str, client_id: str) -> str:
    cid = admin.get(f"/{realm}/clients", params={"clientId": client_id}).json()[0]["id"]
    return admin.get(f"/{realm}/clients/{cid}/client-secret").json()["value"]


def service_token(admin, realm: str, client_id: str) -> str:
    return ServiceTokenProvider.from_issuer(issuer(realm), client_id, secret_of(admin, realm, client_id)).get_token()


def create_user(admin, realm: str, roles=(), password: str | None = None) -> tuple[str, str]:
    name = f"ci-{uuid.uuid4().hex[:8]}"
    rep = {"username": name, "email": f"{name}@example.com", "firstName": "CI", "lastName": name,
           "enabled": True, "emailVerified": True}
    if password:
        rep["credentials"] = [{"type": "password", "value": password, "temporary": False}]
    r = admin.post(f"/{realm}/users", json=rep)
    assert r.status_code == 201, r.text
    uid = r.headers["location"].rsplit("/", 1)[1]
    if roles:
        reps = [admin.get(f"/{realm}/roles/{name_}").json() for name_ in roles]
        admin.post(f"/{realm}/users/{uid}/role-mappings/realm", json=reps).raise_for_status()
    return uid, name


def form_action(r: httpx.Response) -> str:
    """Where the page's form posts to, as a full address (Keycloak writes some of them relative)."""
    m = re.search(r'<form[^>]*\baction="([^"]+)"', r.text)
    assert m, what_page(r.text)
    return urljoin(str(r.url), html.unescape(m.group(1)))


def hidden_fields(page: str) -> dict:
    """The form's hidden inputs, which Keycloak expects back (the consent screen's code, for one)."""
    fields = {}
    for tag in re.findall(r"<input[^>]*>", page):
        if re.search(r'type="hidden"', tag):
            name, value = re.search(r'name="([^"]*)"', tag), re.search(r'value="([^"]*)"', tag)
            if name:
                fields[html.unescape(name.group(1))] = html.unescape(value.group(1)) if value else ""
    return fields


def within_keycloak(h: httpx.Client, r: httpx.Response) -> httpx.Response:
    """Follow redirects while they stay on Keycloak (to its consent page, say); stop at the app's address."""
    for _ in range(10):
        if r.status_code not in (302, 303):
            return r
        location = urljoin(str(r.url), r.headers["location"])
        if not location.startswith(KC):
            return r
        r = h.get(location, headers=cookies(h))
    return r


def what_page(page: str) -> str:
    """A Keycloak page's title and message, for a readable failure."""
    title = re.search(r'id="kc-page-title"[^>]*>\s*([^<]+)', page)
    message = re.search(r'class="[^"]*(?:kc-feedback-text|alert__title|pf-m-danger)[^"]*"[^>]*>\s*([^<]+)', page)
    return f"page: {title.group(1).strip() if title else '?'}; message: {message.group(1).strip() if message else '-'}"


def cookies(h: httpx.Client) -> dict:
    """Keycloak marks its sign-in cookies Secure, and httpx won't send those to http://localhost by itself."""
    return {"Cookie": "; ".join(f"{c.name}={c.value}" for c in h.cookies.jar)}


def sign_in(realm: str, client_id: str, redirect_uri: str, username: str, password: str) -> str:
    """The browser flow by hand: Keycloak's sign-in form, the consent screen if any, then the code for a token."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with httpx.Client(timeout=30) as h:
        page = h.get(f"{issuer(realm)}/protocol/openid-connect/auth", follow_redirects=True, params={
            "client_id": client_id, "response_type": "code", "scope": "openid", "redirect_uri": redirect_uri,
            "state": "ci", "code_challenge": challenge, "code_challenge_method": "S256"})
        r = within_keycloak(h, h.post(form_action(page), headers=cookies(h),
                                      data={**hidden_fields(page.text), "username": username, "password": password}))
        if r.status_code == 200 and 'name="accept"' in r.text:              # the consent screen
            r = within_keycloak(h, h.post(form_action(r), headers=cookies(h),
                                          data={**hidden_fields(r.text), "accept": "Yes"}))
        assert r.status_code in (302, 303), what_page(r.text)
        query = parse_qs(urlparse(r.headers["location"]).query)
        assert "code" in query, f"no code in the redirect: {query}"
        code = query["code"][0]
        t = h.post(f"{issuer(realm)}/protocol/openid-connect/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier})
        assert t.status_code == 200, t.text
        return t.json()["access_token"]


# --- audiences follow keycloak/access.json ---

@pytest.mark.parametrize("client_id", SERVICES)
def test_service_tokens_reach_exactly_the_services_they_call(admin, client_id):
    token = service_token(admin, A, client_id)
    for target in ACCESS["calls"]:
        v = TokenVerifier(issuer(A), A, audience=target)
        if target in ACCESS["calls"][client_id]:
            p = v.verify(token)
            assert p.is_service_account and p.client_id == client_id and not p.is_agent
            assert "admin" not in p.roles
        else:
            with pytest.raises(InvalidToken):
                v.verify(token)


# --- customers are kept apart ---

def test_a_token_from_another_customer_is_refused(admin):
    token = service_token(admin, B, "scribe")
    with pytest.raises(WrongTenant):
        TokenVerifier(issuer(A), A, audience="lectra").verify(token)
    both = TokenVerifier.for_tenants({A: issuer(A), B: issuer(B)}, audience="lectra")
    assert both.verify(token).tenant_id == B


# --- tenant-service and the admin API ---

def test_tenant_service_can_manage_members_through_the_admin_api(admin):
    uid, _ = create_user(admin, A)
    tokens = ServiceTokenProvider.from_issuer(issuer(A), "tenant-service", secret_of(admin, A, "tenant-service"))
    kc = KeycloakAdmin(KC, A, httpx.Client(auth=tokens.httpx_auth(), timeout=15))
    assert uid in {u["id"] for u in kc.list_users()}
    kc.add_roles(uid, {"process_owner"})
    assert "process_owner" in kc.user_realm_roles(uid) and uid in kc.role_member_ids("process_owner")
    kc.remove_roles(uid, {"process_owner"})
    assert "process_owner" not in kc.user_realm_roles(uid)
    kc.set_enabled(uid, False)
    assert kc.get_user(uid)["enabled"] is False
    kc.logout(uid)


# --- people and agents ---

def test_people_get_their_roles_in_the_admin_portal(admin):
    password = secrets.token_urlsafe(16)
    uid, name = create_user(admin, A, roles=("admin",), password=password)
    p = TokenVerifier(issuer(A), A, audience="tenant-service").verify(
        sign_in(A, "app-shell", "http://localhost:3000/callback", name, password))
    assert p.subject == uid and p.client_id == "app-shell" and not p.is_agent
    assert {"employee", "admin"} <= p.roles


def test_an_agent_acts_for_the_person_and_never_as_admin(admin):
    password = secrets.token_urlsafe(16)
    uid, name = create_user(admin, A, roles=("admin", "process_owner"), password=password)
    token = sign_in(A, "external-agents", "https://claude.ai/api/mcp/auth_callback", name, password)
    p = TokenVerifier(issuer(A), A, audience="mcp-server").verify(token)
    assert p.subject == uid and p.client_id == "external-agents" and p.is_agent
    assert {"employee", "process_owner"} <= p.roles and "admin" not in p.roles
    with pytest.raises(InvalidToken):
        TokenVerifier(issuer(A), A, audience="knowledge-base").verify(token)        # only mcp-server accepts it

    # mcp-server exchanges it for a token knowledge-base accepts, still for the same person
    r = httpx.post(f"{issuer(A)}/protocol/openid-connect/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token, "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "knowledge-base", "client_id": "mcp-server", "client_secret": secret_of(admin, A, "mcp-server")})
    assert r.status_code == 200, r.text
    q = TokenVerifier(issuer(A), A, audience="knowledge-base").verify(r.json()["access_token"])
    assert q.subject == uid and q.client_id == "mcp-server" and q.is_agent and "admin" not in q.roles


# --- realm settings ---

@pytest.mark.parametrize("realm", ["taraz", A])
def test_realm_hardening_is_in_place(admin, realm):
    r = admin.get(f"/{realm}").json()
    for setting in ("bruteForceProtected", "eventsEnabled", "adminEventsEnabled", "revokeRefreshToken",
                    "offlineSessionMaxLifespanEnabled"):
        assert r[setting] is True, setting
    default_role = admin.get(f"/{realm}/roles/default-roles-{realm}/composites").json()
    assert "manage-account" not in {x["name"] for x in default_role}


def test_taraz_sends_people_straight_to_microsoft(admin):
    steps = admin.get("/taraz/authentication/flows/browser/executions").json()
    step = next(s for s in steps if s.get("providerId") == "identity-provider-redirector")
    config = admin.get(f"/taraz/authentication/config/{step['authenticationConfig']}").json()
    assert config["config"]["defaultProvider"] == "entra"


def test_keycloak_tells_scribe_when_someone_signs_out(admin):
    urls = set()
    for client_id in ("scribe", "scribe-staging"):              # production and staging, each its own address
        scribe = admin.get("/taraz/clients", params={"clientId": client_id}).json()[0]
        assert scribe["attributes"]["backchannel.logout.url"].endswith("/auth/keycloak/backchannel-logout")
        assert scribe["attributes"]["backchannel.logout.session.required"] == "true"
        urls.add(scribe["attributes"]["backchannel.logout.url"])
    assert len(urls) == 2


def test_scribe_staging_is_a_copy_of_scribe_with_its_own_audience(admin):
    token = service_token(admin, "taraz", "scribe-staging")
    p = TokenVerifier(issuer("taraz"), "taraz", audience="tenant-service").verify(token)
    assert p.is_service_account and p.client_id == "scribe-staging"
    TokenVerifier(issuer("taraz"), "taraz", audience="scribe-staging").verify(token)    # verifies its own sign-ins
    with pytest.raises(InvalidToken):
        TokenVerifier(issuer("taraz"), "taraz", audience="scribe").verify(token)          # not production's


# --- Google sign-in (ci-beta has it, ci-alpha doesn't) ---

def test_google_sign_in_is_offered_with_microsoft(admin):
    google = admin.get(f"/{B}/identity-provider/instances/google").json()
    assert google["providerId"] == "google" and google["enabled"] is True
    assert google["trustEmail"] is False                                 # Google's word on an email never links
    assert google["firstBrokerLoginFlowAlias"] == "first-google-sign-in"
    assert google["postBrokerLoginFlowAlias"] == "after-microsoft-sign-in"   # admins enter their one-time code
    assert "hostedDomain" not in google["config"]                         # any Google account
    mappers = admin.get(f"/{B}/identity-provider/instances/google/mappers").json()
    assert {m["config"].get("attribute") for m in mappers} == {"tenant_id"}  # tokens carry the tenant
    assert admin.get(f"/{A}/identity-provider/instances/google").status_code == 404


def test_an_app_sends_people_to_google_with_a_hint():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with httpx.Client(timeout=30) as h:
        r = h.get(f"{issuer(B)}/protocol/openid-connect/auth", params={
            "client_id": "app-shell", "response_type": "code", "scope": "openid", "state": "ci",
            "redirect_uri": "http://localhost:3000/callback", "kc_idp_hint": "google",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        r = within_keycloak(h, r)
    assert r.status_code in (302, 303), what_page(r.text)
    to = urlparse(r.headers["location"])
    query = parse_qs(to.query)
    assert to.netloc == "accounts.google.com"
    assert query["client_id"] == ["ci-beta.apps.googleusercontent.com"]
    assert query["code_challenge_method"] == ["S256"] and "hd" not in query
    assert query["redirect_uri"] == [f"{issuer(B)}/broker/google/endpoint"]


def test_google_links_to_an_existing_account_only_after_signing_in_to_it(admin):
    steps = admin.get(f"/{B}/authentication/flows/first-google-sign-in/executions").json()
    used = [s.get("providerId") for s in steps if s.get("requirement") != "DISABLED"]
    assert "idp-create-user-if-unique" in used                             # a new account when the email is free
    assert "idp-confirm-link" in used and "idp-username-password-form" in used
    assert "idp-email-verification" not in used and "idp-auto-link" not in used
