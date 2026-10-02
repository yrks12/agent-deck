"""A sign-in made BY HAND on a desk's own screen reaches every other desk.

The gap #94 left, in the owner's words: *"When I log in on one agent's
computer, the others won't get my logins."* #94 shared a sign-in only when the
owner answered a handoff card or uploaded from his Mac -- never one he typed
straight into a desk's browser. `login_vault.sync_running_desks` closes it: it
reads each running desk's browser on a timer, notices an auth cookie (or a
localStorage session) that changed, and fans it out, newest wins.

Fakes only -- no Docker, no Chromium. The live proof is in the PR.
"""

from __future__ import annotations

import json
import logging
import re
import time

import pytest

from server import deckconfig, desk_computer, login_vault

SECRET = "sekrit-session-value-0xC0FFEE"
NEWER = "fresh-hand-login-value-0xFEEDFACE"


def _cookie(name="sessionid", value=SECRET, domain=".instagram.com", **extra) -> dict:
    row = {"name": name, "value": value, "domain": domain, "path": "/",
           "expires": time.time() + 3600, "httpOnly": True, "secure": True,
           "session": False, "sameSite": "Lax"}
    row.update(extra)
    return row


class FakeDriver:
    """What the vault sees over CDP: cookies, localStorage, a current url."""

    def __init__(self, desk, cookies=None, local=None, url="", fail=False):
        self.desk = desk
        self.jar = [dict(c) for c in (cookies or [])]
        self.local = dict(local or {})
        self.url = url
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def page_target(self):
        return {"id": "p"}

    def page_state(self):
        return {"url": self.url, "inputs": [], "frames": []}

    def send(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if self.fail:
            raise desk_computer.browser.BrowserError(
                "cdp_unreachable", f"boom {json.dumps(params)}")
        if method == "Network.getAllCookies":
            return {"cookies": [dict(c) for c in self.jar]}
        if method == "Network.setCookies":
            byk = {(c["name"], c["domain"], c.get("path", "/")): c for c in self.jar}
            for c in params["cookies"]:
                byk[(c["name"], c["domain"], c.get("path", "/"))] = c
            self.jar = list(byk.values())
            return {}
        if method == "Runtime.evaluate":
            expr = params["expression"]
            if "getItem" in expr:
                return {"result": {"value": json.dumps(self.local)}}
            if "setItem" in expr:
                start = expr.index("const d = ") + len("const d = ")
                obj, _ = json.JSONDecoder().raw_decode(expr, start)
                self.local.update(obj)
                return {"result": {"value": len(obj)}}
        raise AssertionError(f"unexpected {method}")

    def set_calls(self):
        return [p for m, p in self.calls if m == "Network.setCookies"]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    cookies = tmp_path / "login-vault" / "cookies.json"
    ls = tmp_path / "login-vault" / "local-storage.json"
    monkeypatch.setattr(login_vault, "VAULT_PATH", cookies)
    monkeypatch.setattr(login_vault, "LS_PATH", ls)
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "exclude_domains", lambda: ())
    return cookies


def _desks(monkeypatch, drivers):
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda d: drivers[d])
    return drivers


# ── the gap #94 left: a hand sign-in on a desk's own screen ──────────────────


def test_a_manual_login_on_a_desk_fans_out_to_the_others(vault, monkeypatch):
    """Owner types a fresh Instagram sign-in into desk A's browser. B, which was
    not signed in, must have it after one sync -- and the vault must hold it."""
    drivers = _desks(monkeypatch, {
        "alpha": FakeDriver("alpha", [_cookie(value=NEWER)]),  # fresh hand login
        "bravo": FakeDriver("bravo", []),                      # not signed in
    })
    report = login_vault.sync_running_desks()
    assert report["synced"] and report["cookies"] == 1
    assert [c["name"] for c in drivers["bravo"].set_calls()[-1]["cookies"]] == ["sessionid"]
    assert drivers["bravo"].jar[0]["value"] == NEWER
    assert {c["name"]: c["value"] for c in login_vault.load(vault)} == {"sessionid": NEWER}


def test_a_login_already_shared_does_not_fan_out_again(vault, monkeypatch):
    """Every desk already holds the same session: a sync is a no-op, no writes."""
    login_vault.save([_cookie(value=NEWER)], vault)
    drivers = _desks(monkeypatch, {
        "alpha": FakeDriver("alpha", [_cookie(value=NEWER)]),
        "bravo": FakeDriver("bravo", [_cookie(value=NEWER)]),
    })
    report = login_vault.sync_running_desks()
    assert report == {"synced": False, "reason": "no_change"}
    assert drivers["bravo"].set_calls() == []


# ── a browser wake / start gets the whole vault ──────────────────────────────


def test_a_browser_wake_injects_the_vault(vault):
    """A desk whose browser was reaped and restarts is seeded from the vault --
    the restart path (`seed`) re-injects every login."""
    login_vault.save([_cookie(value=NEWER)], vault)
    woken = FakeDriver("rip-van", [])
    assert login_vault.seed("rip-van", woken) == 1
    assert woken.set_calls()[0]["cookies"][0]["value"] == NEWER


def test_a_session_only_auth_cookie_survives_a_stop_wake_cycle(vault, monkeypatch):
    """A clean reap drops session-only cookies (the browser never writes them to
    disk). The live sync captures them into the vault while the desk is up, and
    wake re-injects them -- so a reaped desk is not signed out of a site that
    keeps its session in a session cookie."""
    sess = _cookie(value=NEWER)
    sess.pop("expires")
    sess["session"] = True
    drivers = _desks(monkeypatch, {"solo": FakeDriver("solo", [sess])})
    # Captured into the vault, still as a session cookie (no expiry).
    assert login_vault.sync_running_desks()["synced"]
    stored = login_vault.load(vault)
    assert any(c["name"] == "sessionid" and c["value"] == NEWER
               and "expires" not in c for c in stored)
    # Reaped, then woken with a fresh (empty) browser: seed signs it back in.
    woken = FakeDriver("solo", [])
    assert login_vault.seed("solo", woken) >= 1
    back = woken.set_calls()[0]["cookies"]
    assert any(c["name"] == "sessionid" and c["value"] == NEWER
               and "expires" not in c for c in back)


def test_navigate_injects_a_localstorage_session_for_a_waking_browser(vault):
    """vapi keeps its session in localStorage, which cookies cannot carry. When
    a desk navigates to it, the vault's session is written in."""
    origin = "https://dashboard.vapi.ai"
    login_vault.save_ls({origin: {"USER_TOKEN": {"value": NEWER, "_seen": time.time()}}}, vault.parent / "local-storage.json")
    drv = FakeDriver("alpha", url=origin + "/")
    monkey_path = vault.parent / "local-storage.json"
    n = login_vault.inject_on_navigate("alpha", drv, origin + "/", ls_path=monkey_path)
    assert n == 1 and drv.local["USER_TOKEN"] == NEWER


# ── never roll a newer session back to an older one ──────────────────────────


def test_an_older_session_never_overwrites_a_newer_one():
    now = time.time()
    # The newer login carries the later expiry, as a re-issued session does.
    newer = _cookie(value=NEWER, expires=now + 7200)
    older = _cookie(value=SECRET, expires=now + 3600)
    merged = login_vault.merge([newer], [older])
    assert [c["value"] for c in merged] == [NEWER]  # the older read loses


def test_sync_does_not_push_an_older_desk_over_a_newer_vault(vault, monkeypatch):
    """The vault holds a fresh session; a desk still on the old one is brought
    UP to the vault, never the other way round."""
    now = time.time()
    login_vault.save([_cookie(value=NEWER, expires=now + 7200)], vault)
    drivers = _desks(monkeypatch, {
        "stale": FakeDriver("stale", [_cookie(value=SECRET, expires=now + 3600)]),
    })
    login_vault.sync_running_desks()
    assert {c["name"]: c["value"] for c in login_vault.load(vault)} == {"sessionid": NEWER}
    assert drivers["stale"].jar[0]["value"] == NEWER  # pulled up to the vault


# ── the "this desk only" opt-out ─────────────────────────────────────────────


def test_opt_out_sites_are_excluded(vault, monkeypatch):
    """A site on the opt-out list is neither read off a desk nor pushed to one."""
    monkeypatch.setattr(login_vault, "exclude_domains", lambda: ("instagram.com",))
    drivers = _desks(monkeypatch, {
        "alpha": FakeDriver("alpha", [_cookie(value=NEWER)]),
        "bravo": FakeDriver("bravo", []),
    })
    report = login_vault.sync_running_desks()
    assert report == {"synced": False, "reason": "no_change"}
    assert drivers["bravo"].set_calls() == []
    assert login_vault.load(vault) == []


def test_device_bound_google_cookies_are_never_synced(vault, monkeypatch):
    """Google DBSC cookies are browser-bound: copying them does not sign a desk
    in and churns on every rotation, so the live sync leaves them alone --
    while desks still launch with DBSC on (tests/test_desk_dbsc_off.py covers
    the switch that lifts this)."""
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: False)
    drivers = _desks(monkeypatch, {
        "alpha": FakeDriver("alpha", [_cookie("__Secure-1PSIDTS", NEWER, ".google.com")]),
        "bravo": FakeDriver("bravo", []),
    })
    report = login_vault.sync_running_desks()
    assert report == {"synced": False, "reason": "no_change"}
    assert drivers["bravo"].set_calls() == []


# ── secrets never logged ─────────────────────────────────────────────────────


def test_no_cookie_value_reaches_a_log_or_a_report(vault, monkeypatch, caplog, capsys):
    drivers = _desks(monkeypatch, {
        "alpha": FakeDriver("alpha", [_cookie(value=NEWER)]),
        "bravo": FakeDriver("bravo", [], fail=True),  # error path carries params
    })
    caplog.set_level(logging.DEBUG)
    report = login_vault.sync_running_desks()
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err + json.dumps(report)
    assert NEWER not in everything
    assert "login vault" in caplog.text  # it still says what it did


# ── the opt-out config knob ──────────────────────────────────────────────────


def test_login_sync_exclude_defaults_empty_and_parses_a_list(tmp_path):
    assert deckconfig.load(tmp_path / "missing.toml", env={}).desks.login_sync_exclude == ()
    toml = tmp_path / "deck.toml"
    toml.write_text('[desks]\nlogin_sync_exclude = ["example.com", "intranet.local"]\n')
    assert deckconfig.load(toml, env={}).desks.login_sync_exclude == (
        "example.com", "intranet.local")
