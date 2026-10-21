"""A tiny service protected by taraz-auth, for the P2.3 "how to check".

    TARAZ_AUTH_ISSUER=http://localhost:8080/realms/taraz TARAZ_AUTH_TENANT_ID=taraz \
        uvicorn examples.demo_service:app --port 8099

    curl -H "Authorization: Bearer <token>" http://localhost:8099/whoami
"""

from fastapi import Depends, FastAPI

from taraz_auth import TokenVerifier
from taraz_auth.fastapi import FastAPIAuth

app = FastAPI(title="taraz-auth demo")
auth = FastAPIAuth(TokenVerifier.from_env())


@app.get("/whoami")
def whoami(user=Depends(auth.principal)):
    return {
        "username": user.username,
        "tenant_id": user.tenant_id,
        "roles": sorted(user.roles),
        "service_account": user.is_service_account,
        "client_id": user.client_id,
    }


@app.get("/admin-only")
def admin_only(user=Depends(auth.require("admin"))):
    return {"ok": True}
