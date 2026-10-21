# Identity and sign-in (Enterprise OS V1)

Owner: Junaid · Task: P0.4 · Status: draft, 2026-10-19

One sign-in for every Enterprise OS app. Keycloak issues the tokens; Microsoft 365 (Entra ID) checks who the person is; every service checks the token with the shared auth library (P2.3). This page and `realm-taraz.json` are what P2.1 deploys unchanged.

## How a person signs in

1. A person opens an app (app shell, Scribe, knowledge base…). The app sends them to Keycloak, realm `taraz`.
2. Keycloak sends them to Microsoft 365, using Taraz's own tenant, so only Taraz accounts get in.
3. Microsoft returns them to Keycloak at `/realms/taraz/broker/entra/endpoint`. On the first sign-in, Keycloak creates the user and stores `tenant_id=taraz` and their Microsoft IDs.
4. Keycloak gives the app a token (5-minute lifetime, refreshed in the background).
5. Opening a second app doesn't ask again: the Keycloak session is shared (single sign-on).

Keycloak never sees Microsoft passwords.

## Realm `taraz`

One realm per customer organisation; each organisation gets its own cluster. Taraz is the first.

### Microsoft 365 (Entra ID)

| Setting | Value |
|---|---|
| Keycloak provider alias | `entra` (OpenID Connect), shown as "Microsoft 365" |
| Entra tenant | Taraz, `9f1a1bc4-e5b7-4144-99b3-1f692eae04fd`. Every URL is tenant-specific, not `/common` |
| Entra app | "Scribe Calendar Watcher", `7b315648-cdc2-432c-93e8-2881346f886d` (reused from Scribe; multi-tenant, but the tenant-specific URLs limit sign-in to Taraz) |
| Client secret | `${KC_ENTRA_CLIENT_SECRET}`. Keycloak gets its own secret; Scribe keeps a separate one |
| Scopes | `openid profile email`, with PKCE (S256). Claims are read from the ID token (no userinfo call) |
| Groups claim | Switched on for the Entra app, but **no longer used** (since 2026-10-21 admins are decided in the admin portal, not by Entra groups) |
| Redirect URIs to register on the Entra app (Web platform) | local `http://localhost:8080/realms/taraz/broker/entra/endpoint` (done) · staging `https://auth.<staging-domain>/realms/taraz/broker/entra/endpoint` · production `https://auth.<domain>/realms/taraz/broker/entra/endpoint` |

Provider mappers, applied on every sign-in:

| Mapper | Result |
|---|---|
| Hardcoded attribute | `tenant_id = taraz` |
| Claim `oid` → attribute `entra_oid` | The Microsoft user ID. Scribe matches its existing users on it (P2.7) |
| Claim `tid` → attribute `entra_tid` | The Microsoft tenant ID |

`tenant_id`, `entra_oid` and `entra_tid` are declared in the realm's user profile (admin view and edit only). Keycloak 24+ drops undeclared attributes.

### Roles

| Role | Who | How it's given |
|---|---|---|
| `employee` | Every staff member | Default role: everyone gets it on first sign-in |
| `process_owner` | Approves the ontology and processes for their area | Given and removed by an admin in the admin portal (tenant service, P2.2) |
| `admin` | Runs Enterprise OS for the company | Given and removed by an admin in the admin portal (tenant service, P2.2), which writes it to Keycloak. The tenant service won't remove the last admin. On a brand-new realm, the first admin is assigned by hand in the Keycloak admin console |
| `agent` | An AI agent (e.g. Claude) acting for one person, with only that person's access | Given to agent sessions through the `external-agents` client (P2.5) |

### One-time code for admins

The `entra` provider runs the flow `after-microsoft-sign-in` after every Microsoft sign-in:

```text
admins-one-time-code   CONDITIONAL
  Condition - user role (admin)   REQUIRED
  OTP Form                        REQUIRED
Allow access           REQUIRED
```

- **Admins** set up an authenticator app (QR code) the first time, then enter a 6-digit code on every sign-in.
- **Everyone else** passes straight through.
- The final "Allow access" step is required. Without it, a flow where every step is skipped counts as a failed sign-in, and non-admins see "Invalid username or password".

Tested on 2026-10-19: an admin had to set up and enter a code. (Admin then came from the Entra group; since 2026-10-21 it is given in the admin portal. The one-time code applies to anyone holding `admin`, however they got it.)

### Apps (clients)

| Client | Type | Used by |
|---|---|---|
| `app-shell` | Public, PKCE | The Flutter web app shell (P6.1) |
| `scribe-desktop` | Public, PKCE, loopback redirect `http://127.0.0.1/*` | The Scribe Windows app |
| `external-agents` | Public, PKCE, **consent required** | Claude and other agents; redirects to `claude.ai` / `claude.com` `/api/mcp/auth_callback` |
| `scribe` | Confidential, sign-in + service account | Scribe web and workers |
| `knowledge-base` | Confidential, sign-in + service account | KB ingestion and console |
| `lectra`, `mcp-server`, `tenant-service`, `storage-service`, `audit-log` | Confidential, service account only | Backend services calling each other. `tenant-service`'s service account may also view and manage users and read realm roles (realm-management: view-users, query-users, manage-users, view-realm), so the admin portal can change roles |

More backend services are added as the service catalogue (P0.2) settles. The local redirect URIs (`localhost:3000`, `:5173`, `:8000`) are placeholders until each app's staging and production addresses exist.

Every client includes the shared client scope `tenant` (realm default), which writes `tenant_id` into ID and access tokens. The value is fixed per realm (`taraz`), because one realm is one tenant. Service-account tokens (service-to-service calls) therefore carry it too; a mapper reading a user attribute missed them. Changed 2026-10-19 for P2.3. The `tenant_id` user attribute is still stored on sign-in, for reference.

## Token format

Example access token, as issued to `app-shell` for the local test user `test.admin`:

```json
{
  "iss": "https://auth.<domain>/realms/taraz",
  "azp": "app-shell",
  "sub": "64868e9c-7819-4b13-9620-534b141ae5f6",
  "typ": "Bearer",
  "exp": 1792391425,
  "iat": 1792391125,
  "preferred_username": "test.admin",
  "email": "test.admin@example.com",
  "name": "Test Admin",
  "tenant_id": "taraz",
  "realm_access": { "roles": ["employee", "admin", "default-roles-taraz", "offline_access", "uma_authorization"] },
  "scope": "openid profile email"
}
```

What every service checks (the P2.3 auth library does this):
1. Signature, using the realm's public keys from `/realms/taraz/protocol/openid-connect/certs`.
2. `iss` is the realm, and `exp` hasn't passed.
3. `tenant_id` matches the cluster's tenant. Refuse otherwise.
4. The roles in `realm_access.roles` allow the action.

**Open decision (P2.3):** tokens carry `aud: account` today. Decide whether each service only accepts tokens addressed to it, which means adding audience mappers per client.

## Secrets and import

`realm-taraz.json` contains no secrets and no signing keys. Keycloak generates fresh keys on import. Secrets are placeholders filled from environment variables:

`KC_ENTRA_CLIENT_SECRET`, `KC_CLIENT_SECRET_SCRIBE`, `KC_CLIENT_SECRET_KNOWLEDGE_BASE`, `KC_CLIENT_SECRET_LECTRA`, `KC_CLIENT_SECRET_MCP_SERVER`, `KC_CLIENT_SECRET_TENANT_SERVICE`, `KC_CLIENT_SECRET_STORAGE_SERVICE`, `KC_CLIENT_SECRET_AUDIT_LOG`

- **Locally:** run `start-dev --import-realm` with the file in `/opt/keycloak/data/import` and those variables set. Checked 2026-10-19: a fresh Keycloak 26.8.1 loaded all 10 clients, the 4 roles, the Microsoft provider with its 4 mappers, and the admin one-time-code flow.
- **On EKS (P2.1):** use the Keycloak Operator's `KeycloakRealmImport`, with `spec.placeholders` pointing at a Kubernetes Secret in namespace `identity` (fed by External Secrets from AWS Secrets Manager).
- **Watch out:** a realm import only creates a realm and never updates an existing one. Later changes need `keycloak-config-cli` or a re-import job.

## Moving the existing apps onto Keycloak (P2.7)

| App | Today's sign-in | Where users are stored | Code to remove or change |
|---|---|---|---|
| Knowledge base (`tarazautomation/Knowledge-Base`) | Local username and password, custom HMAC tokens (`server/auth.py`); roles admin/user; API keys with reader/ingest/service roles | SQLite on the service's `/data` volume | Replace login and token issuing with the auth library. Map Keycloak roles and groups to the KB's access tags (row-level security, `docs/architecture/06-identity-and-acl.md`): admin → admin, employee → user. API keys become service accounts |
| KB retrieval (`tarazautomation/Knowledge-Base-Retrieval`) | No users of its own: checks a shared token and asks the KB about the caller on every request (`server/auth.py`, `server/scope.py`) | None | Check the Keycloak token locally instead of calling the KB |
| MCP server (inside Knowledge-Base, `server/mcp_remote.py`) | Custom 90-day tokens, some passed in the URL path | KB's SQLite | Keycloak OAuth through `external-agents`. The KB's existing `agent` principal kind maps to the `agent` role |
| Scribe (`Taraz-Technologies/scribe`) | Microsoft via MSAL (`saas/web/microsoft.py`, `/auth/microsoft/login` and `/callback` in `saas/web/app.py`), cookie session; admin = `ADMIN_EMAILS` | Postgres `users`, keyed by Microsoft `oid` and email; company = Entra `tid` | Replace the MSAL login and session with Keycloak via `scribe`. Match users on `entra_oid`; `entra_tid` becomes the company; `ADMIN_EMAILS` become the `admin` role. **Keep** the separate calendar consent (`Calendars.Read`) the scanner needs |
| Lectra (`scribe` repo, `lectra/`) | Static `LECTRA_API_TOKEN` or an Entra JWT with a hard-coded tenant, client and domain (`lectra/service/auth.py`); usually reached through Scribe's proxy | `usage.json` keyed by email | Replace `lectra/service/auth.py` with the auth library and use the `lectra` service account. Set `LECTRA_REQUIRE_AUTH=1` on EKS |
| Scribe desktop (`tarazautomation/scribe-desktop`, not in the V1 plan) | Browser PKCE hand-off to Scribe web, which issues a device token (`saas/web/auth_desktop.py`, `devices.py`) | Scribe's Postgres | Sign in with Keycloak through `scribe-desktop`. Ship a release with the new URLs before Railway is switched off |

## Still to do for P0.4 and P2.1

- [x] `admin` decided in the admin portal, not by Entra groups (changed 2026-10-21; the group rule was removed).
- [x] A one-time code for admins (done 2026-10-19).
- [ ] Replace the Entra secret that was exposed in chat: new secrets for Keycloak and Scribe, then delete the old `y3Q…` one.
- [ ] Linking Keycloak users to Odoo users, so Claude acting in Odoo is the real person (P2.5, with Subhan's MCP server).
- [ ] The audience decision for P2.3 (above).
- [ ] Add staging and production redirect URIs to the Entra app and to each client once the domains exist (P2.1).
