"""The few Keycloak admin calls this service needs, as its own service account.

The service account (client tenant-service) holds realm-management view-users, query-users,
manage-users and view-realm in its realm only.
"""

from typing import Any

import httpx
from taraz_auth import ServiceTokenProvider


class KeycloakError(RuntimeError):
    pass


class KeycloakAdmin:
    def __init__(self, base_url: str, realm: str, http: httpx.Client):
        self.realm = realm
        self._http = http
        self._prefix = f"{base_url.rstrip('/')}/admin/realms/{realm}"
        self._role_cache: dict[str, dict] = {}

    @classmethod
    def from_settings(cls, settings) -> "KeycloakAdmin":
        tokens = ServiceTokenProvider(settings.token_endpoint, settings.client_id, settings.client_secret)
        return cls(settings.keycloak_base, settings.realm, httpx.Client(auth=tokens.httpx_auth(), timeout=15))

    def _call(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, self._prefix + path, **kw)
        if r.status_code == 404 and method == "GET":
            return None
        if r.status_code >= 400:
            raise KeycloakError(f"Keycloak {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    def _pages(self, path: str, **params):
        first = 0
        while True:
            page = self._call("GET", path, params={**params, "first": first, "max": 100}) or []
            yield from page
            if len(page) < 100:
                return
            first += 100

    # --- reads ---

    def list_users(self) -> list[dict]:
        """Every real user in the realm (service accounts left out)."""
        return [u for u in self._pages("/users", briefRepresentation="false")
                if not u.get("serviceAccountClientId") and not u.get("username", "").startswith("service-account-")]

    def get_user(self, user_id: str) -> dict | None:
        return self._call("GET", f"/users/{user_id}")

    def role_member_ids(self, role: str) -> set[str]:
        """Ids of users holding this realm role directly."""
        return {u["id"] for u in self._pages(f"/roles/{role}/users", briefRepresentation="true")}

    def user_realm_roles(self, user_id: str) -> set[str]:
        """Effective realm roles of one user (including those from the default role)."""
        return {r["name"] for r in (self._call("GET", f"/users/{user_id}/role-mappings/realm/composite") or [])}

    # --- writes ---

    def _role(self, name: str) -> dict:
        if name not in self._role_cache:
            rep = self._call("GET", f"/roles/{name}")
            if rep is None:
                raise KeycloakError(f"realm role {name!r} doesn't exist")
            self._role_cache[name] = {"id": rep["id"], "name": rep["name"]}
        return self._role_cache[name]

    def add_roles(self, user_id: str, roles: set[str]) -> None:
        if roles:
            self._call("POST", f"/users/{user_id}/role-mappings/realm", json=[self._role(r) for r in sorted(roles)])

    def remove_roles(self, user_id: str, roles: set[str]) -> None:
        if roles:
            self._call("DELETE", f"/users/{user_id}/role-mappings/realm", json=[self._role(r) for r in sorted(roles)])

    def set_enabled(self, user_id: str, enabled: bool) -> None:
        user = self.get_user(user_id)
        if user is None:
            raise KeycloakError(f"user {user_id} not found in Keycloak")
        user["enabled"] = enabled
        self._call("PUT", f"/users/{user_id}", json=user)

    def logout(self, user_id: str) -> None:
        """End all of the user's sessions, so a disabled user loses access quickly."""
        self._call("POST", f"/users/{user_id}/logout")
