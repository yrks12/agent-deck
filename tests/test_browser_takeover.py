"""The take-over loop: when the agent must stop, and what Sam is told.

Written before `server/browser_takeover.py` exists.

Grok Bot hands control back to the human for passwords, passkeys, 2FA,
CAPTCHAs and payment confirmation (spec M10). We copy that list and one thing
they did not do: the decision is made on **structure**, not on page text.

Text matching fails in both directions at once, and both failures are bad:

  * it **misses** the real thing -- a Stripe card field is inside a
    cross-origin iframe whose text this page cannot see at all, so a page that
    is about to take £39 may not contain the word "card" anywhere;
  * it **fires** on an article -- a blog post titled "Why passwords are dead"
    would hand Sam a 2 a.m. handoff for a page with no form on it, and three
    of those teach him to ignore the queue, at which point the real one is
    also ignored.

So every positive here is paired with a negative that carries the same words
and none of the structure. If a change makes `needs_human` read `page_state`
["text"], the "article about passwords" test is what fails.
"""

import pytest

from server import browser as B
from server import handoff as H
from server import browser_takeover as T

FAKE = "sk_live_" + "FAKE51H8sV2qNrPzXk9TdWmB4gY7cQaE0uLjR"  # split: not a real key

PALM_URL = "https://acme.initech.example/checkout"

# What the driver reports for a frame a person can actually see and type into.
# A frame without this (hidden, 1px, or no geometry) is not a signal: see
# tests/test_pay_guard_vapi.py for the measured Vapi case.
SHOWN = {"hidden": False, "width": 420, "height": 40}


def sess(tmp_path=None, desk="acme", display=":99"):
    return B.BrowserSession(
        desk=desk, display=display, cdp_port=9222,
        profile_dir=f"/Users/samcarter/.claude/agent-bus/browser/profiles/{desk}",
        pid=4242, started_at=1788000000.0,
    )


# ── needs_human: the five classes an agent must never attempt ──────────────


def test_a_real_password_field_is_caught():
    """The good signal. `type="password"` is the structural fact; nothing
    about the words on the page is consulted."""
    state = {
        "url": "https://acme.initech.example/signin",
        "inputs": [{"type": "email", "name": "e"},
                   {"type": "password", "name": "p"}],
        "frames": [],
        "text": "Welcome back",
    }
    assert T.needs_human(state) == "login"


def test_a_page_that_merely_talks_about_passwords_is_not_caught():
    """The other direction, and the one text matching gets wrong.

    Every trigger word is on this page. None of the structure is.
    """
    state = {
        "url": "https://blog.example/why-passwords-are-dead",
        "inputs": [{"type": "search", "name": "q"},
                   {"type": "text", "name": "comment"}],
        "frames": [{"origin": "https://www.youtube.com"}],
        "text": ("Your password is the weakest link. Enter your password, "
                 "your credit card number, the CAPTCHA and the verification "
                 "code we sent you -- attackers ask for all of it."),
    }
    assert T.needs_human(state) is None


def test_an_autocomplete_password_field_counts_even_without_type_password():
    """Real sign-in forms use `autocomplete="current-password"` on inputs that
    a framework may render as `type="text"` while a "show password" toggle is
    on. Still a password field."""
    state = {"url": "https://x.example/in",
             "inputs": [{"type": "text", "autocomplete": "current-password"}],
             "frames": []}
    assert T.needs_human(state) == "login"


def test_a_hidden_password_field_alone_does_not_block(  ):
    """Password managers and framework autofill shims put hidden password
    inputs on ordinary pages. Blocking on those is how the queue fills with
    handoffs for pages that have no visible form."""
    hidden = {"url": "https://shop.example/products",
              "inputs": [{"type": "password", "name": "fake", "hidden": True}],
              "frames": []}
    assert T.needs_human(hidden) is None

    # The paired positive: the identical page with the field actually shown.
    shown = {"url": "https://shop.example/products",
             "inputs": [{"type": "password", "name": "fake", "hidden": False}],
             "frames": []}
    assert T.needs_human(shown) == "login"


def test_a_payment_iframe_is_caught_by_its_origin():
    """The measured case: `acme.initech.example` at a "Pay £39" step. The card
    field is inside Stripe's iframe, cross-origin, and its text is not
    readable from the page at all."""
    state = {
        "url": PALM_URL,
        "inputs": [{"type": "email", "name": "receipt"}],
        "frames": [{"origin": "https://js.stripe.com", **SHOWN}],
        "text": "Pay £39 and read my hand",
    }
    assert T.needs_human(state) == "payment"


def test_a_lookalike_payment_origin_is_not_trusted():
    """`https://js.stripe.com.evil.example` contains the real origin as a
    substring. Matching must be on the whole origin, not `in`."""
    state = {"url": "https://evil.example/pay",
             "inputs": [],
             "frames": [{"origin": "https://js.stripe.com.evil.example"}]}
    assert T.needs_human(state) is None


def test_a_card_number_field_is_caught_by_its_autocomplete():
    """Not every checkout uses an iframe; `autocomplete="cc-number"` is the
    structural signal when the card field is first-party."""
    for token in ("cc-number", "cc-csc", "cc-exp"):
        state = {"url": PALM_URL,
                 "inputs": [{"type": "text", "autocomplete": token}],
                 "frames": []}
        assert T.needs_human(state) == "payment", token


def test_a_captcha_frame_is_caught():
    for origin in ("https://www.google.com", "https://hcaptcha.com",
                   "https://challenges.cloudflare.com"):
        state = {"url": "https://x.example/",
                 "inputs": [],
                 "frames": [{"origin": origin, "url": origin + "/recaptcha/api2/anchor",
                             **SHOWN}]}
        assert T.needs_human(state) == "captcha", origin


def test_a_one_time_code_field_is_caught():
    """`autocomplete="one-time-code"` is the web standard for a 2FA box, and
    it is what makes iOS offer the code from Messages."""
    state = {"url": "https://accounts.example/challenge",
             "inputs": [{"type": "text", "autocomplete": "one-time-code"}],
             "frames": []}
    assert T.needs_human(state) == "2fa"


def test_a_browser_interstitial_is_caught():
    """"Confirm it's you" and Chrome's own safe-browsing pages arrive as a
    CDP `Page.interstitialShown`, which is a structural fact about the tab
    rather than anything on the page."""
    state = {"url": "https://accounts.google.com/signin/v2/challenge",
             "inputs": [], "frames": [], "interstitial": True}
    assert T.needs_human(state) == "other"


def test_a_page_with_nothing_on_it_is_not_a_handoff():
    assert T.needs_human({}) is None
    assert T.needs_human({"url": "https://x.example/", "inputs": [],
                          "frames": [], "text": ""}) is None


def test_payment_wins_over_a_login_on_the_same_page():
    """A checkout page carries both. Money is the irreversible half, and the
    brief Sam reads has to be about the charge, not the sign-in."""
    state = {
        "url": PALM_URL,
        "inputs": [{"type": "password", "name": "p"}],
        "frames": [{"origin": "https://js.stripe.com", **SHOWN}],
    }
    assert T.needs_human(state) == "payment"


def test_every_kind_it_returns_is_one_the_handoff_queue_accepts():
    """`needs_human` feeds `raise_handoff`, which rejects an unknown kind."""
    assert set(T.DETECTED_KINDS) <= set(H.KINDS)


# ── the rule scope: surface + origin, never a URL ──────────────────────────


def test_a_block_is_scoped_to_the_browser_surface_and_an_origin():
    """An always-allow rule granted for the browser must not silently
    authorise the same action on the Mac. The surface is what tells them
    apart, so it has to be on the record from the moment the block is raised.
    """
    scope = T.rule_scope(sess(), "payment",
                         PALM_URL + "?session=abc123def456&sig=deadbeefcafe99")

    assert scope["surface"] == T.SURFACE == "browser"
    assert scope["origin"] == "https://acme.initech.example"
    assert scope["desk"] == "acme"
    assert scope["kind"] == "payment"


def test_a_rule_scope_can_never_carry_a_session_id():
    """This dict is the shape a durable rule would be written from. A one-time
    token stored in a rules file every desk reads is permanent, and the rules
    file is not the vault."""
    scope = T.rule_scope(sess(), "login",
                         "https://shop.example/pay?token=abc123def456&u=sam")
    flat = " ".join(str(v) for v in scope.values())
    assert "abc123def456" not in flat
    assert "?" not in flat


# ── takeover_brief: state first, then the ask, then where ──────────────────


def test_the_brief_leads_with_state_and_says_money_has_not_moved():
    """The load-bearing line. Without it the phone says "I need you" and
    nothing about whether £39 has already left, which is how a person ends up
    opening a laptop in a panic over a page that never submitted."""
    brief = T.takeover_brief(sess(), "payment", PALM_URL)

    lines = [ln for ln in brief.splitlines() if ln.strip()]
    assert "acme" in lines[0].lower()
    # State comes before the ask and before the destination.
    state_at = next(i for i, ln in enumerate(lines) if ln.startswith("*State:"))
    assert state_at == 1
    assert "no payment has been made" in brief.lower()
    assert "nothing" in brief.lower() and "submitted" in brief.lower()


def test_the_brief_says_what_to_do_and_where_and_which_screen():
    """He has to be able to act on it without asking a follow-up question."""
    brief = T.takeover_brief(sess(), "payment", PALM_URL)
    assert "acme.initech.example/checkout" in brief
    assert ":99" in brief
    assert "take" in brief.lower() and "over" in brief.lower()
    # The reason he can hand back at all: it is the same browser.
    assert "same browser" in brief.lower() or "stays signed in" in brief.lower()


def test_the_brief_names_the_origin_separately_from_the_url():
    """The origin is the durable, rule-safe half; the URL is the one to click."""
    brief = T.takeover_brief(sess(), "payment", PALM_URL + "?ocid=99")
    assert "https://acme.initech.example" in brief


def test_the_brief_fits_on_a_lock_screen():
    brief = T.takeover_brief(sess(), "payment", PALM_URL)
    assert len(brief) <= H.PHONE_LIMIT


def test_the_brief_redacts_a_key_that_rode_in_on_the_url(tmp_path):
    """A live key in a query string, on its way to WhatsApp. This is the exact
    leak the vault exists to stop, and the browser is the surface most likely
    to produce one."""
    from server import vault
    vault_path = tmp_path / "vault.json"
    vault.put(vault_path, name="STRIPE_LIVE_KEY", value=FAKE,
              description="Live key", grants=("acme",))

    brief = T.takeover_brief(
        sess(), "payment", f"{PALM_URL}?key={FAKE}",
        redact=B.redactor_for(vault_path),
    )
    assert FAKE not in brief
    assert "[redacted STRIPE_LIVE_KEY]" in brief


def test_the_brief_refuses_a_kind_the_queue_would_reject():
    with pytest.raises(ValueError):
        T.takeover_brief(sess(), "vibes", PALM_URL)


# ── resume_brief: what the agent is told when it gets the keyboard back ────


def test_done_orders_a_recheck_and_skipped_orders_abandonment():
    """Collapse these two and a skipped handoff becomes an infinite retry loop
    against a login screen, which is how an account gets locked."""
    done = T.resume_brief(sess(), "done")
    skipped = T.resume_brief(sess(), "skipped")

    assert done != skipped
    assert "verify" in done.lower() or "re-check" in done.lower()
    assert "not assume" in done.lower()
    assert "abandon" in skipped.lower()
    assert "not retry" in skipped.lower() or "do not retry" in skipped.lower()


def test_taken_over_tells_the_agent_to_keep_its_hands_off():
    """While Sam has the keyboard, the agent and the human are on the same
    display. An agent that keeps clicking is typing into his session."""
    text = T.resume_brief(sess(), "taken_over")
    assert "hands off" in text.lower() or "do not touch" in text.lower()
    assert ":99" in text


def test_the_agent_is_told_the_login_it_did_not_do_is_already_there():
    """The single most valuable fact about this design: the human's sign-in
    persists, because it is one Chrome and one profile. An agent that does not
    know that will try to log in again and hit the same wall."""
    for outcome in ("done", "skipped", "taken_over"):
        text = T.resume_brief(sess(), outcome)
        assert "same browser" in text.lower() or "same chrome" in text.lower()


def test_resume_brief_refuses_an_outcome_the_queue_does_not_have():
    with pytest.raises(ValueError):
        T.resume_brief(sess(), "expired")


# ── it is the same queue, not a second one ────────────────────────────────


def test_a_browser_block_lands_in_the_existing_handoff_queue(tmp_path):
    """A second queue means a second place to look, and the one nobody looks
    at is the one with the blocked agent in it."""
    path = tmp_path / "handoffs.json"
    h = T.raise_browser_handoff(path, sess(), kind="payment", url=PALM_URL)

    queued = H.waiting(path)
    assert [q.id for q in queued] == [h.id]
    assert queued[0].agent == "acme"
    assert queued[0].kind == "payment"
    assert "acme.initech.example" in queued[0].where
    assert queued[0].state.strip()
    # And it composes into a phone message through the existing path.
    assert "acme" in H.compose(queued[0]).lower()


def test_a_browser_handoff_records_the_surface_it_happened_on(tmp_path):
    """`Handoff` has no surface column, so the fact rides in `evidence` where
    a reader and the rule engine can both find it. Named, not bridged: see
    docs/the-browser.md."""
    path = tmp_path / "handoffs.json"
    h = T.raise_browser_handoff(path, sess(), kind="payment", url=PALM_URL)
    assert T.SURFACE in h.evidence
    assert "https://acme.initech.example" in h.evidence


def test_a_browser_handoff_never_stores_the_query_string(tmp_path):
    path = tmp_path / "handoffs.json"
    h = T.raise_browser_handoff(
        path, sess(), kind="payment",
        url=PALM_URL + "?session=abc123def456")
    assert "abc123def456" not in h.evidence
