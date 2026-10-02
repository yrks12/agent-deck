"""Why things were signing out, on the desks and on the owner's Mac.

Owner, verbatim: *"Why are things signing out, both on my Mac and on their
computers?"* MEASURED on the box and his Mac, 2026-10-01 (UTC, names and
times only):

* 17:32:50 and 17:34:20 a desk opened tiktok.com/logout; 17:32:46 and 19:47:07
  desks opened accounts.google.com/SignOutOptions. Every desk shares ONE
  session per site, so one desk's sign-out revoked it for all of them (19:48
  atlas had to sign Google in again; 30 cookies re-fanned to 5 desks).
* TikTok and Instagram: his everyday-Chrome session was copied to every desk
  ("Use my Chrome login", 17:36:14 / 23:39:25) and died on the desks and on
  his Mac (Mac back at tiktok.com/login 18:19:27). The vault held a TikTok
  cookie set stitched from three different sessions (17:36, 18:21, 18:26):
  per-cookie last-writer-wins mixes sessions.
* Stripe and GitHub: his live Mac sessions sit, byte-for-byte by expiry, on
  every desk.

So: per-desk sites never fan out, a logged-out cookie never travels, the
freshest SET wins (not the latest expiry), a desk cannot open a sign-out URL,
and the desks are told why. Fakes only.
"""

from __future__ import annotations

import time

import pytest

from server import capabilities, deckconfig, desk_computer, login_vault
from server.roster import Desk

NEW = "fresh-session-0xFEED"
OLD = "older-session-0xC0DE"


def _cookie(name="sessionid", value=NEW, domain=".example.app", **extra) -> dict:
    row = {"name": name, "value": value, "domain": domain, "path": "/",
           "expires": time.time() + 3600, "httpOnly": True, "secure": True,
           "session": False, "sameSite": "Lax"}
    row.update(extra)
    return row


class Driver:
    def __init__(self, cookies=None):
        self.jar = [dict(c) for c in (cookies or [])]
        self.sets: list[list[dict]] = []

    def page_target(self):
        return {"id": "p"}

    def page_state(self):
        return {"url": "", "inputs": [], "frames": []}

    def send(self, method, params=None):
        if method == "Network.getAllCookies":
            return {"cookies": [dict(c) for c in self.jar]}
        if method == "Network.setCookies":
            self.sets.append(params["cookies"])
            byk = {(c["name"], c["domain"]): c for c in self.jar}
            for c in params["cookies"]:
                byk[(c["name"], c["domain"])] = c
            self.jar = list(byk.values())
            return {}
        raise AssertionError(method)

    def value(self, name="sessionid"):
        return next((c["value"] for c in self.jar if c["name"] == name), None)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "login-vault" / "cookies.json"
    monkeypatch.setattr(login_vault, "VAULT_PATH", path)
    monkeypatch.setattr(login_vault, "LS_PATH", path.parent / "local-storage.json")
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "desk_dbsc_off", lambda: False)
    # The real opt-out reader, over an empty deck.toml: only the built-in
    # per-desk sites apply.
    empty = deckconfig.load(tmp_path / "none.toml", env={})
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: empty)
    return path


def _desks(monkeypatch, drivers):
    monkeypatch.setattr(login_vault, "running_desks", lambda: sorted(drivers))
    monkeypatch.setattr(login_vault, "driver_for", lambda d: drivers[d])
    return drivers


# ── a logged-out cookie never travels ───────────────────────────────────────


@pytest.mark.parametrize("dead", ["", "deleted"])
def test_a_logged_out_cookie_is_never_fanned_out(vault, monkeypatch, dead):
    """A desk that signed out holds `sessionid=""` (or "deleted") with a long
    expiry. It must not overwrite the vault's live session, nor reach a desk
    that is still signed in."""
    login_vault.save([_cookie(value=NEW)], vault)
    drivers = _desks(monkeypatch, {
        "out": Driver([_cookie(value=dead, expires=time.time() + 9e6)]),
        "in": Driver([_cookie(value=NEW)]),
    })
    login_vault.sync_running_desks()
    assert drivers["in"].value() == NEW
    assert drivers["in"].sets == [] or all(
        c["value"] == NEW for batch in drivers["in"].sets for c in batch)
    assert [c["value"] for c in login_vault.load(vault)] == [NEW]


def test_a_logged_out_cookie_is_not_accepted_from_the_mac(vault, monkeypatch):
    _desks(monkeypatch, {"a": Driver()})
    with pytest.raises(login_vault.ShareRefused):
        login_vault.share_in([_cookie(value="")])


# ── per-desk sites are never shared ─────────────────────────────────────────


@pytest.mark.parametrize("domain", [".tiktok.com", ".instagram.com", "dashboard.stripe.com"])
def test_per_desk_sites_are_not_fanned_out_by_the_live_sync(vault, monkeypatch, domain):
    drivers = _desks(monkeypatch, {
        "alpha": Driver([_cookie(domain=domain)]),
        "bravo": Driver([]),
    })
    login_vault.sync_running_desks()
    assert drivers["bravo"].sets == []
    assert login_vault.load(vault) == []


def test_per_desk_sites_are_not_seeded_into_a_new_browser(vault):
    login_vault.save([_cookie(domain=".tiktok.com"), _cookie(domain=".example.app")], vault)
    woken = Driver()
    login_vault.seed("new", woken)
    assert {c["domain"] for batch in woken.sets for c in batch} == {".example.app"}


def test_a_handoff_sign_in_on_a_per_desk_site_stays_on_that_desk(vault, monkeypatch):
    drivers = _desks(monkeypatch, {
        "signer": Driver([_cookie(domain=".instagram.com"), _cookie(domain=".example.app")]),
        "other": Driver([]),
    })
    login_vault.fan_out("signer", handoff_id="h1")
    assert {c["domain"] for batch in drivers["other"].sets for c in batch} == {".example.app"}


def test_the_mac_cannot_share_a_per_desk_site_to_every_desk(vault, monkeypatch):
    drivers = _desks(monkeypatch, {"a": Driver()})
    with pytest.raises(login_vault.ShareRefused) as refused:
        login_vault.share_in([_cookie(domain=".tiktok.com")])
    assert refused.value.reason == "per_desk_site"
    assert drivers["a"].sets == []


# ── the freshest SET wins, not the latest expiry ────────────────────────────


def test_a_fresh_set_with_a_shorter_expiry_beats_a_stale_copy(vault, monkeypatch):
    """Desk bravo's site rotates the session to a new value whose expiry is
    EARLIER than the old one (a fixed-lifetime session, or a refresh). Desk
    alpha still holds the old one, unchanged. The new one must win and reach
    alpha; the old one must never be pushed back over bravo."""
    now = time.time()
    old = _cookie(value=OLD, expires=now + 90 * 86400)
    login_vault.save([old], vault)
    drivers = _desks(monkeypatch, {"alpha": Driver([old]), "bravo": Driver([old])})
    login_vault.sync_running_desks()          # both seen holding OLD
    drivers["bravo"].jar = [_cookie(value=NEW, expires=now + 30 * 86400)]
    login_vault.sync_running_desks()
    assert drivers["bravo"].value() == NEW
    assert drivers["alpha"].value() == NEW
    assert [c["value"] for c in login_vault.load(vault)] == [NEW]


# ── a desk cannot open a sign-out URL ───────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://accounts.google.com/Logout?continue=x",
    "https://accounts.google.com/SignOutOptions",
    "https://www.tiktok.com/logout",
    "https://example.app/users/sign_out",
])
def test_navigate_refuses_a_sign_out_url(monkeypatch, url):
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(desk_computer, "_allowed", lambda *a: None)
    monkeypatch.setattr(desk_computer, "_raise_card", lambda *a, **k: "c1")
    monkeypatch.setattr(desk_computer, "ensure",
                        lambda desk: (_ for _ in ()).throw(AssertionError("navigated")))
    page = desk_computer.navigate("d", url)
    assert "sign" in page["text"].lower() and "every desk" in page["text"]


def test_an_ordinary_url_is_not_a_sign_out():
    assert not login_vault.signs_everyone_out("https://www.tiktok.com/@someone")
    assert not login_vault.signs_everyone_out("https://accounts.google.com/ServiceLogin")


# ── the desks are told ──────────────────────────────────────────────────────


def test_capabilities_tell_a_desk_never_to_sign_out():
    inv = capabilities.Inventory(deck_servers=["computer"])
    text = capabilities.render(inv, Desk(name="atlas", cwd="/tmp", engine="claude",
                                         mission="m"))
    assert "never sign out" in text.lower()
    assert "tiktok" in text.lower()


# ── round 2: purge, provenance, the card's words, sign-out clicks ───────────

from server import browser, browser_takeover, sandbox  # noqa: E402

SESS = browser.BrowserSession(desk="atlas", display=":99", cdp_port=9222,
                              profile_dir="/p", pid=None, started_at=0.0)


def test_the_vault_never_keeps_a_per_desk_site(vault):
    """His Mac's Stripe / Instagram copies sat in the vault although nothing
    used them. A per-desk site's cookie is never written to it."""
    login_vault.save([_cookie(domain="dashboard.stripe.com"),
                      _cookie(domain=".instagram.com"), _cookie()], vault)
    assert [c["domain"] for c in login_vault.load(vault)] == [".example.app"]


def test_every_vault_row_says_where_it_came_from(vault, monkeypatch):
    drivers = _desks(monkeypatch, {"a": Driver([_cookie(name="sid", value=OLD)]),
                                   "b": Driver()})
    login_vault.share_in([_cookie()], source="mac-chrome")
    login_vault.sync_running_desks()
    src = {c["name"]: c.get("_src") for c in login_vault.load(vault)}
    assert src == {"sessionid": "mac-chrome", "sid": "desk"}
    assert all("_src" not in c for batch in drivers["b"].sets for c in batch)


def test_the_passkey_card_does_not_promise_every_desk_for_a_per_desk_site():
    needs = browser_takeover.passkey_needs(SESS, "https://www.tiktok.com/login")
    assert "every desk" not in needs
    assert "this desk" in needs.lower() or "own screen" in needs.lower()
    assert "every desk" in browser_takeover.passkey_needs(SESS, "https://github.com/login")


class Screen:
    """The desk's browser as `act` sees it: what sits under a click."""

    def __init__(self, href="", text=""):
        self.target = {"href": href, "text": text}
        self.sess = SESS

    def page_target(self):
        return {"id": "p"}

    def _evaluate(self, js):
        return self.target

    def page_state(self):
        return {"url": "https://accounts.google.com/", "inputs": [], "frames": []}


@pytest.fixture
def clicks(monkeypatch):
    sent, cards = [], []
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(desk_computer, "_guard", lambda desk: None)
    monkeypatch.setattr(desk_computer, "_allowed", lambda *a: None)
    monkeypatch.setattr(desk_computer, "_raise_card",
                        lambda sess, kind, url, **k: cards.append((kind, url)) or "c1")
    monkeypatch.setattr(sandbox, "send_input", lambda desk, action: sent.append(action))
    return sent, cards


@pytest.mark.parametrize("href,text", [
    ("", "Sign out"), ("", "Log out of all accounts"),
    ("https://www.tiktok.com/logout", "Bye"),
    ("https://accounts.google.com/SignOutOptions?hl=en", ""),
])
def test_a_sign_out_click_raises_a_card_instead_of_clicking(monkeypatch, clicks, href, text):
    sent, cards = clicks
    monkeypatch.setattr(desk_computer, "ContainerCDP", lambda desk: Screen(href, text))
    with pytest.raises(PermissionError) as held:
        desk_computer.act("atlas", {"action": "click", "x": 10, "y": 10})
    assert sent == [] and cards and "c1" in str(held.value)


def test_an_ordinary_click_goes_through(monkeypatch, clicks):
    sent, cards = clicks
    monkeypatch.setattr(desk_computer, "ContainerCDP", lambda desk: Screen("", "Settings"))
    desk_computer.act("atlas", {"action": "click", "x": 10, "y": 10})
    assert len(sent) == 1 and cards == []


def test_an_approved_sign_out_click_goes_through(monkeypatch, clicks):
    sent, cards = clicks
    monkeypatch.setattr(desk_computer, "_allowed", lambda *a: "once")
    monkeypatch.setattr(desk_computer, "ContainerCDP", lambda desk: Screen("", "Sign out"))
    desk_computer.act("atlas", {"action": "click", "x": 10, "y": 10})
    assert len(sent) == 1


def test_a_sign_out_navigation_raises_a_card(monkeypatch, clicks):
    sent, cards = clicks
    page = desk_computer.navigate("atlas", "https://www.tiktok.com/logout")
    assert cards and page["card"] == "c1" and "c1" in page["text"]
