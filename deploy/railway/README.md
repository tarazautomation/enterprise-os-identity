# Keycloak on Railway (interim, until EKS)

Set up on 2026-10-21. Railway workspace "Taraz Technologies", project **Enterprise OS - identity**, environment `production`.

| Service | Source | Notes |
|---|---|---|
| `Postgres` | Railway Postgres template | Database `keycloak` |
| `keycloak` | image `quay.io/keycloak/keycloak:26.8.1` | Start command `/opt/keycloak/bin/kc.sh start`; 1 replica; 2 vCPU / 2 GB; healthcheck `/realms/master` |

Public address: **https://taraz-auth.up.railway.app** (Railway domain, target port 8080).

## Variables on `keycloak` (names only)

| Variable | Value |
|---|---|
| `KC_DB` | `postgres` |
| `KC_DB_URL` | `jdbc:postgresql://${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}` |
| `KC_DB_USERNAME` / `KC_DB_PASSWORD` | references to `Postgres.PGUSER` / `Postgres.PGPASSWORD` |
| `KC_HOSTNAME` | `https://taraz-auth.up.railway.app` |
| `KC_HTTP_ENABLED` / `KC_HTTP_PORT` / `PORT` | `true` / `8080` / `8080` (TLS ends at Railway's edge) |
| `KC_PROXY_HEADERS` | `xforwarded` |
| `KC_HEALTH_ENABLED` | `true` |
| `KC_CACHE` | `local` (one instance; nothing is lost on restart, because sessions are kept in the database) |

The `KC_BOOTSTRAP_ADMIN_*` variables were only for the first start, and are to be deleted.

## How the realm got there

The realm was created through the admin API from `keycloak/realm-taraz.json`:
- **Client secrets** were left out, so Keycloak generated its own. Read one in the admin console: client → Credentials.
- **The Microsoft client secret** was pasted in by hand: Identity providers → Microsoft 365.
- **Microsoft first:** the `taraz` browser flow's Identity Provider Redirector sends people straight to `entra`.

On the Entra app ("Scribe Calendar Watcher", `7b315648-…`), the Web redirect URI `https://taraz-auth.up.railway.app/realms/taraz/broker/entra/endpoint` is registered.

## Admin access

One master-realm admin, with a password and authenticator app, plus a sealed break-glass login (`docs/architecture/identity.md`, "Admin access"). The bootstrap `temp-admin` was deleted.

**Delete `KC_BOOTSTRAP_ADMIN_USERNAME` and `KC_BOOTSTRAP_ADMIN_PASSWORD` from the `keycloak` service.** They were only for the first start.

### Making the break-glass login

1. In the master realm, add a user (for example `break-glass`), set a 40-character random password, and require an authenticator (Required user actions: Configure OTP).
2. Sign in once in a private window, set up its authenticator, and give it the master realm role `admin`.
3. Store the password and the authenticator's secret (or a recovery code) in AWS Secrets Manager, readable only by the people allowed to use it.
4. Turn on brute-force protection and login events in the master realm too.

### The service account for keycloak-config-cli

In the master realm, add a confidential client `keycloak-config-cli` with only "Service accounts" on, give its service account the master realm role `admin`, and put its id and secret in the GitHub environment `keycloak-production` (`keycloak/README.md`).

## Rolling out audiences (PR "A realm per customer")

The realm changes put audiences and per-app roles in tokens. tenant-service then refuses tokens without its audience. Do these in order:

1. Set `TARAZ_AUTH_SKIP_AUDIENCE_CHECK=true` on `tenant-service`.
2. Merge. Railway deploys tenant-service with the new library.
3. Run **apply realms** for `taraz`. It needs `KC_ENTRA_CLIENT_SECRET` in the GitHub environment.
4. Check a token from each app has the audiences in `keycloak/access.json`, then remove `TARAZ_AUTH_SKIP_AUDIENCE_CHECK`.

## Moving off Railway

1. Copy the Postgres database to the new home: same Keycloak version, so IDs are kept.
2. Change `KC_HOSTNAME` (or point DNS at the new home), and add the new redirect URI on the Entra app.
3. Update each app's `TARAZ_AUTH_ISSUER` if the address changed. Everyone signs in once more.

## One-off admin changes: `admin-setup.py`

An admin runs `python deploy/railway/admin-setup.py` on their own PC. It asks for the admin-console username, password and authenticator code (the password and code are hidden; nothing is saved), then:
1. removes the "Enterprise OS Admins group -> admin" rule (since 2026-10-21 the admin portal decides who is admin)
2. gives `tenant-service`'s service account view-users, query-users, manage-users and view-realm in realm `taraz`
3. only with `--new-secret`: issues a new `tenant-service` client secret and saves it to `tenant-service-secret.txt` for the Railway variable. The old secret stops working at once, so update the variable straight away. Set `SECRET_OUT_DIR` to keep the file outside the repo, and delete it afterwards.

Running it again is safe: steps 1 and 2 change nothing the second time. If a call fails, it stops and lists the steps already done. `scribe-setup.py` works the same way; both share `kc_admin.py`.

## Tenant service (P2.2)

Service `tenant-service` in the same project, built from this repo (`services/tenant-service/Dockerfile`, build context = repo root), with its own database `Postgres-XjsC` (database `tenants`).

| Setting | Value |
|---|---|
| Address | https://taraz-tenants.up.railway.app (API description at `/openapi.json`) |
| Pre-deploy command | `alembic upgrade head` (migrations run before each deploy, not at start-up) |
| Healthcheck | `/healthz` |
| Watch paths | `services/tenant-service/**`, `libs/taraz-auth/**` |
| Variables | `RAILWAY_DOCKERFILE_PATH`, `DATABASE_URL` (reference to `Postgres-XjsC`), `TARAZ_AUTH_ISSUER`, `TARAZ_AUTH_TENANT_ID`, `TENANT_NAME`, `TARAZ_AUTH_CLIENT_ID` (`tenant-service`), `TARAZ_AUTH_CLIENT_SECRET` (issued by `admin-setup.py --new-secret`), `SYNC_INTERVAL_SECONDS`, `PORT` |

First deploy (2026-10-21): the migration ran, and the first sync found 1 user.

## Scribe's Keycloak sign-in (P2.7)

Before Scribe staging or production is switched to `SIGN_IN=keycloak`, an admin runs `deploy/railway/scribe-setup.py` once on their own PC (it asks for the admin password and one-time code). It adds the `entra` client scope (Microsoft user ID and tenant ID in Scribe's tokens). With `--new-secret` it also writes a fresh `scribe` client secret to `scribe-secret.txt`; Scribe's sign-in then fails until `TARAZ_AUTH_CLIENT_SECRET` is updated.

Scribe's variables (project "Scribe Minio", service `scribe`; staging deploys from branch `staging`, production from `main`):

| Variable | Value |
|---|---|
| `SIGN_IN` | `keycloak` (remove it, or set `microsoft`, to go back to the old sign-in) |
| `TARAZ_AUTH_ISSUER` | `https://taraz-auth.up.railway.app/realms/taraz` |
| `TARAZ_AUTH_CLIENT_ID` | production: `scribe` (the default). Staging: `scribe-staging` |
| `TARAZ_AUTH_CLIENT_SECRET` | that client's secret (Keycloak admin console > Clients > the client > Credentials) |
| `TENANT_SERVICE_URL` | tenant-service's address, so a leaver's desktop is stopped at its next refresh |
| `ALLOWED_TENANT_IDS` | Taraz's Microsoft tenant id (`9f1a1bc4-e5b7-4144-99b3-1f692eae04fd`), and those of other companies that may use Scribe |

Production and staging are separate Keycloak clients, `scribe` and `scribe-staging` (`keycloak/tenants/taraz.json`), because a client has one back-channel logout address: each tells its own Scribe when someone signs out. `scribe` allows `https://web-production-ca083.up.railway.app/*`, and `scribe-staging` allows `https://scribe-staging.up.railway.app/*`.

Before switching production, run `python scripts/keycloak_cutover_check.py` in Scribe's production shell. It lists accounts without a Microsoft id, prints the operator's Microsoft id for `ADMIN_MICROSOFT_IDS`, and checks the variables above.
