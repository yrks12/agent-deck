"""The browser guard must not wall a desk off a whole SaaS dashboard.

MEASURED 2026-09-30 on the box: desk atlas could not click anything on
dashboard.vapi.ai -- not the API-keys page, not an OAuth "Authorize" page.
Every Vapi page came back `needs_human == "payment"` and raised a payment card
(fjuq, 4bxj, un6m). A headless load of https://dashboard.vapi.ai/register shows
why: Stripe.js is on the page and injects two iframes on `https://js.stripe.com`
-- `__privateStripeController` and `__privateStripeMetricsController` -- both
`visibility: hidden`, 1px high. There is no card field anywhere. The guard
fired on the mere *presence* of the payment origin in the frame list.

And resolving a card never unlocked anything: fjuq was answered `done` and
4bxj was raised on the same origin three minutes later, because nothing
between the owner's answer and the next `_guard` call remembered it.

Owner ruling: nothing is blocked. A real payment step becomes ONE approve card
("Allow" / "Allow always for this site" / "No"); Allow lets the desk carry on
itself, and Allow always sticks for that desk and site.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import browser, browser_cdp, computer_mcp, desk_computer, handoff
from server import browser_takeover as T
from server import office, sandbox
from server.sources import comms as comms_mod

DESK = "atlas"
TOKEN = "t-secret-not-a-real-credential"

_STRIPE_CTRL = ("https://js.stripe.com/v3/controller-with-preconnect-971e440e8"
                "bcc304b1dd6cde5b2052cef.html#__shared_params__[version]=v3")
_STRIPE_METRICS = ("https://js.stripe.com/v3/m-outer-3437aaddcdf6922d623e172c2"
                   "d6f9278.html#url=https%3A%2F%2Fdashboard.vapi.ai%2Fregister")

#: What the live driver reports on a Vapi dashboard page, with frame geometry.
VAPI_KEYS = {
    "url": "https://dashboard.vapi.ai/org/api-keys",
    "inputs": [{"type": "text", "autocomplete": "", "name": "search",
                "sitekey": "", "disabled": False, "hidden": False}],
    "frames": [
        {"url": _STRIPE_CTRL, "hidden": True, "width": 1280, "height": 1},
        {"url": _STRIPE_METRICS, "hidden": True, "width": 1280, "height": 1},
    ],
}

#: The same page as the pre-fix driver reported it: frame URLs only.
VAPI_KEYS_URL_ONLY = {**VAPI_KEYS, "frames": [{"url": _STRIPE_CTRL},
                                              {"url": _STRIPE_METRICS}]}

CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_live_a1b2c3"
#: A real Stripe Checkout: visible card number / expiry / CVC fields.
STRIPE_CHECKOUT = {
    "url": CHECKOUT_URL,
    "inputs": [
        {"type": "email", "autocomplete": "email", "hidden": False},
        {"type": "text", "autocomplete": "cc-number", "hidden": False},
        {"type": "text", "autocomplete": "cc-exp", "hidden": False},
        {"type": "text", "autocomplete": "cc-csc", "hidden": False},
    ],
    "frames": [{"url": _STRIPE_CTRL, "hidden": True, "width": 1280, "height": 1}],
}


# ── 1. detection: structure that is VISIBLE, never mere presence ───────────


def test_a_vapi_dashboard_with_hidden_stripe_frames_is_not_a_payment():
    assert T.needs_human(VAPI_KEYS) is None


def test_hidden_payment_frames_reported_without_geometry_are_not_a_payment():
    """The exact shape atlas's driver sent: frame URLs, no size, no card field."""
    assert T.needs_human(VAPI_KEYS_URL_ONLY) is None


def test_a_vapi_oauth_authorize_page_is_not_a_payment():
    state = {**VAPI_KEYS,
             "url": "https://dashboard.vapi.ai/auth/cli?redirect_uri=http%3A%2F"
                    "%2Flocalhost%3A39361%2Fcallback",
             "inputs": []}
    assert T.needs_human(state) is None


def test_a_real_checkout_with_visible_card_fields_is_a_payment():
    assert T.needs_human(STRIPE_CHECKOUT) == "payment"


def test_a_visible_stripe_card_element_is_a_payment():
    """Stripe Elements: the card field is a visible, sized cross-origin frame."""
    state = {"url": "https://acme.initech.example/checkout", "inputs": [],
             "frames": [{"url": "https://js.stripe.com/v3/elements-inner-card-"
                                "abc.html", "hidden": False,
                         "width": 420, "height": 40}]}
    assert T.needs_human(state) == "payment"


def test_a_tiny_or_hidden_stripe_frame_is_not_a_payment():
    for frame in ({"hidden": False, "width": 1, "height": 1},
                  {"hidden": False, "width": 0, "height": 0},
                  {"hidden": True, "width": 420, "height": 40}):
        state = {"url": "https://x.example/", "inputs": [],
                 "frames": [{"url": _STRIPE_CTRL, **frame}]}
        assert T.needs_human(state) is None, frame


def test_an_invisible_recaptcha_is_not_a_captcha():
    """reCAPTCHA v3 / invisible v2 sit on login pages everywhere; nothing to solve."""
    state = {"url": "https://zadarma.com/en/registration/",
             "inputs": [{"type": "hidden", "sitekey": "6Lc-abc", "hidden": True},
                        {"type": "", "sitekey": "6Lc-abc", "size": "invisible",
                         "hidden": False}],
             "frames": [{"url": "https://www.google.com/recaptcha/api2/anchor?"
                                "k=6Lc&size=invisible", "hidden": False,
                         "width": 256, "height": 60}]}
    assert T.needs_human(state) is None


def test_a_google_maps_embed_is_not_a_captcha():
    state = {"url": "https://harbor.example/contact", "inputs": [],
             "frames": [{"url": "https://www.google.com/maps/embed?pb=1",
                         "hidden": False, "width": 600, "height": 450}]}
    assert T.needs_human(state) is None


def test_a_visible_checkbox_captcha_is_still_a_captcha():
    state = {"url": "https://x.example/", "inputs": [],
             "frames": [{"url": "https://newassets.hcaptcha.com/captcha/v1/x/"
                                "static/hcaptcha.html#frame=checkbox",
                         "hidden": False, "width": 303, "height": 78}]}
    assert T.needs_human(state) == "captcha"


def test_the_live_driver_reports_frame_visibility_and_size():
    """Without geometry from the driver, no frame can ever count as visible."""
    js = browser_cdp._PAGE_STATE_JS
    frames_part = js[js.index("const frames"):]
    for token in ("width", "height", "hidden"):
        assert token in frames_part, token
    assert "data-size" in js, "an invisible captcha's data-size must be read"


# ── 2 + 3. one approve card; Allow / Allow always unlock the agent ──────────


class FakeDriver:
    def __init__(self, state):
        self.state = state
        self.sess = browser.BrowserSession(
            desk=DESK, display=":99", cdp_port=9222, profile_dir="/p",
            pid=None, started_at=0.0)

    def page_state(self):
        return self.state

    def goto(self, url):
        pass

    def _evaluate(self, js):
        if js == "document.readyState":
            return "complete"
        return {"url": self.state["url"], "title": "Checkout", "text": "Pay"}


@pytest.fixture
def sent(monkeypatch):
    """Every gesture that actually reached the desk's screen."""
    out = []
    monkeypatch.setattr(sandbox, "send_input",
                        lambda desk, action: out.append((desk, action)))
    return out


@pytest.fixture
def store(tmp_path, monkeypatch, sent):
    path = tmp_path / "handoffs.json"
    monkeypatch.setattr(handoff, "DEFAULT_PATH", path)
    monkeypatch.setattr(desk_computer.time, "sleep", lambda s: None)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    return path


def on_page(monkeypatch, state):
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: FakeDriver(state))


def call(name, args=None):
    return computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}}})["result"]


@pytest.fixture
def client(tmp_path, store, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": DESK, "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None}]}))
    from server import app as app_mod
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)
    monkeypatch.setattr(app_mod, "_wake_later", lambda name, reason: None)
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), deliver=app_mod._deliver_or_wake,
        roster_path=roster, prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json", handoffs_path=store)
    surface.refresh()
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_clicking_anywhere_on_vapi_goes_straight_through(
        store, sent, monkeypatch):
    on_page(monkeypatch, VAPI_KEYS_URL_ONLY)
    reply = call("click", {"x": 10, "y": 10})
    assert reply["isError"] is False, reply
    assert sent == [(DESK, {"action": "click", "x": 10, "y": 10})]
    assert handoff.load(store) == []


def test_a_real_payment_raises_one_approve_card_and_allow_lets_the_desk_pay(
        store, sent, client, monkeypatch):
    on_page(monkeypatch, STRIPE_CHECKOUT)

    blocked = call("click", {"x": 5, "y": 5})
    call("click", {"x": 5, "y": 5})
    assert blocked["isError"] is True and sent == []
    cards = handoff.waiting(store)
    assert len(cards) == 1 and cards[0].kind == "payment"
    card = cards[0]
    assert "allow" in card.needs.lower()
    assert "https://checkout.stripe.com" in card.needs

    row = client.get("/v1/handoffs", headers=AUTH).json()["handoffs"][0]
    assert [o["reply"] for o in row["options"]] == ["done", "skipped"]
    assert "allow" in row["options"][0]["summary"].lower()
    assert row["always_option"]["reply"] == "always"

    ok = client.post(f"/v1/handoffs/{card.id}", json={"reply": "done"},
                     headers=AUTH)
    assert ok.status_code == 200, ok.text

    again = call("click", {"x": 5, "y": 5})
    assert again["isError"] is False, again
    assert len(sent) == 1
    assert handoff.waiting(store) == [], "no second card after Allow"
    mail = [json.loads(x) for x in office.MESSAGES_FILE.read_text().splitlines()
            if x]
    told = [m["text"] for m in mail if m.get("to") == DESK]
    assert told and "yourself" in told[-1].lower()


def test_approve_is_accepted_as_the_allow_verb(
        store, sent, client, monkeypatch):
    on_page(monkeypatch, STRIPE_CHECKOUT)
    call("click", {"x": 5, "y": 5})
    card = handoff.waiting(store)[0]
    ok = client.post(f"/v1/handoffs/{card.id}", json={"reply": "approve"},
                     headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert call("click", {"x": 5, "y": 5})["isError"] is False


def test_a_one_time_allow_expires_but_allow_always_survives_a_new_session(
        store, sent, client, monkeypatch):
    on_page(monkeypatch, STRIPE_CHECKOUT)
    call("click", {"x": 5, "y": 5})
    card = handoff.waiting(store)[0]
    ok = client.post(f"/v1/handoffs/{card.id}", json={"reply": "always"},
                     headers=AUTH)
    assert ok.status_code == 200, ok.text

    # A new session, days later: nothing in memory, only what is on disk.
    later = desk_computer.time.time() + 30 * 86400
    monkeypatch.setattr(T.time, "time", lambda: later)
    assert call("click", {"x": 5, "y": 5})["isError"] is False
    assert handoff.waiting(store) == []

    # Scoped: another desk, or another site, is still asked.
    grants = T.grants_path(store)
    assert T.granted(grants, desk="acme", origin="https://checkout.stripe.com",
                     kind="payment") is None
    assert T.granted(grants, desk=DESK, origin="https://pay.example",
                     kind="payment") is None
    assert T.granted(grants, desk=DESK, origin="https://checkout.stripe.com",
                     kind="payment") == "always"


def test_a_plain_allow_does_not_last_for_ever(
        store, sent, client, monkeypatch):
    on_page(monkeypatch, STRIPE_CHECKOUT)
    call("click", {"x": 5, "y": 5})
    card = handoff.waiting(store)[0]
    client.post(f"/v1/handoffs/{card.id}", json={"reply": "done"}, headers=AUTH)
    later = desk_computer.time.time() + 30 * 86400
    monkeypatch.setattr(T.time, "time", lambda: later)
    assert call("click", {"x": 5, "y": 5})["isError"] is True


def test_no_leaves_the_page_guarded(
        store, sent, client, monkeypatch):
    on_page(monkeypatch, STRIPE_CHECKOUT)
    call("click", {"x": 5, "y": 5})
    card = handoff.waiting(store)[0]
    client.post(f"/v1/handoffs/{card.id}", json={"reply": "skipped"},
                headers=AUTH)
    assert call("click", {"x": 5, "y": 5})["isError"] is True
    assert sent == []
