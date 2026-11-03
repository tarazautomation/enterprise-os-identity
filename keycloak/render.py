"""Render each customer's realm from the shared template. One realm per customer (tenant).

    python keycloak/render.py                         write keycloak/realm-<tenant>.json for each keycloak/tenants/<tenant>.json
    python keycloak/render.py --check                 fail if a written file is out of date (CI runs this)
    python keycloak/render.py --tenants DIR --out DIR other tenant files, e.g. the CI test tenants

Inputs:
  template/realm.json  the realm, with {{...}} where customers differ
  access.json          which app may call which (token audiences), which user roles each app's tokens carry,
                       which clients act for agents, and the admin API rights of tenant-service
  tenants/<id>.json    one customer: realm name, its Microsoft (Entra) tenant and app, redirect addresses,
                       and the name of the environment variable holding its Microsoft secret

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


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
            name = tenant["secrets"][key.split(":", 1)[1]]
            if not SECRET_ENV.fullmatch(name):
                raise ValueError(f"secret variable {name!r} must look like KC_NAME")
            return f"$(env:{name})"
        value = tenant
        for part in key.split("."):
            value = value[part]
        if not isinstance(value, str):
            raise ValueError(f"{{{{{key}}}}} must be text")
        return value

    return TOKEN.sub(one, node)


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


def render(template: dict, tenant: dict, access: dict) -> dict:
    if not REALM_NAME.fullmatch(tenant["realm"]):
        raise ValueError(f"realm {tenant['realm']!r}: use lowercase letters, digits and dashes, starting with a letter")
    realm = fill(copy.deepcopy(template), tenant)
    apply_access(realm, access)
    microsoft_first(realm, tenant.get("microsoft_first", False))
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
