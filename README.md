# Enterprise OS: identity

Sign-in and identity for Enterprise OS: one login for every app, through Keycloak and Microsoft 365.
Owner: Junaid (lane: identity, Scribe, Lectra, app shell). This repo moves into the platform repo once it exists (P0.6).

| Folder | What | Task |
|---|---|---|
| `docs/architecture/identity.md` | The design: realm, roles, apps, token format, admin one-time code, and how each existing app moves onto Keycloak | P0.4 |
| `keycloak/realm-taraz.json` | The realm `taraz`, as one file. No secrets: 8 `${KC_*}` placeholders, filled from the environment at import | P0.4 |
| `deploy/k8s/` | Keycloak on Kubernetes (operator, 2 instances, Postgres, realm import), proven on a local k3d cluster. See its README for what changes on EKS | P2.1 |
| `deploy/railway/` | How the interim Railway deployment is set up (until EKS exists) | P2.1 (interim) |
| `libs/taraz-auth/` | The shared Python library every service uses to check tokens, read tenant and roles, and call other services | P2.3 |
| `services/tenant-service/` | The member directory and admin-portal back end (coming next) | P2.2 |

## Where it runs today

- **Railway (interim):** https://taraz-auth.up.railway.app. Issuer: `https://taraz-auth.up.railway.app/realms/taraz`. It moves to a Taraz domain, then to EKS; apps read the issuer from settings, so that's a configuration change.
- **Local:** `deploy/k8s/local/up.sh` brings up the same setup on a k3d cluster at http://localhost:8080.

## Rules for this repo

- **Never commit secrets.** The realm file uses `${KC_*}` placeholders, and secrets live in Railway variables (later AWS Secrets Manager).
- **When the realm changes,** export it again (users skipped), replace secrets with placeholders, regenerate `deploy/k8s/base/keycloak/realm-import.json` with `deploy/k8s/tools/make-realm-import.py`, and note the change in `identity.md`.
