# taraz-auth

The shared sign-in checks for Enterprise OS services (P2.3). Every service uses this instead of writing its own token checking. It does three things:

1. **Checks a Keycloak token** and tells you who's calling: a person, an AI agent acting for a person, or another service.
2. **Reads the tenant and roles**, and refuses tokens for another company, for another service, or without the right role.
3. **Gets a service token**, so your service can call other services as itself.

Python 3.11+. Depends on PyJWT and httpx; FastAPI support is optional.

## Install

Each release is a wheel on the repo's GitHub Releases (tag `taraz-auth-v<version>`), with its SHA-256 in `SHA256SUMS`. Install it by URL, pinned by hash:

```text
# requirements.txt
taraz-auth[fastapi] @ https://github.com/tarazautomation/enterprise-os-identity/releases/download/taraz-auth-v2.0.0/taraz_auth-2.0.0-py3-none-any.whl --hash=sha256:<from SHA256SUMS>
```

To publish a release, bump the version in `pyproject.toml` and push the tag: the `release taraz-auth` workflow tests, builds and publishes it.

## Settings (environment variables)

| Variable | Example | Needed for |
|---|---|---|
| `TARAZ_AUTH_AUDIENCE` | `knowledge-base` | **required**: this service's client id. Tokens must be addressed to it |
| `TARAZ_AUTH_SKIP_AUDIENCE_CHECK` | `true` | only while a service has no client of its own yet |
| `TARAZ_AUTH_ISSUER` | `https://auth.<domain>/realms/taraz` (local: `http://localhost:8080/realms/taraz`) | a service for one customer |
| `TARAZ_AUTH_TENANT_ID` | `taraz` | a service for one customer |
| `TARAZ_AUTH_JWKS_URL` | `http://keycloak-service.identity:8080/realms/taraz/protocol/openid-connect/certs` | optional: when services reach Keycloak at an internal address |
| `TARAZ_AUTH_TENANTS` | `taraz,acme` | a service for several customers, one realm each |
| `TARAZ_AUTH_ISSUER_BASE` | `https://auth.<domain>` | with `TARAZ_AUTH_TENANTS`: each issuer is `<base>/realms/<tenant>` |
| `TARAZ_AUTH_JWKS_BASE` | `http://keycloak-service.identity:8080` | optional, with `TARAZ_AUTH_TENANTS` |
| `TARAZ_AUTH_CLIENT_ID` / `TARAZ_AUTH_CLIENT_SECRET` | `scribe` / (from the secret store) | calling other services |
| `TARAZ_AUTH_TOKEN_URL` | | optional: defaults to `<issuer>/protocol/openid-connect/token` |

## Use it in a FastAPI service (KB, Scribe, MCP server, Lectra)

```python
from fastapi import Depends, FastAPI
from taraz_auth import TokenVerifier
from taraz_auth.fastapi import FastAPIAuth

app = FastAPI()
auth = FastAPIAuth(TokenVerifier.from_env())

@app.get("/me")
def me(user=Depends(auth.principal)):            # any valid token for this tenant
    return {"name": user.name, "roles": sorted(user.roles)}

@app.post("/ontology/approve")
def approve(user=Depends(auth.require_any("admin", "process_owner"))):
    ...

@app.delete("/users/{id}")
def delete_user(id: str, user=Depends(auth.require("admin"))):
    ...
```

What callers get back when something's wrong:

| Situation | HTTP | `detail.error` |
|---|---|---|
| No token, or not `Bearer` | 401 | `missing_token` |
| Malformed, forged, wrong issuer, not an access token | 401 | `invalid_token` |
| Expired | 401 | `token_expired` |
| From a realm this service doesn't serve, or a `tenant_id` that doesn't match its realm | 401 | `wrong_tenant` |
| Addressed to another service | 401 | `invalid_token` |
| Valid, but missing a role | 403 | `insufficient_role` |
| Keycloak's keys can't be fetched | 503 | `keys_unavailable` |

401 answers include `WWW-Authenticate: Bearer error="..."`. The caller is also available as `request.state.principal` (useful for the audit log, P2.8).

Routes that use these dependencies declare the `bearerAuth` scheme (HTTP bearer, JWT) in their OpenAPI description, so Swagger shows an Authorize button. In a dependency of your own, take the caller with `Depends(auth.principal)` rather than calling `auth.principal(request)`, or its routes won't declare it.

## Use it without FastAPI

```python
from taraz_auth import TokenVerifier, AuthError

verifier = TokenVerifier.from_env()
try:
    user = verifier.verify(token)            # a Principal
    user.require_roles("admin")
except AuthError as e:
    return e.status, e.code, e.message
```

`Principal` has `subject`, `tenant_id`, `roles`, `username`, `email`, `name`, `client_id`, `claims`, `is_service_account`, `is_agent`, `has_role()`, `require_roles()` and `require_any_role()`.

## Several customers

One realm per customer. A service that serves several customers lists them, and uses `Principal.tenant_id` to keep each customer's data apart:

```python
verifier = TokenVerifier.for_tenants(
    {"taraz": "https://auth.<domain>/realms/taraz", "acme": "https://auth.<domain>/realms/acme"},
    audience="knowledge-base",
)
user = verifier.verify(token)
rows = db.query(..., tenant_id=user.tenant_id)
```

or from the environment, with `TARAZ_AUTH_TENANTS` and `TARAZ_AUTH_ISSUER_BASE`.

## Agents

An agent (e.g. Claude) signs in for a person through the `external-agents` client. Its token is for that person (`subject`), with `client_id` `external-agents`, and is addressed to `mcp-server` only. To reach another service, `mcp-server` exchanges it (OAuth token exchange) for a token addressed to that service, still for the same person, with `client_id` `mcp-server`.

`is_agent` is true for both. Agents carry the person's roles except `admin` (`keycloak/access.json`), so `require("admin")` always refuses them.

## Call another service as your service

```python
import httpx
from taraz_auth import ServiceTokenProvider

tokens = ServiceTokenProvider.from_env()       # TARAZ_AUTH_CLIENT_ID / _CLIENT_SECRET / _ISSUER
with httpx.Client(auth=tokens.httpx_auth()) as http:
    http.get("http://knowledge-base.kb/api/search", params={"q": "..."})
```

The token is cached and renewed before it expires. On a 401, it's renewed once and the request retried. Service tokens carry `tenant_id` and are recognised as `is_service_account`, which needs both the `service-account-` username and the `client_id` claim from Keycloak's `service_account` client scope.

## What it checks, and why

1. **Signature,** against the realm's public keys (cached for 5 minutes, so a retired key stops working within 5 minutes; an unknown key ID triggers a refetch, at most once every 30 seconds, so a new key can take up to 30 seconds to be accepted). Only RS256 is accepted, so unsigned or HMAC-forged tokens are refused.
2. **Issuer,** expiry and issued-at, with 30 seconds of clock leeway. `exp`, `iat`, `iss` and `sub` must be present.
3. **`typ` is `Bearer`,** so ID tokens can't be used as access tokens.
4. **The realm is one this service serves,** and its `tenant_id` matches. Every token, including service tokens, carries its realm's `tenant_id`.
5. **Audience:** the token must be addressed to this service. Which apps may call which is in `keycloak/access.json`.

## Tests

```bash
pip install -e ".[test]"
pytest                       # no network; uses locally generated keys
```

Against a real Keycloak (for example the local cluster), also set `TARAZ_AUTH_LIVE_ISSUER`, `TARAZ_AUTH_LIVE_CLIENT_ID` and `TARAZ_AUTH_LIVE_CLIENT_SECRET`.

## Checked on 2026-10-19

- 38 unit tests pass (Python 3.14; the code targets 3.11+). The 2 live tests pass against the local Keycloak (P2.1 cluster).
- **P2.3 how-to-check**, using `examples/demo_service.py` and a real service token from Keycloak:

| Call | Answer |
|---|---|
| Valid Taraz service token | 200, `service-account-scribe`, tenant `taraz` |
| **Same token, after it expired** | **401 `token_expired`** |
| **Same token, sent to a service for another tenant** | **401 `wrong_tenant`** |
| No token | 401 `missing_token` |
| Tampered token | 401 `invalid_token` |
| Valid token on an admin-only route | 403 `insufficient_role` |

- The wheel `taraz_auth-1.0.0-py3-none-any.whl` installs cleanly into a fresh environment.

## Changes in 2.0

- `audience` is required. Set `TARAZ_AUTH_AUDIENCE` to the service's client id, or pass `audience=None` on purpose.
- Several customers: `TokenVerifier.for_tenants()` and `TARAZ_AUTH_TENANTS`.
- A token from a realm the service doesn't serve gets `wrong_tenant`.
- `is_agent` means the token came through an agent client, not that the person holds the `agent` role.

## Still to do

- **Adoption (part of the P2.3 done-when):** the knowledge base, Scribe and MCP server must use it (see the migration table in identity.md). Scribe vendors 1.0 today: move it to the published wheel.
