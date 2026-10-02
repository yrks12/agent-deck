"""S7d: the app's sign-in routes for a Claude account.

    POST /v1/accounts/login                  {id, label}  -> {login_id, url, status, ...}
    POST /v1/accounts/login/{login_id}/code  {code}       -> {status: done|failed, ...}
    GET  /v1/accounts/login/{login_id}                    -> {status: waiting_code|done|failed|expired}

Bearer auth on all three. Refusals keep their slug. The code is handed to the
login manager and appears in no response.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import account_login
from server import api as api_mod

AUTH = {"Authorization": "Bearer t"}
CODE = "SECRET-CODE-1234#st"


class _Logins:
    def __init__(self):
        self.codes: list[str] = []
        self.raise_next: account_login.LoginError | None = None

    def _maybe(self):
        if self.raise_next:
            err, self.raise_next = self.raise_next, None
            raise err

    def start(self, ident, label=""):
        self._maybe()
        return {"login_id": "L1", "account": ident, "label": label, "status": "waiting_code",
                "url": "https://claude.ai/oauth/authorize?x", "expires_at": 1.0, "detail": ""}

    def submit(self, login_id, code):
        self._maybe()
        self.codes.append(code)
        return {"login_id": login_id, "account": "work", "label": "Work", "status": "done",
                "url": "", "expires_at": 1.0, "detail": ""}

    def get(self, login_id):
        self._maybe()
        return {"login_id": login_id, "account": "work", "label": "Work",
                "status": "waiting_code", "url": "u", "expires_at": 1.0, "detail": ""}


@pytest.fixture
def client(monkeypatch):
    from server import accounts_api
    monkeypatch.setenv(api_mod.TOKEN_ENV, "t")
    app = FastAPI()
    api_mod.register(app, surface=api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=Path("/nonexistent/r.json"), prefs_path=Path("/nonexistent/p.json")),
        background=False)
    logins = _Logins()
    app.include_router(accounts_api.build_router(meters=object(), mover=object(),
                                                 logins=logins))
    c = TestClient(app)
    c.logins = logins
    return c


def test_start_returns_the_url(client):
    r = client.post("/v1/accounts/login", json={"id": "work", "label": "Work"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://claude.ai/") and r.json()["login_id"] == "L1"


def test_the_code_goes_through_and_is_not_echoed(client):
    r = client.post("/v1/accounts/login/L1/code", json={"code": CODE}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert client.logins.codes == [CODE]
    assert CODE not in r.text


def test_status_is_readable(client):
    r = client.get("/v1/accounts/login/L1", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "waiting_code"


@pytest.mark.parametrize("err, status", [
    (account_login.LoginError(409, "login_in_progress"), 409),
    (account_login.LoginError(400, "bad_account"), 400),
    (account_login.LoginError(502, "login_failed"), 502),
])
def test_start_refusals_keep_their_slug(client, err, status):
    client.logins.raise_next = err
    r = client.post("/v1/accounts/login", json={"id": "work"}, headers=AUTH)
    assert r.status_code == status and r.json()["reason"] == err.reason


@pytest.mark.parametrize("err", [
    account_login.LoginError(410, "expired"),
    account_login.LoginError(404, "unknown_login"),
    account_login.LoginError(400, "bad_code"),
])
def test_code_refusals_keep_their_slug_and_never_echo_the_code(client, err):
    client.logins.raise_next = err
    r = client.post("/v1/accounts/login/L1/code", json={"code": CODE}, headers=AUTH)
    assert r.status_code == err.status and r.json()["reason"] == err.reason
    assert CODE not in r.text


def test_bodies_are_checked(client):
    assert client.post("/v1/accounts/login", json={}, headers=AUTH).json()["reason"] \
        == "bad_request"
    assert client.post("/v1/accounts/login/L1/code", json={}, headers=AUTH).json()["reason"] \
        == "bad_request"


def test_all_three_are_mounted_and_need_auth(monkeypatch):
    from server import app as app_mod
    monkeypatch.setenv(api_mod.TOKEN_ENV, "t")
    real = TestClient(app_mod.app)
    assert real.post("/v1/accounts/login", json={"id": "w"}).status_code == 401
    assert real.post("/v1/accounts/login/x/code", json={"code": "c"}).status_code == 401
    assert real.get("/v1/accounts/login/x").status_code == 401
