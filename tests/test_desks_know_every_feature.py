"""A desk is told everything the deck can do for it -- generated, never stale.

Owner, 2026-09-30: "why is Atlas not aware of the login with my Mac to get
passkeys?" and "we need to let him know of ANY feature we have existing".
MEASURED: the brief named the computer tools and nothing else the deck does --
not the Mac sign-in card, not the login vault, not the store, not calls. A
desk asked the owner to take over the screen for a sign-in the deck could do
in one tap on his Mac.

So the "What the deck does for you" section is generated from a registry
(`server/features.py`) of what is enabled on THIS deck, and rides the rules
versioning (`server/rules.py`), so a running desk is caught up on its next
message. The detectors below are what keep it from drifting again:

* every MCP tool a desk is given has a registry entry;
* every route the deck serves is a feature or explicitly internal;
* every user-facing config flag is a feature or explicitly internal;
* every tool a line names really exists;
* every app button a line names really exists in the Mac app.
"""

import re
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from server import (computer_mcp, deck_mcp, deckconfig, features, hire,
                    mac_mcp, rules)

ROOT = Path(__file__).resolve().parents[1]
TRAY = ROOT / "macos" / "Sources" / "DeckUI" / "AttentionTrayView.swift"
# The card's button words: one list for the side pane, the thread and the phone.
SIGN_IN_CARD = ROOT / "macos" / "Sources" / "DeckKit" / "SignInCard.swift"

SERVED_TOOLS = {
    computer_mcp.NAME: {t["name"] for t in computer_mcp.TOOLS},
    deck_mcp.NAME: {t["name"] for t in deck_mcp.TOOLS},
    mac_mcp.NAME: {t["name"] for t in mac_mcp.TOOLS},
}


def _all_registry_tools():
    return {t for f in features.FEATURES for t in f.tools}


# ── the detectors: nothing new lands without a line ───────────────────────


def test_every_tool_a_desk_is_given_has_a_registry_entry():
    missing = sorted(f"mcp__{server}__{tool}"
                     for server, tools in SERVED_TOOLS.items()
                     for tool in tools
                     if f"mcp__{server}__{tool}" not in _all_registry_tools())
    assert not missing, (
        f"{missing} are served to desks but no desk is told about them: add "
        f"an entry to server/features.py")


def test_every_route_the_deck_serves_is_a_feature_or_internal():
    from server import app as app_mod
    paths = {getattr(r, "path", "") for r in app_mod.app.routes}
    uncovered = sorted(p for p in paths if p and not features.covers_route(p))
    assert not uncovered, (
        f"{uncovered} are served but are neither a feature in "
        f"server/features.py nor listed in features.INTERNAL_ROUTES")


def test_every_user_facing_flag_is_a_feature_or_internal():
    flags = []
    for section in fields(deckconfig.DeckConfig):
        sub = getattr(deckconfig.DeckConfig(), section.name)
        if not is_dataclass(sub):
            continue
        for f in fields(sub):
            if isinstance(getattr(sub, f.name), bool):
                flags.append(f"{section.name}.{f.name}")
    known = {f.flag for f in features.FEATURES if f.flag} | features.INTERNAL_FLAGS
    assert flags, "the sweep found no flags at all -- it is not looking"
    assert not sorted(set(flags) - known), (
        f"{sorted(set(flags) - known)}: a config switch nothing tells a desk "
        f"about; add it to server/features.py or INTERNAL_FLAGS")


def test_every_tool_a_line_names_exists():
    text = features.section()
    named = set(re.findall(r"mcp__([a-z]+)__([a-z_]+)", text))
    assert named, "the section names no tools at all"
    for server, tool in named:
        assert tool in SERVED_TOOLS.get(server, set()), \
            f"mcp__{server}__{tool} is named to desks but not served"
    for f in features.FEATURES:
        for t in f.tools:
            server, tool = t.split("__")[1:]
            assert tool in SERVED_TOOLS.get(server, set()), t


def test_every_app_button_a_line_names_exists_in_the_mac_app():
    swift = TRAY.read_text() + SIGN_IN_CARD.read_text()
    text = features.section()
    assert "Sign in fresh with passkey" in text
    assert '"Sign in fresh with passkey"' in swift
    assert "Use my Chrome login" in text
    assert 'Use my \\(browser) login' in swift


# ── it is generated from what is enabled ─────────────────────────────────


def test_every_enabled_feature_has_its_line_and_a_disabled_one_does_not(
        monkeypatch):
    text = features.section()
    for f in features.FEATURES:
        if f.enabled():
            assert f.render() in text, f.key
    shared = next(f for f in features.FEATURES if f.flag == "desks.shared_logins")
    monkeypatch.setattr(features, "_flag", lambda path: False)
    assert shared.line not in features.section()
    assert shared.off_line in features.section()


def test_the_groups_cover_what_the_owner_listed():
    text = features.section().lower()
    for word in ("sign in fresh with passkey", "type_password",
                 "allow always", "store_search", "store_install", "mac",
                 "urgent", "ask", "message_desk", "voice", "call",
                 "routine", "hire", "take over", "terminal", "usage",
                 "ntfy", "whatsapp", "vault"):
        assert word in text, word


def test_the_section_is_in_the_brief_and_in_the_versioned_rules(monkeypatch):
    from server import capabilities
    from server.roster import Desk
    desk = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m",
                charter="c", reports_to=None)
    assert features.section() in hire.brief(
        desk, inventory=capabilities.Inventory(), team=[])
    assert features.section() in rules.sections()
    before = rules.version()
    monkeypatch.setattr(features, "_flag", lambda path: False)
    assert rules.version() != before, \
        "turning a feature off must reach running desks as a rules change"


def test_mac_jobs_coming_does_not_read_as_mac_sign_in_coming():
    """MEASURED: Atlas read "His Mac: coming" as the passkey card needing a
    connected Mac too ("until your Mac is connected... take over"). The sign-in
    card works through the Mac app today; only Mac JOBS are pending."""
    jobs = next(f for f in features.FEATURES if f.key == "mac_jobs")
    assert "coming" in jobs.off_line
    assert "sign-in card" in jobs.off_line and "works now" in jobs.off_line


# ── sign-in order: Chrome login, then passkey, then (his choice) takeover ──
#
# MEASURED 2026-10-01, owner's screenshot: Atlas described the passkey route
# and called Take over "the fallback", and music-growth told him "it needs you
# to take over its screen once to confirm a passkey for YouTube Studio".


def _sign_in_line():
    return next(f for f in features.FEATURES if f.key == "mac_sign_in").render()


def test_the_sign_in_line_puts_chrome_login_first_then_passkey_then_takeover():
    line = _sign_in_line()
    line = line[line.index("Every other site"):]
    chrome = line.index("Use my Chrome login")
    passkey = line.index("Sign in fresh with passkey")
    takeover = line.lower().index("take over")
    assert chrome < passkey < takeover, line
    assert "no touch id" in line.lower(), "say why it comes first"
    assert "last resort" in line.lower()


def test_mac_sign_in_is_available_now_and_separate_from_mac_jobs():
    line = _sign_in_line()
    assert "works now" in line.lower()
    assert "not the mac jobs" in line.lower()
    jobs = next(f for f in features.FEATURES if f.key == "mac_jobs")
    assert "coming" in jobs.off_line and "coming" not in line.lower()


def test_the_rules_forbid_asking_him_to_take_over_for_a_sign_in():
    text = "\n".join(rules.lines()).lower()
    assert "never ask him to take over" in text
    assert "fallback" not in text, "takeover is never described as the fallback"


def test_a_non_google_passkey_card_tells_him_to_tap_his_chrome_login_first():
    from server import browser, browser_takeover
    sess = browser.BrowserSession(desk="music-growth", display=":99",
                                  cdp_port=9222, profile_dir="/p", pid=None,
                                  started_at=0.0)
    needs = browser_takeover.passkey_needs(sess, "https://github.com/webauthn")
    assert needs.index("Use my Chrome login") < needs.index(
        "Sign in fresh with passkey")
    assert "Try another way" not in needs


# ── an identity provider's sign-in step with no field is still a sign-in ──
#
# MEASURED 2026-10-01 on music-growth: YouTube Studio sent the desk to
# accounts.google.com/v3/signin/challenge/selection ("choose how to sign in":
# passkey or other ways). Two hidden inputs, no password field, not the
# /challenge/pk path -- so needs_human said None, NO card was raised, and the
# desk told the owner a card was on his Mac when there was none.

GOOGLE_SELECTION = {
    "url": ("https://accounts.google.com/v3/signin/challenge/selection?TL=ADG"
            "&continue=https%3A%2F%2Fstudio.youtube.com"),
    "inputs": [{"type": "hidden", "hidden": True},
               {"type": "hidden", "hidden": True}],
    "frames": [],
}


def test_a_google_sign_in_choice_page_is_a_sign_in():
    from server import browser_takeover
    assert browser_takeover.needs_human(GOOGLE_SELECTION) == "login"
    for url in ("https://accounts.google.com/v3/signin/identifier?flowName=x",
                "https://accounts.google.com/signin/v2/challenge/pwd",
                "https://login.microsoftonline.com/common/oauth2/authorize"):
        assert browser_takeover.needs_human({**GOOGLE_SELECTION, "url": url}) \
            == "login", url


def test_an_ordinary_google_page_is_not_a_sign_in():
    from server import browser_takeover
    for url in ("https://accounts.google.com.evil.example/v3/signin/identifier",
                "https://myaccount.google.com/security",
                "https://www.google.com/search?q=signin"):
        assert browser_takeover.needs_human({**GOOGLE_SELECTION, "url": url}) \
            is None, url


def test_a_sign_in_card_leads_with_his_chrome_login_not_a_takeover():
    """MEASURED 2026-10-01, card 8feh: "... or No. You can also take over the
    screen." -- the one suggestion he does not want on a sign-in."""
    from server import browser, browser_takeover
    sess = browser.BrowserSession(desk="music-growth", display=":99",
                                  cdp_port=9222, profile_dir="/p", pid=None,
                                  started_at=0.0)
    needs = browser_takeover.approve_needs(sess, "login",
                                           "https://app.example.com")
    assert needs.index("Use my Chrome login") < needs.index(
        "Sign in fresh with passkey")
    assert "take over" not in needs.lower()


# ── Mac jobs, live (2026-10-01): available, which Mac, and the grant flow ──


def _mac_line(monkeypatch, nodes, live=True):
    jobs = next(f for f in features.FEATURES if f.key == "mac_jobs")
    monkeypatch.setattr(features, "_mac_nodes", lambda: nodes)
    if not live:
        return jobs.off_line
    return features.mac_jobs_line()


def test_mac_jobs_say_available_name_the_registered_mac_and_the_grant_flow(
        monkeypatch):
    line = _mac_line(monkeypatch, [{"name": "MacBook Pro", "mode": "ask"}])
    low = line.lower()
    assert "available" in low and "coming" not in low
    assert "MacBook Pro" in line and "online" in low
    for word in ("Allow for 1 hour", "Always", "Deny", "Ask me"):
        assert word in line, word
    assert "sandbox" in low and "folders he picked" in low
    for tool in ("mcp__mac__status", "mcp__mac__run"):
        assert tool in line


def test_mac_jobs_with_no_mac_registered_say_so(monkeypatch):
    line = _mac_line(monkeypatch, [])
    assert "available" in line.lower()
    assert "no mac is registered yet" in line.lower()


def test_the_live_section_uses_the_live_mac_line(monkeypatch):
    monkeypatch.setattr(features, "_mac_nodes",
                        lambda: [{"name": "MacBook Pro", "mode": "ask"}])
    monkeypatch.setattr(features, "_mac_jobs_live", lambda: True)
    assert "MacBook Pro" in features.section()
    monkeypatch.setattr(features, "_mac_jobs_live", lambda: False)
    assert "MacBook Pro" not in features.section()


def test_the_upload_and_phone_tools_say_when_to_use_them():
    """LIVE 2026-10-01: a desk dead-ended on Instagram's avatar and posts."""
    entries = [f for f in features.FEATURES
               if {"mcp__computer__upload_file", "mcp__computer__mobile_mode"}
               & set(f.tools)]
    assert entries, "no registry entry names upload_file / mobile_mode"
    text = " ".join(f.line for f in entries)
    assert "Instagram" in text and "mobile_mode" in text and "upload_file" in text
