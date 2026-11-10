"""What admin-setup.py and scribe-setup.py share: signing in to the master realm and calling Keycloak's admin API.

Every answer is checked: a call that fails stops the script with what failed and what was already done, so a
half-finished run is never mistaken for a finished one.
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KEYCLOAK_URL", "https://taraz-auth.up.railway.app")


class Admin:
    def __init__(self, token: str):
        self._token = token
        self.done: list[str] = []          # steps finished so far, reported if a later one fails

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        """One admin API call. Returns the decoded answer; stops the script on any status not in `ok`."""
        status, answer = request(method, path, token=self._token, body=body)
        if status not in ok:
            finished = "; ".join(self.done) or "nothing"
            sys.exit(f"{method} {path} failed (HTTP {status}): {answer}\nAlready done: {finished}")
        return answer

    def step(self, what: str) -> None:
        self.done.append(what)
        print("done:", what)


def request(method, path, token=None, body=None, form=None):
    headers, data = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]


def sign_in() -> Admin:
    """Ask for the admin-console username, password and authenticator code. The password and code aren't shown
    as they're typed, and nothing is saved or printed."""
    user = input("Admin console username: ").strip()
    pw = getpass.getpass("Password (hidden): ")
    code = getpass.getpass("6-digit code from your authenticator app (hidden): ").strip()
    status, answer = request("POST", "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli", "username": user, "password": pw, "totp": code})
    del pw, code
    if status != 200:
        sys.exit(f"Sign-in failed (HTTP {status}). Check the password and use a fresh code.")
    return Admin(answer["access_token"])


def save_secret(path: str, value: str) -> None:
    """Write a new client secret where only this user can read it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
