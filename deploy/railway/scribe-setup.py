"""Gets the Railway Keycloak ready for Scribe's Keycloak sign-in (P2.7). Run by an admin on their own PC.

    python deploy/railway/scribe-setup.py [--new-secret]

It asks for your admin-console username, password and a 6-digit authenticator code (the password and code are
hidden; nothing is saved or printed), then:
  1. adds the client scope `entra` (Microsoft user ID and tenant ID in tokens) to realm taraz, and
     to the `scribe` client's default scopes
  2. only with --new-secret: issues a new client secret for `scribe` and saves it to scribe-secret.txt (in
     SECRET_OUT_DIR, or the current folder), for Scribe's Railway variable TARAZ_AUTH_CLIENT_SECRET. The old
     secret stops working at once, so Scribe's sign-in fails until that variable is updated; delete the file after
  3. prints the scribe client's redirect addresses, so you can check Scribe's are there

Running it again is safe: the scope is created once, and the secret changes only when asked for.
If a call fails, it stops and says which steps were already done.
"""
import os
import sys

from kc_admin import save_secret, sign_in

OUT = os.path.join(os.environ.get("SECRET_OUT_DIR", os.getcwd()), "scribe-secret.txt")
A = "/admin/realms/taraz"
new_secret = "--new-secret" in sys.argv[1:]


def mapper(attr):
    return {"name": attr, "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {"user.attribute": attr, "claim.name": attr, "jsonType.label": "String",
                       "id.token.claim": "true", "access.token.claim": "true",
                       "userinfo.token.claim": "true", "introspection.token.claim": "true"}}


def entra_scope():
    return next((x for x in kc.call("GET", f"{A}/client-scopes") if x["name"] == "entra"), None)


kc = sign_in()

# 1. the entra scope, on the scribe client
scope = entra_scope()
if scope is None:
    kc.call("POST", f"{A}/client-scopes", body={
        "name": "entra", "protocol": "openid-connect",
        "description": "Adds the Microsoft user ID (entra_oid) and Microsoft tenant ID (entra_tid). "
                       "For apps that match people with their Microsoft account, like Scribe.",
        "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
        "protocolMappers": [mapper("entra_oid"), mapper("entra_tid")]})
    scope = entra_scope()
    print("created scope entra")
else:
    print("scope entra already exists")
clients = kc.call("GET", f"{A}/clients?clientId=scribe")
if not clients:
    sys.exit("realm taraz has no scribe client")
client = clients[0]
kc.call("PUT", f"{A}/clients/{client['id']}/default-client-scopes/{scope['id']}")
kc.step("scope entra is on scribe's default scopes")

# 2. fresh secret for scribe, saved locally for the Railway variable
if new_secret:
    save_secret(OUT, kc.call("POST", f"{A}/clients/{client['id']}/client-secret")["value"])
    kc.step(f"new scribe secret saved to {OUT} (not shown): update Scribe's TARAZ_AUTH_CLIENT_SECRET now")
else:
    print("scribe's secret is unchanged (--new-secret issues a new one)")

# 3. where Keycloak may send people back to
print("scribe redirect addresses:", client.get("redirectUris"))
