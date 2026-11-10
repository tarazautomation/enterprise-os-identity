"""One-off admin changes on the Railway Keycloak, run by an admin on their own PC.

    python deploy/railway/admin-setup.py [--new-secret]

It asks for your admin-console username, password and a 6-digit authenticator code (the password and code are
hidden; nothing is saved or printed), then:
  1. removes the "Enterprise OS Admins group -> admin" rule (the admin portal decides admins now)
  2. lets the tenant-service's service account read and manage users in realm taraz
  3. only with --new-secret: issues a new client secret for tenant-service and saves it to
     tenant-service-secret.txt (in SECRET_OUT_DIR, or the current folder), for the Railway variable. The old
     secret stops working at once, so update TARAZ_AUTH_CLIENT_SECRET straight away, then delete the file.

Running it again is safe: steps 1 and 2 change nothing the second time, and step 3 runs only when asked for.
If a call fails, it stops and says which steps were already done.
"""
import os
import sys

from kc_admin import save_secret, sign_in

OUT = os.path.join(os.environ.get("SECRET_OUT_DIR", os.getcwd()), "tenant-service-secret.txt")
A = "/admin/realms/taraz"
new_secret = "--new-secret" in sys.argv[1:]

kc = sign_in()

# 1. remove group -> admin rule
mappers = kc.call("GET", f"{A}/identity-provider/instances/entra/mappers")
for m in [m for m in mappers if m["identityProviderMapper"] == "oidc-role-idp-mapper"]:
    kc.call("DELETE", f"{A}/identity-provider/instances/entra/mappers/{m['id']}")
    print("removed rule", repr(m["name"]))
kc.step("Microsoft group -> admin rule removed")

# 2. tenant-service permissions
clients = kc.call("GET", f"{A}/clients?clientId=tenant-service")
if not clients:
    sys.exit("realm taraz has no tenant-service client")
cid = clients[0]["id"]
sau = kc.call("GET", f"{A}/clients/{cid}/service-account-user")["id"]
rm = kc.call("GET", f"{A}/clients?clientId=realm-management")[0]["id"]
wanted = [r for r in kc.call("GET", f"{A}/clients/{rm}/roles")
          if r["name"] in ("view-users", "query-users", "manage-users", "view-realm")]
kc.call("POST", f"{A}/users/{sau}/role-mappings/clients/{rm}", body=wanted)
kc.step("tenant-service may read and manage users")

# 3. fresh secret for tenant-service, saved locally for the Railway variable
if new_secret:
    save_secret(OUT, kc.call("POST", f"{A}/clients/{cid}/client-secret")["value"])
    kc.step(f"new tenant-service secret saved to {OUT} (not shown): update Railway now")
else:
    print("tenant-service's secret is unchanged (--new-secret issues a new one)")

print("Microsoft mappers now:", [m["name"] for m in kc.call("GET", f"{A}/identity-provider/instances/entra/mappers")])
