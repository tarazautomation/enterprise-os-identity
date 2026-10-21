"""One-off admin changes on the Railway Keycloak, run by an admin on their own PC.

    python deploy/railway/admin-setup.py

It asks for your admin-console username, password and a 6-digit authenticator code (hidden; nothing
is saved or printed), then:
  1. removes the "Enterprise OS Admins group -> admin" rule (the admin portal decides admins now)
  2. lets the tenant-service's service account read and manage users in realm taraz
  3. issues a new client secret for tenant-service and saves it to tenant-service-secret.txt
     next to this script's working copy, for the Railway variable (delete the file afterwards)
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KEYCLOAK_URL", "https://taraz-auth.up.railway.app")
OUT = os.path.join(os.environ.get("SECRET_OUT_DIR", os.getcwd()), "tenant-service-secret.txt")


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

# 1. remove group -> admin rule
s, mappers = call("GET", f"{A}/identity-provider/instances/entra/mappers", tok)
for m in [m for m in mappers if m["identityProviderMapper"] == "oidc-role-idp-mapper"]:
    print("removed rule", repr(m["name"]), "->", call("DELETE", f"{A}/identity-provider/instances/entra/mappers/{m['id']}", tok)[0])

# 2. tenant-service permissions
cid = call("GET", f"{A}/clients?clientId=tenant-service", tok)[1][0]["id"]
sau = call("GET", f"{A}/clients/{cid}/service-account-user", tok)[1]["id"]
rm = call("GET", f"{A}/clients?clientId=realm-management", tok)[1][0]["id"]
wanted = [r for r in call("GET", f"{A}/clients/{rm}/roles", tok)[1] if r["name"] in ("view-users", "query-users", "manage-users", "view-realm")]
print("tenant-service permissions ->", call("POST", f"{A}/users/{sau}/role-mappings/clients/{rm}", tok, body=wanted)[0])

# 3. fresh secret for tenant-service, saved locally for the Railway variable
s, sec = call("POST", f"{A}/clients/{cid}/client-secret", tok)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(sec["value"])
print("new tenant-service secret saved to", OUT, "(not shown)")

s, mappers = call("GET", f"{A}/identity-provider/instances/entra/mappers", tok)
print("Microsoft mappers now:", [m["name"] for m in mappers])
