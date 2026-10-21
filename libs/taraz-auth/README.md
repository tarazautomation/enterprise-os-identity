# taraz-auth

The shared sign-in checks for Enterprise OS services (P2.3). Every service uses this instead of writing its own token checking. It does three things:

1. **Checks a Keycloak token** and tells you who's calling: a person, an AI agent acting for a person, or another service.
2. **Reads the tenant and roles**, and refuses tokens for another company or without the right role.
3. **Gets a service token**, so your service can call other services as itself.

Python 3.11+. Depends on PyJWT and httpx; FastAPI support is optional.

## Install

```bash
pip install "taraz-auth[fastapi]==1.0.0"
```

Where it's published is still open (see the end). Until then, install from the wheel (`dist/taraz_auth-1.0.0-py3-none-any.whl`) or from git at tag `taraz-auth-v1.0.0`.

## Settings (environment variables)

| Variable | Example | Needed for |
|---|---|---|
| `TARAZ_AUTH_ISSUER` | `https://auth.<domain>/realms/taraz` (local: `http://localhost:8080/realms/taraz`) | everything |
| `TARAZ_AUTH_TENANT_ID` | `taraz` | checking tokens |
| `TARAZ_AUTH_JWKS_URL` | `http://keycloak-service.identity:8080/realms/taraz/protocol/openid-connect/certs` | optional: when services reach Keycloak at an internal address |
| `TARAZ_AUTH_AUDIENCE` | `knowledge-base` | optional: only if we decide each service checks its audience |
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
| For another tenant, or no tenant | 401 | `wrong_tenant` |
| Valid, but missing a role | 403 | `insufficient_role` |
| Keycloak's keys can't be fetched | 503 | `keys_unavailable` |

401 answers include `WWW-Authenticate: Bearer error="..."`. The caller is also available as `request.state.principal` (useful for the audit log, P2.8).

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

## Call another service as your service

```python
import httpx
from taraz_auth import ServiceTokenProvider

tokens = ServiceTokenProvider.from_env()       # TARAZ_AUTH_CLIENT_ID / _CLIENT_SECRET / _ISSUER
with httpx.Client(auth=tokens.httpx_auth()) as http:
    http.get("http://knowledge-base.kb/api/search", params={"q": "..."})
```

The token is cached and renewed before it expires. On a 401, it's renewed once and the request retried. Service tokens carry `tenant_id` and are recognised as `is_service_account`.

## What it checks, and why

1. **Signature,** against the realm's public keys (cached for 5 minutes; unknown key IDs trigger a refetch, so key rotation just works). Only RS256 is accepted, so unsigned or HMAC-forged tokens are refused.
2. **Issuer,** expiry and issued-at, with 30 seconds of clock leeway. `exp`, `iat`, `iss` and `sub` must be present.
3. **`typ` is `Bearer`,** so ID tokens can't be used as access tokens.
4. **`tenant_id` equals this cluster's tenant.** Every Taraz token, including service tokens, carries `tenant_id=taraz`.
5. **Audience,** only if `TARAZ_AUTH_AUDIENCE` is set. This waits on the open audience decision in identity.md.

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

## Still to decide

- **Where to publish.** P0.6 plans a shared-libraries folder in the platform repo. Options: install from git by tag, or a private package index (for example AWS CodeArtifact). Decide with Zinoor.
- **Audience checks per service.** The library supports them; they're off until decided.
- **Adoption (part of the P2.3 done-when):** the knowledge base, Scribe and MCP server must use it. Scribe is Junaid's; the KB and MCP server are Subhan's (see the migration table in identity.md).
