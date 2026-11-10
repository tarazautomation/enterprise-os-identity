"""Render each customer's realm from the shared template. One realm per customer (tenant).

    python keycloak/render.py                         write keycloak/realm-<tenant>.json for each keycloak/tenants/<tenant>.json
    python keycloak/render.py --check                 fail if a written file is out of date (CI runs this)
    python keycloak/render.py --tenants DIR --out DIR other tenant files, e.g. the CI test tenants

Inputs:
  template/realm.json  the realm, with {{...}} where customers differ
  access.json          which app may call which (token audiences), which user roles each app's tokens carry,
                       which clients act for agents, and the admin API rights of tenant-service
  tenants/<id>.json    one customer: realm name, its Microsoft (Entra) tenant and app, redirect addresses,
                       back-channel logout addresses, the name of the variable holding its Microsoft secret, and
                       client_copies: more clients made from one in the template, e.g. a staging copy of scribe
                       with its own addresses (a client has one back-channel logout address), "lab": true
                       for a test realm, the only kind allowed localhost addresses for its web apps, and
                       "google": {"client_id": ...} to offer Google sign-in next to Microsoft (add_google)

No secret is in any of these files. Client secrets are left to Keycloak, which generates one per client, and the
Microsoft secret is written as $(env:NAME) for keycloak-config-cli to fill in when it applies the realm.
"""

import argparse
import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r"\{\{([a-z_.:-]+)\}\}")
REALM_NAME = re.compile(r"[a-z][a-z0-9-]{1,39}")
SECRET_ENV = re.compile(r"KC_[A-Z0-9_]+")
LOCAL = re.compile(r"(?i)^https?://(localhost|127\.0\.0\.1|\[::1\])([:/]|$)")
DESKTOP_APPS = {"scribe-desktop"}      # a desktop app's sign-in returns to a port on the person's own machine


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def secret_ref(tenant: dict, name: str) -> str:
    """The placeholder keycloak-config-cli fills with the secret named in the tenant file's "secrets"."""
    env = tenant["secrets"][name]
    if not SECRET_ENV.fullmatch(env):
        raise ValueError(f"secret variable {env!r} must look like KC_NAME")
    return f"$(env:{env})"


def fill(node, tenant: dict):
    """Replace every {{name}} in the template with the tenant's value."""
    if isinstance(node, dict):
        return {k: fill(v, tenant) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v, tenant) for v in node]
    if not isinstance(node, str):
        return node
    whole = TOKEN.fullmatch(node)
    if whole and whole.group(1).startswith("redirect_uris:"):
        return list(tenant["redirect_uris"][whole.group(1).split(":", 1)[1]])

    def one(m: re.Match) -> str:
        key = m.group(1)
        if key.startswith("secret:"):
            return secret_ref(tenant, key.split(":", 1)[1])
        value = tenant
        for part in key.split("."):
            value = value[part]
        if not isinstance(value, str):
            raise ValueError(f"{{{{{key}}}}} must be text")
        return value

    return TOKEN.sub(one, node)


def add_copies(template: dict, access: dict, copies: dict) -> None:
    """Add each copy as a client of its own: the original's settings, calls and roles, under its own client id.
    Its redirect addresses and back-channel logout address come from the tenant file under the copy's id."""
    clients = {c["clientId"]: c for c in template["clients"]}
    for cid, spec in copies.items():
        src = spec["of"]
        if not REALM_NAME.fullmatch(cid) or cid in clients:
            raise ValueError(f"client_copies: {cid!r} must be a new client id (lowercase letters, digits, dashes)")
        if src not in clients:
            raise ValueError(f"client_copies: {cid!r} copies {src!r}, which isn't a client in the template")
        c = copy.deepcopy(clients[src])
        c["clientId"], c["name"] = cid, spec.get("name", f"{clients[src].get('name', src)} ({cid})")
        c["redirectUris"] = f"{{{{redirect_uris:{cid}}}}}"
        template["clients"].append(c)
        clients[cid] = c
        if src in access["calls"]:                              # a client that verifies its own sign-ins lists itself
            access["calls"][cid] = [cid if t == src else t for t in access["calls"][src]]
        for key in ("user_roles", "admin_api"):
            if src in access[key]:
                access[key][cid] = list(access[key][src])
        if src in access["token_exchange"]:
            access["token_exchange"].append(cid)


def audience_scope(target: str) -> dict:
    return {
        "name": f"aud-{target}",
        "description": f"Puts {target} in the token's audience, so {target} accepts the token",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
        "protocolMappers": [{
            "name": f"audience {target}",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "consentRequired": False,
            "config": {"included.client.audience": target, "id.token.claim": "false",
                       "access.token.claim": "true", "introspection.token.claim": "true"},
        }],
    }


def apply_access(realm: dict, access: dict) -> None:
    clients = {c["clientId"]: c for c in realm["clients"]}

    def client(cid: str) -> dict:
        if cid not in clients:
            raise ValueError(f"access.json names {cid!r}, which isn't a client in the template")
        return clients[cid]

    # Every Enterprise OS client (the ones with tenant_id in their tokens) must say whom it calls, even if no one
    ours = sorted(cid for cid, c in clients.items() if "tenant" in c.get("defaultClientScopes", []))
    missing = [cid for cid in ours if cid not in access["calls"]]
    if missing:
        raise ValueError(f"access.json 'calls' has no entry for {missing}")

    targets = sorted({t for ts in access["calls"].values() for t in ts})
    for t in targets:
        client(t)
    realm["clientScopes"] = realm["clientScopes"] + [audience_scope(t) for t in targets]
    for cid, ts in access["calls"].items():
        c = client(cid)
        c["fullScopeAllowed"] = False            # tokens carry only the roles mapped below
        c["defaultClientScopes"] = c["defaultClientScopes"] + [f"aud-{t}" for t in ts]

    scope_mappings = realm.setdefault("scopeMappings", [])
    for cid, roles in access["user_roles"].items():
        client(cid)
        scope_mappings.append({"client": cid, "roles": list(roles)})

    client_scope_mappings = realm.setdefault("clientScopeMappings", {})
    for cid, roles in access["admin_api"].items():
        client(cid)
        client_scope_mappings.setdefault("realm-management", []).append({"client": cid, "roles": list(roles)})

    for cid in access["token_exchange"]:
        client(cid).setdefault("attributes", {})["standard.token.exchange.enabled"] = "true"


def microsoft_first(realm: dict, on: bool) -> None:
    """Send people straight to Microsoft instead of showing Keycloak's own sign-in form first."""
    if not on:
        return
    flow = next(f for f in realm["authenticationFlows"] if f["alias"] == realm["browserFlow"])
    step = next(e for e in flow["authenticationExecutions"] if e.get("authenticator") == "identity-provider-redirector")
    step["authenticatorConfig"] = "microsoft-first"
    realm.setdefault("authenticatorConfig", []).append({"alias": "microsoft-first", "config": {"defaultProvider": "entra"}})


def check_addresses(tenant: dict) -> None:
    """A real customer's web apps return only to their real addresses. On localhost, any program on the person's
    machine could receive their sign-in, so localhost is for test realms ("lab": true) and desktop apps."""
    if tenant.get("lab"):
        return
    for cid, uris in tenant["redirect_uris"].items():
        local = [u for u in uris if LOCAL.match(u)]
        if local and cid not in DESKTOP_APPS:
            raise ValueError(f"{tenant['realm']}: {cid} may not return to {local[0]} (localhost is for lab realms)")
        plain = [u for u in uris if u.lower().startswith("http://") and not LOCAL.match(u)]
        if plain:
            raise ValueError(f"{tenant['realm']}: {cid} returns to {plain[0]}; use https")


def step(requirement: str, authenticator: str = "", flow: str = "", config: str = "", priority: int = 0) -> dict:
    """One step of an authentication flow: an authenticator, or a sub-flow by its alias."""
    e = {"authenticatorFlow": bool(flow), "requirement": requirement, "priority": priority,
         "autheticatorFlow": bool(flow), "userSetupAllowed": False}       # sic: Keycloak's own spelling
    e.update({"flowAlias": flow} if flow else {"authenticator": authenticator})
    if config:
        e["authenticatorConfig"] = config
    return e


def add_google(realm: dict, tenant: dict) -> None:
    """Sign in with Google next to Microsoft. Any Google account may sign in, unless the tenant file gives a
    "hosted_domain" (one Google Workspace domain, which Google is asked for and Keycloak checks in its answer).

    Microsoft stays the default: an app offers Google by sending kc_idp_hint=google. A first Google sign-in makes a
    new account, unless one with that email exists: then the person must sign in to that account (with Microsoft)
    before Google is linked to it. Google's word on the email is never enough to link, so owning a Google account
    with someone's address doesn't get you their account. Admins enter their one-time code, as after Microsoft."""
    google = tenant.get("google")
    if not google:
        return
    config = {
        "clientId": google["client_id"],
        "clientSecret": secret_ref(tenant, "google"),
        "defaultScope": "openid profile email",
        "pkceEnabled": "true",
        "pkceMethod": "S256",
        "syncMode": "IMPORT",                 # Microsoft keeps names and email current for linked accounts
        "acceptsPromptNoneForwardFromClient": "false",
        "offlineAccess": "false",
        "userIp": "false",
        "filteredByClaim": "false",
        "guiOrder": "2",
    }
    if google.get("hosted_domain"):
        config["hostedDomain"] = google["hosted_domain"]
    realm["identityProviders"].append({
        "alias": "google", "displayName": "Google", "providerId": "google", "enabled": True,
        "trustEmail": False, "storeToken": False, "addReadTokenRoleOnCreate": False, "authenticateByDefault": False,
        "linkOnly": False, "hideOnLogin": False,
        "firstBrokerLoginFlowAlias": "first-google-sign-in", "postBrokerLoginFlowAlias": "after-microsoft-sign-in",
        "config": config,
    })
    realm["identityProviderMappers"].append({
        "name": f"tenant_id = {tenant['realm']}", "identityProviderAlias": "google",
        "identityProviderMapper": "hardcoded-attribute-idp-mapper",
        "config": {"syncMode": "INHERIT", "attribute.value": tenant["realm"], "attribute": "tenant_id"},
    })
    realm["authenticationFlows"] += [
        {"alias": "first-google-sign-in", "providerId": "basic-flow", "topLevel": True, "builtIn": False,
         "description": "A first Google sign-in: a new account, or Google linked to an existing one once the person "
                        "has signed in to it",
         "authenticationExecutions": [
             step("REQUIRED", "idp-review-profile", config="review profile config", priority=10),
             step("REQUIRED", flow="google: new or existing account", priority=20)]},
        {"alias": "google: new or existing account", "providerId": "basic-flow", "topLevel": False, "builtIn": False,
         "description": "A new account if the email is free, otherwise prove it's yours",
         "authenticationExecutions": [
             step("ALTERNATIVE", "idp-create-user-if-unique", config="create unique user config", priority=10),
             step("ALTERNATIVE", flow="google: prove the existing account is yours", priority=20)]},
        {"alias": "google: prove the existing account is yours", "providerId": "basic-flow", "topLevel": False,
         "builtIn": False,
         "description": "Confirm, then sign in to the existing account (Microsoft); no linking on email alone",
         "authenticationExecutions": [
             step("REQUIRED", "idp-confirm-link", priority=10),
             step("REQUIRED", "idp-username-password-form", priority=20)]},
    ]


def render(template: dict, tenant: dict, access: dict) -> dict:
    if not REALM_NAME.fullmatch(tenant["realm"]):
        raise ValueError(f"realm {tenant['realm']!r}: use lowercase letters, digits and dashes, starting with a letter")
    check_addresses(tenant)
    template, access = copy.deepcopy(template), copy.deepcopy(access)
    add_copies(template, access, tenant.get("client_copies", {}))
    realm = fill(template, tenant)
    apply_access(realm, access)
    microsoft_first(realm, tenant.get("microsoft_first", False))
    add_google(realm, tenant)
    for cid, url in tenant.get("backchannel_logout", {}).items():
        client = next((c for c in realm["clients"] if c["clientId"] == cid), None)
        if client is None:
            raise ValueError(f"backchannel_logout names {cid!r}, which isn't a client in the template")
        client.setdefault("attributes", {})["backchannel.logout.url"] = url    # where Keycloak's logouts go
    leftover = TOKEN.search(json.dumps(realm))
    if leftover:
        raise ValueError(f"{leftover.group(0)} has no value")
    return realm


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenants", default=HERE / "tenants", help="folder of tenant files")
    ap.add_argument("--out", default=HERE, help="where realm-<tenant>.json files go")
    ap.add_argument("--check", action="store_true", help="only check the written files are up to date")
    args = ap.parse_args(argv)

    template, access = load(HERE / "template" / "realm.json"), load(HERE / "access.json")
    stale = []
    for tenant_file in sorted(Path(args.tenants).glob("*.json")):
        tenant = load(tenant_file)
        if tenant["realm"] != tenant_file.stem:
            sys.exit(f"{tenant_file}: 'realm' must equal the file name")
        text = json.dumps(render(template, tenant, access), indent=2, ensure_ascii=False) + "\n"
        out = Path(args.out) / f"realm-{tenant['realm']}.json"
        if args.check:
            if not out.exists() or out.read_text(encoding="utf-8") != text:
                stale.append(str(out))
        else:
            out.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {out}")
    if stale:
        sys.exit("out of date, run `python keycloak/render.py`: " + ", ".join(stale))


if __name__ == "__main__":
    main()
