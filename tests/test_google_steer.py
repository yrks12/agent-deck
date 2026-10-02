"""Google can't be copied from his Chrome: Device Bound Session Credentials.

MEASURED (PR #164): Google/YouTube/Gmail sessions in his everyday Chrome are
device-bound, so "Use my Chrome login" copies cookies Google then rejects on a
desk. Only the fresh passkey sign-in works there. Every other site keeps the
Chrome login first.
"""
import pytest

from server.roster import Desk
from server import browser, browser_takeover, capabilities, features, hire

SESS = browser.BrowserSession(desk="atlas", display=":99", cdp_port=9222,
                              profile_dir="/p", pid=None, started_at=0.0)
CHROME = "Use my Chrome login"
FRESH = "Sign in fresh with passkey"

GOOGLE = ["https://accounts.google.com", "https://studio.youtube.com",
          "https://www.youtube.com/watch?v=x", "https://mail.google.com",
          "https://gmail.com", "https://search.google.com", "https://google.com"]
OTHER = ["https://github.com", "https://example.com", "https://notgoogle.com",
         "https://google.com.evil.example"]


@pytest.mark.parametrize("origin", GOOGLE)
def test_google_family_is_device_bound(origin):
    assert browser_takeover.is_dbsc_site(origin)


@pytest.mark.parametrize("origin", OTHER)
def test_other_sites_are_not(origin):
    assert not browser_takeover.is_dbsc_site(origin)


@pytest.mark.parametrize("origin", GOOGLE)
def test_google_card_primary_action_is_passkey(origin):
    needs = browser_takeover.approve_needs(SESS, "login", origin)
    assert needs.startswith(f"Atlas needs to be signed in on {origin}")
    assert needs.index(FRESH) < (needs.index(CHROME) if CHROME in needs else 10**6)
    assert "can't be copied from your Chrome" in needs
    assert f"tap \"{CHROME}\"" not in needs


@pytest.mark.parametrize("origin", OTHER)
def test_non_google_card_primary_action_is_chrome_login(origin):
    needs = browser_takeover.approve_needs(SESS, "login", origin)
    assert needs.index(CHROME) < needs.index(FRESH)


def test_google_passkey_card_leads_with_passkey():
    needs = browser_takeover.passkey_needs(
        SESS, "https://accounts.google.com/v3/signin/challenge/pk")
    assert FRESH in needs and "can't be copied from your Chrome" in needs
    assert CHROME not in needs


def test_non_google_passkey_card_keeps_chrome_first():
    needs = browser_takeover.passkey_needs(SESS, "https://github.com/webauthn")
    assert needs.index(CHROME) < needs.index(FRESH)


def test_capability_text_has_the_google_exception():
    line = next(f for f in features.FEATURES if f.key == "mac_sign_in").render()
    for blob in (line, hire.COMPUTER):
        low = blob.lower()
        assert "google" in low and "youtube" in low and "gmail" in low, blob
        assert "except google" in low or "google can't be copied" in low, blob
    desk = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m",
                label="COS", charter="Run the portfolio.")
    inv = capabilities.Inventory(connected=["Gmail"], needs_auth=["Gmail"])
    text = capabilities.render(inv, desk).lower()
    assert "fresh with passkey" in text and "google" in text
