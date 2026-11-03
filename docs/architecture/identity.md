# Identity and sign-in (Enterprise OS V1)

Owner: Junaid · Task: P0.4 · Status: draft, 2026-10-19

One sign-in for every Enterprise OS app. Keycloak issues the tokens; Microsoft 365 (Entra ID) checks who the person is; every service checks the token with the shared auth library (P2.3). The realm comes from `keycloak/` (a template, one file per customer, and `access.json`); keycloak-config-cli applies it to each Keycloak, and CI checks the rendered files are current. A setting changed by hand in a Keycloak console is put back by the next apply if it's in these files; things added by hand that aren't in them stay (nothing is deleted).

## How a person signs in

1. A person opens an app (app shell, Scribe, knowledge base…). The app sends them to Keycloak, realm `taraz`.
2. Keycloak sends them to Microsoft 365, using Taraz's own tenant, so only Taraz accounts get in.
3. Microsoft returns them to Keycloak at `/realms/taraz/broker/entra/endpoint`. On the first sign-in, Keycloak creates the user and stores `tenant_id=taraz` and their Microsoft IDs.
4. Keycloak gives the app a token (5-minute lifetime, refreshed in the background).
5. Opening a second app doesn't ask again: the Keycloak session is shared (single sign-on).

Keycloak never sees Microsoft passwords.

## Realms: one per customer

One realm per customer organisation, written from one template (`keycloak/README.md`). Customers can share a cluster and a Keycloak; each has its own users, keys, roles and Microsoft sign-in. Taraz is the first (realm `taraz`). Decided 2026-11-03, replacing "each organisation gets its own cluster".

### Microsoft 365 (Entra ID)

| Setting | Value |
|---|---|
| Keycloak provider alias | `entra` (OpenID Connect), shown as "Microsoft 365" |
| Entra tenant | Taraz, `9f1a1bc4-e5b7-4144-99b3-1f692eae04fd`. Every URL is tenant-specific, not `/common` |
| Entra app | "Scribe Calendar Watcher", `7b315648-cdc2-432c-93e8-2881346f886d` (reused from Scribe; multi-tenant, but the tenant-specific URLs limit sign-in to Taraz) |
| Client secret | `${KC_ENTRA_CLIENT_SECRET}`. Keycloak gets its own secret; Scribe keeps a separate one |
| Scopes | `openid profile email`, with PKCE (S256). Claims are read from the ID token (no userinfo call) |
| Logout URL | **None, on purpose.** Signing out of an app ends the Keycloak session, not the person's Microsoft session (which Outlook and Teams also use). With Microsoft's logout URL set, Keycloak sent the browser to Microsoft on sign-out and waited for it to come back; Microsoft never did (Keycloak's return address isn't registered), so the Keycloak session stayed alive and the next sign-in went straight in. Found on Scribe staging and removed 2026-10-21 |
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
| `scribe-desktop` | Public, PKCE, loopback redirect `http://127.0.0.1/*` | Reserved for the Scribe Windows app. **Not used yet:** the app signs in through Scribe's server (`scribe`), which keeps the installed app working with no update (see P2.7 below) |
| `external-agents` | Public, PKCE, **consent required** | Claude and other agents; redirects to `claude.ai` / `claude.com` `/api/mcp/auth_callback` |
| `scribe` | Confidential, sign-in + service account | Scribe web and workers |
| `knowledge-base` | Confidential, sign-in + service account | KB ingestion and console |
| `lectra`, `mcp-server`, `tenant-service`, `storage-service`, `audit-log` | Confidential, service account only | Backend services calling each other. `tenant-service`'s service account may also view and manage users and read realm roles (realm-management: view-users, query-users, manage-users, view-realm), so the admin portal can change roles |

More backend services are added as the service catalogue (P0.2) settles. The local redirect URIs (`localhost:3000`, `:5173`, `:8000`) are placeholders until each app's staging and production addresses exist.

The client scope `entra` adds the Microsoft user ID (`entra_oid`) and tenant ID (`entra_tid`) to ID and access tokens. Only `scribe` includes it (added 2026-10-21), because Scribe matches its existing users on the Microsoft ID; add it to any other app that needs to know a person's Microsoft account.

Every client includes the shared client scope `tenant` (realm default), which writes `tenant_id` into ID and access tokens. The value is fixed per realm (the realm's name), because one realm is one tenant.

### Audiences (decided 2026-11-03)

Each service accepts only tokens addressed to it (`aud`). `keycloak/access.json` lists which client may call which; it becomes each client's audiences. Clients carry only the roles mapped to them (`fullScopeAllowed` off): people's apps carry `employee`, `process_owner` and `admin`, agents never `admin`, service accounts none. tenant-service accepts member changes only from the admin portal (`app-shell`).

### Agents (decided 2026-11-03)

An agent signs in for a person through `external-agents`; its token is addressed to `mcp-server` only. `mcp-server` exchanges it (OAuth token exchange) for a token addressed to the service it calls, for the same person. Services recognise agents by the client (`azp` is `external-agents` or `mcp-server`), not by a role. The `agent` realm role is no longer used. Service-account tokens (service-to-service calls) therefore carry it too; a mapper reading a user attribute missed them. Changed 2026-10-19 for P2.3. The `tenant_id` user attribute is still stored on sign-in, for reference.

### Hardening set in the realm file (2026-11-02)

| Setting | Value | Why |
|---|---|---|
| Brute-force protection | On, 10 failures | Limits guesses at the admin one-time code |
| Login and admin events | On, with details, kept 90 days | A record of sign-ins and of who changed what |
| Refresh tokens | Single use (`revokeRefreshToken`) | A copied refresh token stops working |
| Offline sessions | 60-day maximum | They had no maximum age |
| Default role | No longer includes `account:manage-account` | Microsoft owns profile fields. People can't edit them or manage consents in the account console |
| `scribe-desktop` | Disabled | Reserved and unused. Turn it on when the app starts using it |
| `scribe` redirects | `http://localhost:8000/*` removed | To run Scribe locally against a realm, add it in that realm only |

`app-shell` and `knowledge-base` keep their localhost addresses until their real ones exist (P2.1).

**A realm import only creates,** so these don't reach a realm that already exists: set them by hand there too.

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

Each service also checks that its own client id is in `aud` (decided 2026-11-03, see Audiences).

## Secrets and import

The realm files contain no secrets and no signing keys. Keycloak generates keys and each client's secret. The one placeholder is the Microsoft secret, `$(env:KC_ENTRA_CLIENT_SECRET)` for Taraz, filled in when keycloak-config-cli applies the realm (`keycloak/README.md`).

- **Locally:** run `start-dev --import-realm` with the file in `/opt/keycloak/data/import` and those variables set. Checked 2026-10-19: a fresh Keycloak 26.8.1 loaded all 10 clients, the 4 roles, the Microsoft provider with its 4 mappers, and the admin one-time-code flow.
- **On EKS (P2.1):** use the Keycloak Operator's `KeycloakRealmImport`, with `spec.placeholders` pointing at a Kubernetes Secret in namespace `identity` (fed by External Secrets from AWS Secrets Manager).
- **Existing realms** are changed with keycloak-config-cli: the "apply realms" workflow (`keycloak/README.md`). A realm import only creates.

## Moving the existing apps onto Keycloak (P2.7)

| App | Today's sign-in | Where users are stored | Code to remove or change |
|---|---|---|---|
| Knowledge base (`tarazautomation/Knowledge-Base`) | Local username and password, custom HMAC tokens (`server/auth.py`); roles admin/user; API keys with reader/ingest/service roles | SQLite on the service's `/data` volume | Replace login and token issuing with the auth library. Map Keycloak roles and groups to the KB's access tags (row-level security, `docs/architecture/06-identity-and-acl.md`): admin → admin, employee → user. API keys become service accounts |
| KB retrieval (`tarazautomation/Knowledge-Base-Retrieval`) | No users of its own: checks a shared token and asks the KB about the caller on every request (`server/auth.py`, `server/scope.py`) | None | Check the Keycloak token locally instead of calling the KB |
| MCP server (inside Knowledge-Base, `server/mcp_remote.py`) | Custom 90-day tokens, some passed in the URL path | KB's SQLite | Keycloak OAuth through `external-agents`. The KB's existing `agent` principal kind maps to the `agent` role |
| Scribe (`Taraz-Technologies/scribe`) | Microsoft via MSAL (`saas/web/microsoft.py`, `/auth/microsoft/login` and `/callback` in `saas/web/app.py`), cookie session; operator access = `ADMIN_EMAILS` | Postgres `users`, keyed by Microsoft `oid` and email; company = Entra `tid` | **Done on branch `keycloak-sign-in` (2026-10-21), behind the switch `SIGN_IN=keycloak`.** Keycloak signs people in through `scribe` (`saas/web/keycloak.py`); users are matched on `entra_oid` (scope `entra`), then email; `entra_tid` is the company. Scribe's own cookie session stays. **Calendar access is a separate step:** the old Microsoft flow becomes "Connect Microsoft calendar" (already in onboarding), which refuses a different Microsoft account than the one signed in. **`ADMIN_EMAILS` stays as it is:** it opens every company's recordings, so it is a platform operator, not the company `admin` role (decided 2026-10-21). **Taraz accounts only** until V2 adds other companies (no one outside Taraz uses Scribe yet) |
| Lectra (`scribe` repo, `lectra/`) | Static `LECTRA_API_TOKEN` or an Entra JWT with a hard-coded tenant, client and domain (`lectra/service/auth.py`); usually reached through Scribe's proxy | `usage.json` keyed by email | Replace `lectra/service/auth.py` with the auth library and use the `lectra` service account. Set `LECTRA_REQUIRE_AUTH=1` on EKS |
| Scribe desktop (`tarazautomation/scribe-desktop`, not in the V1 plan) | Browser PKCE hand-off to Scribe web, which issues a device token (`saas/web/auth_desktop.py`, `devices.py`) | Scribe's Postgres | **No change to the app.** It only talks to Scribe's server (`/auth/desktop/start`, `/api/desktop/token`); with `SIGN_IN=keycloak` that server signs the person in through Keycloak and hands back the same device token. A new person who signs in from the app first connects their calendar on the website |

## Still to do for P0.4 and P2.1

- [x] `admin` decided in the admin portal, not by Entra groups (changed 2026-10-21; the group rule was removed).
- [x] A one-time code for admins (done 2026-10-19).
- [ ] Replace the Entra secret that was exposed in chat: new secrets for Keycloak and Scribe, then delete the old one.
- [ ] Linking Keycloak users to Odoo users, so Claude acting in Odoo is the real person (P2.5, with Subhan's MCP server).
- [x] The audience decision for P2.3 (decided 2026-11-03: each service checks).
- [ ] Add staging and production redirect URIs to the Entra app and to each client once the domains exist (P2.1).
- [x] Sending people straight to Microsoft is now set per tenant (`microsoft_first` in its tenant file). On for Taraz.

## Leavers (decided 2026-11-03)

Microsoft doesn't tell Keycloak when someone leaves. tenant-service's leavers' sync reads the company's Microsoft users through Microsoft Graph every 15 minutes and disables, and signs out, every member whose Microsoft account is disabled or deleted. With `ENTRA_SIGNIN_APP_ID` set (the sign-in app, with assignment required), it also disables members who are no longer assigned to that app, directly or through a group. Scribe's desktops, which run on Scribe's own device tokens, ask tenant-service at each refresh whether the person is still an active member (Scribe's `TENANT_SERVICE_URL`).

Each customer needs a second Entra app for it, with the Graph application permission `User.Read.All` and admin consent (tenant-service README, settings).

## Operating targets (decided 2026-11-03)

| | Railway (now) | EKS production |
|---|---|---|
| Data loss on a restore (RPO) | up to 24 hours: daily backups of Keycloak's and tenant-service's Postgres | up to 5 minutes: RDS point-in-time recovery, Multi-AZ |
| Sign-in back after an outage (RTO) | 4 hours | 1 hour |
| Restore drill | every quarter: restore into a scratch database and sign in | same |

A restore must keep Keycloak's user ids: tenant-service and the apps store them.

Keycloak security releases: **critical within 72 hours, high within 7 days, others within 30 days.** Watch the advisories on `keycloak/keycloak` (GitHub "Security alerts" for that repository). Upgrade the image in `deploy/railway`, `deploy/k8s` and the CI workflow together.

## Admin access (decided 2026-11-03)

- **One person** is the master-realm admin, with a password and authenticator app.
- **A sealed break-glass login** in the master realm: a long random password and its own authenticator, kept in AWS Secrets Manager, used only if the admin is locked out or unavailable. Using it is an incident: say so, then change its password.
- **Day-to-day realm changes** don't need a person: the "apply realms" workflow signs in as a master-realm service account (`keycloak/README.md`).
- Each customer's own admins hold the `admin` role in their realm and work in the admin portal. That role can't reach Keycloak itself.
