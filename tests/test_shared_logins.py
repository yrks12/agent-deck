"""The owner signs in once and every desk's browser is signed in.

Owner, verbatim: *"why does each agent need me to log in on his computer if
it's the same server?"* Each desk has its own container and its own Chromium
profile, so today a sign-in on one desk's screen signs nothing else in.

`server/login_vault.py` is the answer: when a browser sign-in handoff is
answered `done`, the signed-in desk's cookies are read over CDP, merged into
one vault on the deck's disk (0700 / 0600), and pushed into every other
running desk's browser; a desk whose browser starts later is seeded from the
vault. `[desks] shared_logins = false` keeps per-desk isolation.

Every test here uses fakes -- no Docker, no Chromium. The live proof is in the
PR (wake-probe plus a throwaway desk against httpbin.org).
"""

from __future__ import annotations

import json
import logging
import stat
import time

import pytest

from server import api, deckconfig, desk_computer, handoff, login_vault

SECRET = "sekrit-session-value-0xC0FFEE"


def _cookie(name="SID", value=SECRET, domain=".example.com", **extra) -> dict:
    row = {"name": name, "value": value, "domain": domain, "path": "/",
           "expires": time.time() + 3600, "size": 10, "httpOnly": True,
           "secure": True, "session": False, "sameSite": "Lax",
           "priority": "Medium", "sourceScheme": "Secure", "sourcePort": 443}
    row.update(extra)
    return row


class FakeDriver:
    """What `ContainerCDP.send` looks like to the vault: one CDP call, one reply."""

    def __init__(self, desk: str, cookies: list[dict] | None = None,
                 fail: bool = False) -> None:
        self.desk = desk
        self.jar = list(cookies or [])
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def page_target(self):
        return {"id": "p"}

    def send(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params or {}))
        if self.fail:
            raise desk_computer.browser.BrowserError(
                "cdp_unreachable", f"boom {json.dumps(params)}")
        if method == "Network.getAllCookies":
            return {"cookies": [dict(c) for c in self.jar]}
        if method == "Network.setCookies":
            self.jar.extend(params["cookies"])
            return {}
        raise AssertionError(method)

    def set_calls(self) -> list[dict]:
        return [p for m, p in self.calls if m == "Network.setCookies"]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "login-vault" / "cookies.json"
    monkeypatch.setattr(login_vault, "VAULT_PATH", path)
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    return path


@pytest.fixture
def desks(monkeypatch):
    """Three running desks; the owner signed in on `acme`."""
    drivers = {
        "acme": FakeDriver("acme", [_cookie(), _cookie("NID", "anon", ".google.com")]),
        "harbor": FakeDriver("harbor"),
        "ops": FakeDriver("ops"),
    }
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda desk: drivers[desk])
    return drivers


def _browser_handoff(hid="h1", agent="acme", status="done") -> handoff.Handoff:
    return handoff.Handoff(
        id=hid, ts=time.time(), agent=agent, kind="login",
        needs="Take over acme's screen and sign in.", state="stopped",
        where="https://accounts.example.com/signin",
        evidence="surface=browser desk=acme origin=https://accounts.example.com "
                 "display=:99",
        status=status)


# ── CDP export / import ─────────────────────────────────────────────────────


def test_export_reads_every_cookie_over_cdp_and_keeps_only_settable_fields():
    driver = FakeDriver("acme", [_cookie(size=99, session=False)])
    rows = login_vault.export_cookies(driver)
    assert driver.calls[0][0] == "Network.getAllCookies"
    assert rows[0]["value"] == SECRET
    # `size` and `session` are read-only in Network.CookieParam; sending them
    # back makes Chromium reject the whole setCookies call.
    assert "size" not in rows[0] and "session" not in rows[0]


def test_a_session_cookie_is_imported_as_a_session_cookie():
    driver = FakeDriver("acme", [_cookie(session=True, expires=-1)])
    rows = login_vault.export_cookies(driver)
    assert "expires" not in rows[0]


def test_import_uses_one_set_cookies_call_and_drops_expired_rows():
    target = FakeDriver("harbor")
    fresh, stale = _cookie("A"), _cookie("B", expires=time.time() - 5)
    assert login_vault.import_cookies(target, [fresh, stale]) == 1
    assert [c["name"] for c in target.set_calls()[0]["cookies"]] == ["A"]


# ── the vault on disk ───────────────────────────────────────────────────────


def test_the_vault_is_private_to_the_deck_user(vault):
    login_vault.save([_cookie()], vault)
    assert stat.S_IMODE(vault.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(vault.stat().st_mode) == 0o600
    assert login_vault.load(vault)[0]["value"] == SECRET


def test_merge_replaces_the_same_cookie_and_keeps_the_others(vault):
    login_vault.save([_cookie("SID", "old"), _cookie("KEEP", "k")], vault)
    merged = login_vault.merge(login_vault.load(vault), [_cookie("SID", "new")])
    by_name = {c["name"]: c["value"] for c in merged}
    assert by_name == {"SID": "new", "KEEP": "k"}


def test_a_corrupt_vault_is_empty_not_a_crash(vault):
    vault.parent.mkdir(parents=True)
    vault.write_text("{not json")
    assert login_vault.load(vault) == []


# ── a new desk gets the vault when its browser starts ───────────────────────


def test_a_new_desks_browser_is_seeded_from_the_vault(vault):
    login_vault.save([_cookie()], vault)
    newcomer = FakeDriver("newbie")
    assert login_vault.seed("newbie", newcomer) == 1
    assert newcomer.set_calls()[0]["cookies"][0]["value"] == SECRET


def test_seed_is_a_no_op_when_isolation_is_chosen(vault, monkeypatch):
    login_vault.save([_cookie()], vault)
    monkeypatch.setattr(login_vault, "enabled", lambda: False)
    newcomer = FakeDriver("newbie")
    assert login_vault.seed("newbie", newcomer) == 0
    assert newcomer.calls == []


def test_ensure_seeds_only_when_it_launched_the_browser(monkeypatch):
    """`desk_computer.ensure` runs on every tool call; seeding every call would
    overwrite a desk's own newer cookies with the vault's older ones."""
    seeded: list[str] = []
    monkeypatch.setattr(login_vault, "seed", lambda desk, d: seeded.append(desk) or 0)
    monkeypatch.setattr(desk_computer.sandbox, "is_up", lambda desk: True)
    monkeypatch.setattr(desk_computer.sandbox, "_run", lambda argv, **k: None)
    state = {"up": False}

    class Driver(FakeDriver):
        def page_target(self):
            return {"id": "p"} if state["up"] else None

        def wait_for_page(self, timeout=0):
            state["up"] = True

    monkeypatch.setattr(desk_computer, "ContainerCDP", lambda desk: Driver(desk))
    desk_computer.ensure("acme")
    desk_computer.ensure("acme")
    assert seeded == ["acme"]


# ── a completed sign-in fans out, exactly once ──────────────────────────────


def test_a_done_browser_handoff_signs_in_every_running_desk(vault, desks):
    report = login_vault.after_handoff(_browser_handoff(), "done", background=False)
    assert report["desks"] == 2 and report["cookies"] == 2
    for name in ("harbor", "ops"):
        assert {c["name"] for c in desks[name].set_calls()[0]["cookies"]} == {"SID", "NID"}
    assert desks["acme"].set_calls() == []  # never back into the source
    assert {c["name"] for c in login_vault.load(vault)} == {"SID", "NID"}


def test_the_same_handoff_fans_out_exactly_once(vault, desks):
    login_vault.after_handoff(_browser_handoff(), "done", background=False)
    again = login_vault.after_handoff(_browser_handoff(), "done", background=False)
    assert again["shared"] is False and again["reason"] == "already_shared"
    assert len(desks["harbor"].set_calls()) == 1


@pytest.mark.parametrize("outcome,evidence", [
    ("skipped", "surface=browser desk=acme"),
    ("done", "surface=terminal desk=acme"),
])
def test_only_a_done_browser_handoff_shares(vault, desks, outcome, evidence):
    h = _browser_handoff()
    h = handoff.Handoff(**{**h.__dict__, "evidence": evidence})
    report = login_vault.after_handoff(h, outcome, background=False)
    assert report["shared"] is False
    assert desks["harbor"].calls == []


def test_isolation_toggle_keeps_every_desk_separate(vault, desks, monkeypatch):
    monkeypatch.setattr(login_vault, "enabled", lambda: False)
    report = login_vault.after_handoff(_browser_handoff(), "done", background=False)
    assert report == {"shared": False, "reason": "isolated"}
    assert desks["harbor"].calls == [] and not vault.exists()


def test_one_dead_desk_does_not_stop_the_others(vault, desks):
    desks["harbor"].fail = True
    report = login_vault.after_handoff(_browser_handoff(), "done", background=False)
    assert report["desks"] == 1 and report["failed"] == ["harbor"]
    assert desks["ops"].set_calls()


def test_resolve_handoff_hands_the_settled_card_to_the_vault(tmp_path, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(login_vault, "after_handoff",
                        lambda h, outcome, **k: seen.append((h.id, outcome)))
    path = tmp_path / "handoffs.json"
    h = handoff.raise_handoff(path, agent="acme", kind="login", needs="sign in",
                              state="stopped", where="https://example.com",
                              evidence="surface=browser desk=acme")
    surface = api.Surface.__new__(api.Surface)
    monkeypatch.setattr(api.Surface, "handoffs_path", property(lambda self: path))
    monkeypatch.setattr(api.Surface, "_resume_after_handoff", lambda *a: True)
    monkeypatch.setattr(api.Surface, "_handoff_row", lambda self, h: {"id": h.id})
    surface.resolve_handoff(h.id, "done")
    assert seen == [(h.id, "done")]


# ── the toggle ──────────────────────────────────────────────────────────────


def test_shared_logins_defaults_on_and_can_be_turned_off(tmp_path):
    assert deckconfig.load(tmp_path / "missing.toml", env={}).desks.shared_logins is True
    toml = tmp_path / "deck.toml"
    toml.write_text("[desks]\nshared_logins = false\n")
    assert deckconfig.load(toml, env={}).desks.shared_logins is False


def test_enabled_reads_the_deck_config(tmp_path, monkeypatch):
    toml = tmp_path / "deck.toml"
    toml.write_text("[desks]\nshared_logins = false\n")
    monkeypatch.setenv("DECK_CONFIG", str(toml))
    assert login_vault.enabled() is False
    monkeypatch.setenv("DECK_CONFIG", str(tmp_path / "missing.toml"))
    assert login_vault.enabled() is True


# ── secrets never logged ────────────────────────────────────────────────────


def test_no_cookie_value_reaches_a_log_or_a_report(vault, desks, caplog, capsys):
    desks["ops"].fail = True  # the error path carries the params in its text
    caplog.set_level(logging.DEBUG)
    report = login_vault.after_handoff(_browser_handoff(), "done", background=False)
    login_vault.seed("newbie", FakeDriver("newbie", fail=True))
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err + json.dumps(report)
    assert SECRET not in everything
    assert "login vault" in caplog.text  # it does say what it did
