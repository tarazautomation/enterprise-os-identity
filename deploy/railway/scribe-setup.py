"""Gets the Railway Keycloak ready for Scribe's Keycloak sign-in (P2.7). Run by an admin on their own PC.

    python deploy/railway/scribe-setup.py

It asks for your admin-console username, password and a 6-digit authenticator code (hidden; nothing
is saved or printed), then:
  1. adds the client scope `entra` (Microsoft user ID and tenant ID in tokens) to realm taraz, and
     to the `scribe` client's default scopes
  2. issues a new client secret for `scribe` and saves it to scribe-secret.txt in the current
     folder, for Scribe's Railway variable TARAZ_AUTH_CLIENT_SECRET (delete the file afterwards)
  3. prints the scribe client's redirect addresses, so you can check Scribe's are there
Running it again is safe: the scope is only created once. Step 2 replaces the secret each time.
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KEYCLOAK_URL", "https://taraz-auth.up.railway.app")
OUT = os.path.join(os.environ.get("SECRET_OUT_DIR", os.getcwd()), "scribe-secret.txt")


def call(method, path, token=None, body=None, form=None):
    headers, data = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def mapper(attr):
    return {"name": attr, "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {"user.attribute": attr, "claim.name": attr, "jsonType.label": "String",
                       "id.token.claim": "true", "access.token.claim": "true",
                       "userinfo.token.claim": "true", "introspection.token.claim": "true"}}


user = input("Admin console username [junaid.asrar]: ").strip() or "junaid.asrar"
pw = getpass.getpass("Password (hidden): ")
code = input("6-digit code from your authenticator app: ").strip()
s, b = call("POST", "/realms/master/protocol/openid-connect/token",
            form={"grant_type": "password", "client_id": "admin-cli", "username": user, "password": pw, "totp": code})
del pw
if s != 200:
    sys.exit(f"Sign-in failed (HTTP {s}). Check the password and use a fresh code. {b}")
tok = b["access_token"]
A = "/admin/realms/taraz"

# 1. the entra scope, on the scribe client
scope = next((x for x in call("GET", f"{A}/client-scopes", tok)[1] if x["name"] == "entra"), None)
if scope is None:
    s, _ = call("POST", f"{A}/client-scopes", tok, body={
        "name": "entra", "protocol": "openid-connect",
        "description": "Adds the Microsoft user ID (entra_oid) and Microsoft tenant ID (entra_tid). "
                       "For apps that match people with their Microsoft account, like Scribe.",
        "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
        "protocolMappers": [mapper("entra_oid"), mapper("entra_tid")]})
    print("created scope entra ->", s)
    scope = next(x for x in call("GET", f"{A}/client-scopes", tok)[1] if x["name"] == "entra")
else:
    print("scope entra already exists")
client = call("GET", f"{A}/clients?clientId=scribe", tok)[1][0]
print("entra added to scribe's default scopes ->",
      call("PUT", f"{A}/clients/{client['id']}/default-client-scopes/{scope['id']}", tok)[0])

# 2. fresh secret for scribe, saved locally for the Railway variable
s, sec = call("POST", f"{A}/clients/{client['id']}/client-secret", tok)
if s != 200:
    sys.exit(f"Could not issue a new scribe secret (HTTP {s}). {sec}")
with open(OUT, "w", encoding="utf-8") as f:
    f.write(sec["value"])
print("new scribe secret saved to", OUT, "(not shown)")

# 3. where Keycloak may send people back to
print("scribe redirect addresses:", client.get("redirectUris"))
