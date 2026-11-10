"""Wrap a rendered realm (keycloak/realm-<tenant>.json) in a KeycloakRealmImport for the Keycloak Operator.

The rendered realm holds $(env:KC_...) placeholders for keycloak-config-cli. Here each one becomes the operator's
${KC_...} placeholder, mapped to the same-named key of the Secret "keycloak-realm-secrets" in the Keycloak
namespace. Run again whenever the realm changes (after `python keycloak/render.py`):

    python make-realm-import.py <keycloak/realm-taraz.json> <output realm-import.json> [--lab]

--lab adds the localhost addresses the lab's apps use through port-forwards (LAB_REDIRECTS). The production realm
has none, so a sign-in can't be sent to a program listening on someone's machine. It also leaves out Google sign-in:
Google returns people only to the addresses registered with it, and the lab's isn't one.

Note: a realm import only CREATES a realm. If the realm already exists, it is left as is. Change existing realms
with keycloak-config-cli (keycloak/README.md).
"""
import json
import re
import sys

SECRET_NAME = "keycloak-realm-secrets"
LAB_REDIRECTS = {
    "app-shell": ["http://localhost:3000/*", "http://localhost:5173/*"],
    "knowledge-base": ["http://localhost:8000/*"],
}
PLACEHOLDER = re.compile(r"\$\(env:(KC_[A-Z0-9_]+)\)")


def main(src: str, dst: str, lab: bool = False) -> None:
    realm = json.loads(open(src, encoding="utf-8").read())
    if lab:
        for client in realm["clients"]:
            extra = LAB_REDIRECTS.get(client["clientId"], [])
            client["redirectUris"] = client.get("redirectUris", []) + [u for u in extra
                                                                      if u not in client.get("redirectUris", [])]
        realm["identityProviders"] = [i for i in realm["identityProviders"] if i["alias"] != "google"]
        realm["identityProviderMappers"] = [m for m in realm["identityProviderMappers"]
                                            if m["identityProviderAlias"] != "google"]
    text = json.dumps(realm)
    placeholders = sorted(set(PLACEHOLDER.findall(text)))
    realm = json.loads(PLACEHOLDER.sub(r"${\1}", text))
    cr = {
        "apiVersion": "k8s.keycloak.org/v2beta1",
        "kind": "KeycloakRealmImport",
        "metadata": {"name": f"{realm['realm']}-realm", "namespace": "identity"},
        "spec": {
            "keycloakCRName": "keycloak",
            "placeholders": {p: {"secret": {"name": SECRET_NAME, "key": p}} for p in placeholders},
            "realm": realm,
        },
    }
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cr, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {dst}: realm '{realm['realm']}', {len(placeholders)} placeholders from Secret {SECRET_NAME}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--lab"]
    main(args[0], args[1], lab="--lab" in sys.argv[1:])
