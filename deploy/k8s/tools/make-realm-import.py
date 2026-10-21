"""Wrap realm-taraz.json in a KeycloakRealmImport for the Keycloak Operator.

Every ${KC_...} placeholder in the realm file is mapped to the same-named key of the
Secret "keycloak-realm-secrets" in the Keycloak namespace. Run again whenever the realm
file changes:

    python make-realm-import.py <realm-taraz.json> <output realm-import.json>

Note: a realm import only CREATES a realm. If the realm already exists, it is left as is.
"""
import json
import re
import sys

SECRET_NAME = "keycloak-realm-secrets"


def main(src: str, dst: str) -> None:
    text = open(src, encoding="utf-8").read()
    realm = json.loads(text)
    placeholders = sorted(set(re.findall(r"\$\{(KC_[A-Z0-9_]+)\}", text)))
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
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(cr, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {dst}: realm '{realm['realm']}', {len(placeholders)} placeholders from Secret {SECRET_NAME}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
