# Realms: one per customer

Each customer (tenant) gets its own realm, with its own users, keys, roles and Microsoft (Entra) sign-in. All
customers can share one Keycloak and one cluster. The realms are written from one template, so they stay alike.

| File | What it is |
|---|---|
| `template/realm.json` | The realm, with `{{...}}` where customers differ. Edit this to change every customer |
| `tenants/<tenant>.json` | One customer: realm name, display name, Microsoft tenant and app, redirect addresses, the name of the variable holding its Microsoft secret |
| `access.json` | Who may call whom, and the roles each app's tokens carry. See below |
| `render.py` | Writes `realm-<tenant>.json` for each tenant file |
| `realm-<tenant>.json` | The result. Don't edit it by hand: CI fails if it doesn't match |
| `config-cli.env` | Settings for keycloak-config-cli, which applies realms to a running Keycloak |

No secret is in these files. Keycloak generates each client's secret, read it in the admin console (client →
Credentials). The Microsoft secret is written as `$(env:KC_...)` and filled in when the realm is applied.

## Who may call whom (`access.json`)

A service accepts a token only if the token lists the service's client id in its audience (`aud`). `calls`
decides which audiences each client's tokens carry, so it is the allow-list:

- To let X call Y, add `Y` to `calls.X`, render, and apply.
- A client that checks its own sign-in tokens (Scribe, the knowledge base) lists itself.
- `user_roles` lists the roles a person's token carries in that app. Agents (`external-agents`, `mcp-server`)
  never carry `admin`. Service accounts carry none.
- `token_exchange`: `mcp-server` may exchange an agent's token for one addressed to a service it calls. The new
  token is for the same person, with `azp=mcp-server`.

The first version of `calls` was written from what each app is known to do. **Check it against the apps' real
calls** before each service turns its audience check on.

## Adding a customer

1. Register an Entra app in the customer's Microsoft tenant (Web redirect URI
   `https://auth.<domain>/realms/<tenant>/broker/entra/endpoint`, scopes `openid profile email`), and turn on
   "assignment required" so only assigned people get in.
2. Copy `tenants/taraz.json` to `tenants/<tenant>.json` and fill it in.
3. `python keycloak/render.py`, commit, and open a pull request.
4. After merging, add the Microsoft secret to the GitHub environment under the name in `secrets.entra`, and run
   **Actions → apply realms** with the tenant's name.
5. Give the customer's first admin the `admin` role in the new realm's admin console.

## Applying

The template leaves out the few realm settings added after Keycloak 26.5 (`maxSecondaryAuthFailures`, two WebAuthn resident-key settings, `scimApiEnabled` and the Microsoft provider's `types`). keycloak-config-cli 6.5.1 is built on 26.5 and refuses them. They were at their defaults, so Keycloak's defaults apply. Add them back when a keycloak-config-cli release supports 26.8.

`keycloak-config-cli` brings a live realm in line with its file. It creates the realm if it is missing, and it
never deletes anything (`config-cli.env`): to remove a client, role or mapper, delete it by hand too.

The **apply realms** workflow (`.github/workflows/apply-realms.yml`) runs it for one tenant, by hand. It needs the
GitHub environment `keycloak-production` with:

- `KEYCLOAK_URL`
- `KEYCLOAK_CLIENT_ID` and `KEYCLOAK_CLIENT_SECRET`: a confidential client in the **master** realm, with service
  accounts on and the master realm role `admin` (or `create-realm` plus `realm-admin` on each tenant's realm).
  It replaces people signing in to make realm changes
- each tenant's Microsoft secret, under the name in its tenant file

Protect that environment with a required reviewer.

**The first apply to an existing realm** also changes what's in tokens: audiences appear, and apps get only
their mapped roles. Apply before services turn their audience check on, and check each app afterwards.

## Checked in CI

The `keycloak` job starts Keycloak, applies the Taraz realm and two test tenants twice, and tests real tokens
(`tests/integration`). Microsoft sign-in itself can't run in CI: check it in the lab or on staging.
