# Tenant service (P2.2)

The **member directory** of Enterprise OS and the **back end of the admin portal**, for one tenant (company).

- **Keycloak** is the master copy for accounts and roles. This service mirrors them, and syncs every 30 seconds.
- **Admins change roles here.** The change is written to Keycloak, so it's in the member's next token.
- **This service alone keeps** each member's app access, agents (who acts for whom), invites, and a change history.

## API (v1)

Full description: `openapi.json` (generated from the code; offered as the tenant service's part of P0.3).

| Call | Who |
|---|---|
| `GET /v1/me` | any member |
| `GET /v1/members?q=` · `GET /v1/members/{id}` | any member, and other services. Names and emails only, except for admins and services |
| `GET /v1/members?role=&status=` | admins and services |
| `GET /v1/roles` · `GET /v1/tenant` | any member, and other services |
| `PUT /v1/members/{id}/roles` (admin, process_owner) | admins |
| `POST /v1/members/{id}/disable` · `/enable` | admins |
| `GET /v1/members/{id}/events` | admins |
| `POST/GET /v1/invites` · `DELETE /v1/invites/{id}` | admins (stored now; the email flow comes in V2) |
| `GET /v1/agents` | admins (filled by P2.5) |
| `GET /v1/status` · `POST /v1/sync` (`?allow_removals=true` when many members really left) | admins |
| `GET /healthz` | anyone (no token) |
| `/docs` · `/redoc` · `/openapi.json` | only with `DOCS_ENABLED=true`; off by default |

Rules:
- **Tokens must be addressed to `tenant-service`** (its audience), and **changes come only from the admin portal**: an admin's token from another app (`ADMIN_CLIENTS`, default `app-shell`) is refused.
- **App access isn't offered yet.** It's kept in the database, but apps don't enforce it, so the API doesn't show it until they do.
- **Only `admin` and `process_owner` can be set.** Everyone gets `employee` through the realm's default role, and `agent` is for AI agents.
- **The last admin can't be removed or disabled,** and nobody can disable themselves.
- **Disabling someone also ends their sessions.** So does making someone admin: they sign in again, and the realm
  asks for their one-time code before their tokens carry `admin`.
- **Leavers are turned off automatically** when the leavers' sync is configured: every 15 minutes it reads the company's Microsoft users and disables, and signs out, every member whose Microsoft account is disabled or deleted, every guest from another organisation (unless `ENTRA_ALLOW_GUESTS=true`), and with `ENTRA_SIGNIN_APP_ID` every member no longer assigned to the sign-in app. Members who sign in with Google only aren't in Microsoft, so it leaves them alone: an admin turns them off. It never turns anyone back on, and changes nothing if more than `ENTRA_SYNC_MAX_DISABLE` would go at once (`tenant_service/entra.py`).
- **Every change is recorded** in the member's history, with who made it. The event is saved before Keycloak is changed and then marked `done` or `failed`, so a change that fails half way still shows who tried what. Invites are recorded under `invite:<invite id>`.
- **A sync never empties the directory.** A Keycloak 404 on a listing is an error, not "nobody", and a sync that would remove more than `SYNC_MAX_REMOVE` members, or every member, changes nothing and shows why in `/v1/status`. If they really left, an admin runs `POST /v1/sync?allow_removals=true`.
- **Several replicas can run.** Changes that touch admins take a Postgres advisory lock, so the last-admin rule holds when two admins act at once, and each sync runs on one replica at a time.

Answers to bad requests: 401 (no, invalid, expired or wrong-tenant token), 403 (missing role), 404, 409 (last admin, self, duplicate invite, too many removals), 422 (unknown role or app), 502 (Keycloak failed or couldn't be reached; the detail goes to the log, not to the caller).

## Settings

| Variable | Example |
|---|---|
| `TARAZ_AUTH_ISSUER` | `https://taraz-auth.up.railway.app/realms/taraz` |
| `TARAZ_AUTH_TENANT_ID` | `taraz` |
| `TENANT_NAME` | `Taraz Technologies` |
| `TARAZ_AUTH_CLIENT_ID` / `TARAZ_AUTH_CLIENT_SECRET` | `tenant-service` / (from Keycloak; the client's service account needs realm-management view-users, query-users, manage-users, view-realm) |
| `DATABASE_URL` | Postgres URL (`postgresql://…` is fine) |
| `SYNC_INTERVAL_SECONDS` | `30` |
| `SYNC_MAX_REMOVE` | optional: `5`. More members missing from Keycloak than this in one sync: change nothing |
| `DOCS_ENABLED` | optional: `true` to serve `/docs`, `/redoc` and `/openapi.json`. Off by default; the API is described in `openapi.json` here |
| `FORWARDED_ALLOW_IPS` | optional: the proxy addresses whose `X-Forwarded-*` headers are trusted. Default `*`, since Railway's edge has no fixed range; set the ingress range on Kubernetes |
| `TARAZ_AUTH_JWKS_URL` | optional internal address of Keycloak's public keys |
| `KEYCLOAK_INTERNAL_URL` | optional: `http://keycloak-service.identity.svc.cluster.local:8080`, the base URL for admin API and token calls when the issuer's address doesn't work from inside the service (in a pod, `localhost` is the pod). Defaults to the issuer's base. The issuer stays the public value that tokens are checked against |
| `TARAZ_AUTH_TOKEN_URL` | optional: where this service gets its own token. Defaults to `<KEYCLOAK_INTERNAL_URL or issuer base>/realms/<realm>/protocol/openid-connect/token` |
| `ADMIN_CLIENTS` | optional: the apps admins may change members from. Default `app-shell` |
| `ENTRA_TENANT_ID` · `ENTRA_SYNC_CLIENT_ID` · `ENTRA_SYNC_CLIENT_SECRET` | the leavers' sync: the customer's Microsoft tenant, and a sync app there with the Graph application permission `User.Read.All` (admin consent). Leave unset to turn the sync off |
| `ENTRA_ALLOW_GUESTS` | optional: `true` keeps the Microsoft tenant's guests (userType Guest) enabled. By default the leavers' sync disables them |
| `ENTRA_SIGNIN_APP_ID` | optional: the client id of the Entra app people sign in through (assignment required). Members no longer assigned to it are disabled. The sync app then also needs Graph `Application.Read.All` and `GroupMember.Read.All` |
| `ENTRA_SYNC_INTERVAL_SECONDS` · `ENTRA_SYNC_MAX_DISABLE` | optional: `900` and `5` |
| `TARAZ_AUTH_SKIP_AUDIENCE_CHECK` | `true` only until the realm has been applied with audiences (`keycloak/README.md`) |

One tenant-service runs per customer, with that customer's issuer and tenant.

## Run, test, migrate

The image installs `requirements.lock` (every package pinned by hash, written by `lock.py`) on a base image pinned by digest. Dependabot proposes updates to both; CI builds the image and audits the lock.

```bash
pip install -e ../../libs/taraz-auth[fastapi] -e ".[test]"
pytest                                  # no network; with TEST_POSTGRES_URL set, also the lock tests on Postgres
alembic upgrade head                    # needs DATABASE_URL
uvicorn tenant_service.main:app --port 8090
python -m tenant_service.export_openapi openapi.json
docker build -f services/tenant-service/Dockerfile -t tenant-service ../..   # from the repo root
python lock.py                          # on Linux: rewrite requirements.lock after changing dependencies
```

## Checked on 2026-10-21

- **16 unit tests pass** (in-memory database, fake Keycloak, real signed tokens).
- **Live, against the local k3d Keycloak:** the real service, with its database migrated by Alembic and the 30-second sync.

| Check | Result |
|---|---|
| **P2.2 done-when 1:** a user created in Keycloak shows up in the directory within 1 minute | ✅ after 29 s |
| **P2.2 done-when 3:** removing a role through the API removes it from the next token | ✅ added, then removed `process_owner`; the next tokens matched |
| **P2.2 done-when 2:** the API matches its description | ✅ `openapi.json` is generated from the code |
| An employee changing roles | refused, 403 |
| No token | refused, 401 |
| Disable / enable | ✅ written to Keycloak; sessions ended |
| Change history | ✅ joined → disabled → enabled, with who did it |
