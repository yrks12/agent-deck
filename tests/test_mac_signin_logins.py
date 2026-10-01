"""A sign-in done with the Mac's passkey reaches every desk's browser.

Owner, verbatim: *"i need to be able to login with the passkeys from my mac on
their computers"*. A passkey lives in his iCloud Keychain, on his Mac and his
iPhone -- never in a desk's Linux container -- so a passkey-only sign-in cannot
finish on a desk's screen. The Mac app signs him in in a throwaway Chrome
profile on the Mac (Touch ID offers the iCloud passkey there), reads that
profile's cookies over CDP, and hands them to the deck on `POST /v1/logins`.

This file pins the deck half: the route writes the cookies into the 0700 login
vault (so future desks are seeded) and into every running desk's browser, is
behind the `/v1` bearer, returns counts only, never logs a value, refuses a
malformed body with a slug, and honours `[desks] shared_logins = false`.

Fakes only: no Docker, no Chromium.
"""

from __future__ import annotations

import json
import logging
import stat
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api, desk_computer, login_vault

TOKEN = "t-test-token-not-a-real-one"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "mac-passkey-session-0xFEEDFACE"


def _cookie(name="SID", value=SECRET, domain=".example.com", **extra) -> dict:
    """A row the way Chrome's `Storage.getCookies` reports it on the Mac."""
    row = {"name": name, "value": value, "domain": domain, "path": "/",
           "expires": time.time() + 3600, "size": 40, "httpOnly": True,
           "secure": True, "session": False, "sameSite": "None",
           "priority": "Medium", "sourceScheme": "Secure", "sourcePort": 443}
    row.update(extra)
    return row


class FakeDriver:
    def __init__(self, desk: str, *, fail: bool = False, browser: bool = True):
        self.desk, self.fail, self.browser = desk, fail, browser
        self.calls: list[tuple[str, dict]] = []

    def page_target(self):
        return {"id": "p"} if self.browser else None

    def send(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params or {}))
        if self.fail:
            raise desk_computer.browser.BrowserError(
                "cdp_unreachable", f"boom {json.dumps(params)}")
        if method == "Network.setCookies":
            return {}
        raise AssertionError(f"the Mac route only writes cookies, not {method}")

    def set_rows(self) -> list[dict]:
        return [r for m, p in self.calls if m == "Network.setCookies"
                for r in p["cookies"]]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "login-vault" / "cookies.json"
    monkeypatch.setattr(login_vault, "VAULT_PATH", path)
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    return path


@pytest.fixture
def desks(monkeypatch):
    drivers = {"acme": FakeDriver("acme"), "villas": FakeDriver("villas"),
               "ops": FakeDriver("ops")}
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda desk: drivers[desk])
    return drivers


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(api.TOKEN_ENV, TOKEN)
    surface = api.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=tmp_path / "roster.json", prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json", handoffs_path=tmp_path / "handoffs.jsonl")
    app = FastAPI()
    api.register(app, surface=surface, background=False)
    return TestClient(app)


def _post(client, body, headers=AUTH):
    return client.post("/v1/logins", json=body, headers=headers)


# ── the happy path ──────────────────────────────────────────────────────────


def test_mac_cookies_reach_every_running_desk_and_the_vault(client, vault, desks):
    rows = [_cookie(), _cookie("__Secure-1PSID", domain=".google.com")]
    reply = _post(client, {"cookies": rows, "source": "mac"})

    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["ok"] is True and body["shared"] is True
    assert body["cookies"] == 2 and body["desks"] == 3 and body["failed"] == []
    for driver in desks.values():  # the card's own desk too: it is the one stuck
        assert {r["name"] for r in driver.set_rows()} == {"SID", "__Secure-1PSID"}
    stored = login_vault.load(vault)
    assert {r["name"] for r in stored} == {"SID", "__Secure-1PSID"}


def test_the_vault_is_private(client, vault, desks):
    _post(client, {"cookies": [_cookie()]})
    assert stat.S_IMODE(vault.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(vault.stat().st_mode) == 0o600


def test_read_only_fields_are_stripped_and_a_session_cookie_stays_one(
        client, vault, desks):
    _post(client, {"cookies": [_cookie(session=True, expires=-1)]})
    row = desks["acme"].set_rows()[0]
    assert "size" not in row and "session" not in row
    assert "expires" not in row, "a session cookie must not gain an expiry"


def test_an_expired_or_nameless_row_is_dropped(client, vault, desks):
    rows = [_cookie(), _cookie("OLD", expires=time.time() - 60),
            {"value": "x", "domain": ".example.com"}]
    body = _post(client, {"cookies": rows}).json()
    assert body["cookies"] == 1
    assert [r["name"] for r in desks["ops"].set_rows()] == ["SID"]


def test_a_desk_without_a_browser_is_seeded_later_not_counted(
        client, vault, desks):
    desks["ops"].browser = False
    body = _post(client, {"cookies": [_cookie()]}).json()
    assert body["desks"] == 2 and desks["ops"].set_rows() == []
    assert login_vault.load(vault), "the vault still holds it for when ops starts"


def test_one_dead_desk_does_not_stop_the_others(client, vault, desks):
    desks["villas"].fail = True
    body = _post(client, {"cookies": [_cookie()]}).json()
    assert body["shared"] is True and body["failed"] == ["villas"]
    assert body["desks"] == 2


def test_a_later_upload_merges_with_what_the_vault_had(client, vault, desks):
    _post(client, {"cookies": [_cookie("A")]})
    _post(client, {"cookies": [_cookie("B")]})
    assert {r["name"] for r in login_vault.load(vault)} == {"A", "B"}


# ── secrecy ─────────────────────────────────────────────────────────────────


def test_no_cookie_value_is_returned_or_logged(client, vault, desks, caplog):
    desks["villas"].fail = True  # a CDP error echoes its params
    caplog.set_level(logging.DEBUG)
    reply = _post(client, {"cookies": [_cookie()]})
    assert SECRET not in reply.text
    assert SECRET not in caplog.text


def test_the_route_is_write_only(client, vault, desks):
    _post(client, {"cookies": [_cookie()]})
    assert client.get("/v1/logins", headers=AUTH).status_code in (404, 405)


# ── refusals ────────────────────────────────────────────────────────────────


def test_no_bearer_no_entry(client, vault, desks):
    reply = _post(client, {"cookies": [_cookie()]}, headers={})
    assert reply.status_code == 401
    assert not vault.exists() and desks["acme"].calls == []


@pytest.mark.parametrize("body", [{}, {"cookies": "SID=1"}, {"cookies": [1, 2]},
                                  {"cookies": []}])
def test_a_body_with_no_usable_cookie_is_refused_by_slug(client, vault, desks, body):
    reply = _post(client, body)
    assert reply.status_code == 400
    assert reply.json()["reason"] == "no_cookies"
    assert not vault.exists()


def test_too_many_cookies_is_refused(client, vault, desks):
    rows = [_cookie(f"c{i}") for i in range(login_vault.MAX_COOKIES + 1)]
    reply = _post(client, {"cookies": rows})
    assert reply.status_code == 413 and reply.json()["reason"] == "too_many_cookies"


def test_isolated_desks_refuse_rather_than_pretend(client, vault, desks,
                                                   monkeypatch):
    monkeypatch.setattr(login_vault, "enabled", lambda: False)
    reply = _post(client, {"cookies": [_cookie()]})
    assert reply.status_code == 409 and reply.json()["reason"] == "isolated"
    assert not vault.exists() and desks["acme"].calls == []
