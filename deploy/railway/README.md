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

Only the master-realm admin `junaid.asrar`, with a password and authenticator app. The bootstrap `temp-admin` was deleted.

## Moving off Railway

1. Copy the Postgres database to the new home: same Keycloak version, so IDs are kept.
2. Change `KC_HOSTNAME` (or point DNS at the new home), and add the new redirect URI on the Entra app.
3. Update each app's `TARAZ_AUTH_ISSUER` if the address changed. Everyone signs in once more.

## One-off admin changes: `admin-setup.py`

An admin runs `python deploy/railway/admin-setup.py` on their own PC. It asks for the admin-console username, password and authenticator code (hidden, never saved), then:
1. removes the "Enterprise OS Admins group -> admin" rule (since 2026-10-21 the admin portal decides who is admin)
2. gives `tenant-service`'s service account view-users, query-users, manage-users and view-realm in realm `taraz`
3. issues a new `tenant-service` client secret and saves it to `tenant-service-secret.txt` for the Railway variable. Set `SECRET_OUT_DIR` to keep it outside the repo, and delete it afterwards.

## Tenant service (P2.2)

Service `tenant-service` in the same project, built from this repo (`services/tenant-service/Dockerfile`, build context = repo root), with its own database `Postgres-XjsC` (database `tenants`).

| Setting | Value |
|---|---|
| Address | https://taraz-tenants.up.railway.app (API description at `/openapi.json`) |
| Pre-deploy command | `alembic upgrade head` (migrations run before each deploy, not at start-up) |
| Healthcheck | `/healthz` |
| Watch paths | `services/tenant-service/**`, `libs/taraz-auth/**` |
| Variables | `RAILWAY_DOCKERFILE_PATH`, `DATABASE_URL` (reference to `Postgres-XjsC`), `TARAZ_AUTH_ISSUER`, `TARAZ_AUTH_TENANT_ID`, `TENANT_NAME`, `TARAZ_AUTH_CLIENT_ID` (`tenant-service`), `TARAZ_AUTH_CLIENT_SECRET` (issued by `admin-setup.py`), `SYNC_INTERVAL_SECONDS`, `PORT` |

First deploy (2026-10-21): the migration ran, and the first sync found 1 user.
