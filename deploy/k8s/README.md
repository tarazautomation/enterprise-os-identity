# Keycloak on Kubernetes (P2.1)

Owner: Junaid · Status: proven on a local k3d cluster on 2026-10-19; staging and production wait for the EKS cluster (P1.1, P1.2, P1.4).

Keycloak for Enterprise OS runs in namespace `identity`, managed by the official Keycloak Operator:
- 2 copies of Keycloak, with users and sessions stored in Postgres
- realm `taraz` loaded from `realm-taraz.json` (P0.4) by the operator's realm import
- Microsoft 365 sign-in for Taraz staff; admins must enter a one-time code

The design (realm, roles, apps, token format, migration table) is in `identity.md` (P0.4).

## Layout

```text
base/                       the same on every cluster
  operator/                 Keycloak Operator 26.8.1: unmodified upstream files + namespace + kustomization
  keycloak/
    keycloak.yaml           Keycloak (2 instances). Hostname and DB host are set per cluster
    realm-import.json       realm "taraz", generated from realm-taraz.json
local/                      local k3d cluster only
  kustomization.yaml        base/keycloak + local Postgres + Traefik route, hostname http://localhost:8080
  postgres.yaml             Postgres 16 (stands in for RDS)
  ingress.yaml              Traefik route (stands in for the Istio gateway)
  up.sh                     brings everything up; safe to re-run
  create-app-secrets.sh     random client secrets for the 7 confidential apps
  set-entra-secret.sh       asks for the Microsoft secret with hidden typing and stores it
tools/
  make-realm-import.py      realm-taraz.json -> base/keycloak/realm-import.json
```

## Run it locally

Needs Linux or WSL with Docker, plus k3d (v5.9.0), kubectl, openssl and curl. Port 8080 must be free.

1. Store the Microsoft secret once. It's hidden while you type, and it goes only into the cluster Secret:
   ```bash
   bash local/set-entra-secret.sh
   ```
   On the very first run the cluster doesn't exist yet. In that case run `local/up.sh` first: it stops and tells you to run this.
2. Bring it all up:
   ```bash
   bash local/up.sh
   ```
3. Sign in at http://localhost:8080/realms/taraz/account with **Sign in with Microsoft 365**. Members of the Entra group "Enterprise OS Admins" set up an authenticator app on their first sign-in.
4. Admin console: http://localhost:8080/admin. The user and password are in Secret `identity/keycloak-initial-admin`.

The Entra app needs this Web redirect URI: `http://localhost:8080/realms/taraz/broker/entra/endpoint` (already added).

On WSL, Ubuntu shuts down when nothing is using it, and that stops the cluster. Keep an Ubuntu window open, or run `wsl -d <distro> -- sleep infinity` in the background.

## Checks done locally on 2026-10-19

| P2.1 done-when | Result |
|---|---|
| `/realms/taraz/.well-known/openid-configuration` returns 200 | ✅ |
| A Taraz employee signs in with Microsoft 365 and gets `tenant_id=taraz` and role `employee` | ✅ (the test user also got `admin` from the group) |
| Admin sign-in asks for a one-time code | ✅ |
| Deleting the Keycloak pod loses no users | ✅ One pod deleted: the other kept serving. Both deleted: account, roles, authenticator and session all kept |

## What changes for staging and production

Add an overlay per cluster (for example `staging/`, `production/`) next to `local/`, with these differences:

| Area | Local | Staging / production |
|---|---|---|
| Database | `local/postgres.yaml` in the cluster | **RDS Postgres 16**: set `spec.db.host` to the RDS endpoint; no Postgres manifest |
| `keycloak-db` Secret (username, password) | Made by `up.sh` | **ExternalSecret** from AWS Secrets Manager |
| `keycloak-realm-secrets` Secret (8 keys, below) | Made by `create-app-secrets.sh` and `set-entra-secret.sh` | **ExternalSecret** from AWS Secrets Manager |
| Address | `http://localhost:8080` | `spec.hostname.hostname: https://auth.<domain>` |
| Routing and TLS | Traefik Ingress, plain HTTP | **Istio gateway** (Gateway API HTTPRoute to Service `keycloak-service`, port 8080). TLS ends at the gateway; Keycloak keeps `proxy.headers: xforwarded` |
| Deploying | `kubectl apply` | **Argo CD**: `base/operator` first, with ServerSideApply (the CRDs are too big for client-side apply), then the overlay |
| Entra app | localhost redirect URI | Add the Web redirect URI `https://auth.<domain>/realms/taraz/broker/entra/endpoint` |
| App redirect URIs in the realm | localhost and Railway placeholders | Each app's real staging/production URLs (`app-shell`, `scribe`, `knowledge-base`) |
| Size | 2 × (250m CPU, 768Mi to 1.5Gi memory) | Same to start; tune under load (P8.2) |

Keys in `keycloak-realm-secrets`: `KC_ENTRA_CLIENT_SECRET`, `KC_CLIENT_SECRET_SCRIBE`, `KC_CLIENT_SECRET_KNOWLEDGE_BASE`, `KC_CLIENT_SECRET_LECTRA`, `KC_CLIENT_SECRET_MCP_SERVER`, `KC_CLIENT_SECRET_TENANT_SERVICE`, `KC_CLIENT_SECRET_STORAGE_SERVICE`, `KC_CLIENT_SECRET_AUDIT_LOG`.

## Things to know

- **A realm import only creates.** If the realm already exists, the import leaves it alone. Change a live realm with keycloak-config-cli, or by hand followed by a fresh export. After changing `realm-taraz.json`, run `python tools/make-realm-import.py ../../keycloak/realm-taraz.json base/keycloak/realm-import.json   (from deploy/k8s)`.
- **The temporary admin.** The operator creates `keycloak-initial-admin`. On staging and production, sign in once, give a real admin access to the master realm, then delete the temporary admin.
- **API version.** The manifests use `k8s.keycloak.org/v2beta1`; `v2alpha1` still works but prints a deprecation warning.
- **The Entra secret currently in use is due to be replaced.** It was exposed in chat on 2026-10-19 and will be rotated at the end of V1 work. Update `KC_ENTRA_CLIENT_SECRET` then, and in Keycloak (Identity providers → Microsoft 365 → Client Secret) too, because the realm already exists.
