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
| `GET /v1/members?q=&role=&app=&status=` · `GET /v1/members/{id}` | any member, and other services |
| `GET /v1/roles` · `GET /v1/apps` · `GET /v1/tenant` | any member, and other services |
| `PUT /v1/members/{id}/roles` (admin, process_owner) | admins |
| `PUT /v1/members/{id}/apps` | admins |
| `POST /v1/members/{id}/disable` · `/enable` | admins |
| `GET /v1/members/{id}/events` | admins |
| `POST/GET /v1/invites` · `DELETE /v1/invites/{id}` | admins (stored now; the email flow comes in V2) |
| `GET /v1/agents` | admins (filled by P2.5) |
| `GET /v1/status` · `POST /v1/sync` | admins |
| `GET /healthz` | anyone (no token) |

Rules:
- **Only `admin` and `process_owner` can be set.** Everyone gets `employee` through the realm's default role, and `agent` is for AI agents.
- **The last admin can't be removed or disabled,** and nobody can disable themselves.
- **Disabling someone also ends their sessions.**
- **Every change is recorded** in the member's history, with who made it.

Answers to bad requests: 401 (no, invalid, expired or wrong-tenant token), 403 (missing role), 404, 409 (last admin, self, duplicate invite), 422 (unknown role or app), 502 (Keycloak error).

## Settings

| Variable | Example |
|---|---|
| `TARAZ_AUTH_ISSUER` | `https://taraz-auth.up.railway.app/realms/taraz` |
| `TARAZ_AUTH_TENANT_ID` | `taraz` |
| `TENANT_NAME` | `Taraz Technologies` |
| `TARAZ_AUTH_CLIENT_ID` / `TARAZ_AUTH_CLIENT_SECRET` | `tenant-service` / (from Keycloak; the client's service account needs realm-management view-users, query-users, manage-users, view-realm) |
| `DATABASE_URL` | Postgres URL (`postgresql://…` is fine) |
| `SYNC_INTERVAL_SECONDS` | `30` |
| `TARAZ_AUTH_JWKS_URL` | optional internal address of Keycloak's public keys |
| `KEYCLOAK_INTERNAL_URL` | optional: `http://keycloak-service.identity.svc.cluster.local:8080`, the base URL for admin API and token calls when the issuer's address doesn't work from inside the service (in a pod, `localhost` is the pod). Defaults to the issuer's base. The issuer stays the public value that tokens are checked against |
| `TARAZ_AUTH_TOKEN_URL` | optional: where this service gets its own token. Defaults to `<KEYCLOAK_INTERNAL_URL or issuer base>/realms/<realm>/protocol/openid-connect/token` |

## Run, test, migrate

```bash
pip install -e ../../libs/taraz-auth[fastapi] -e ".[test]"
pytest                                  # 19 tests, no network
alembic upgrade head                    # needs DATABASE_URL
uvicorn tenant_service.main:app --port 8090
python -m tenant_service.export_openapi openapi.json
docker build -f services/tenant-service/Dockerfile -t tenant-service ../..   # from the repo root
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
