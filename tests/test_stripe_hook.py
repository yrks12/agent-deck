"""Event wake-ups (Feature C): the Stripe webhook, verified, on its own app.

The webhook app exposes NOTHING but `POST /hooks/stripe`, so a public listener
for it can never reach the board, /api or /v1. Every body is checked against
`Stripe-Signature` (HMAC-SHA256 over "t.payload", 300 s tolerance,
constant-time) before it is parsed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from server import events, events_api, stripe_hook

SECRET = "whsec_test_only"


def _sign(payload: bytes, *, t: int | None = None, secret: str = SECRET) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256)
    return f"t={t},v1={mac.hexdigest()}"


CHARGE = {
    "id": "evt_1", "type": "charge.succeeded",
    "data": {"object": {"id": "ch_1", "amount": 900, "currency": "gbp",
                        "billing_details": {"email": "a@example.com"},
                        "description": "Reading", "metadata": {"site": "acme"}}},
}


def test_a_good_signature_verifies():
    body = json.dumps(CHARGE).encode()
    stripe_hook.verify(body, _sign(body), SECRET, now=time.time())


@pytest.mark.parametrize("header", [
    "", "t=1", "v1=abc", "t=notanumber,v1=abc", "garbage",
])
def test_a_malformed_header_is_refused(header):
    with pytest.raises(stripe_hook.SignatureError):
        stripe_hook.verify(b"{}", header, SECRET, now=time.time())


def test_a_bad_signature_is_refused():
    body = json.dumps(CHARGE).encode()
    with pytest.raises(stripe_hook.SignatureError, match="signature"):
        stripe_hook.verify(body, _sign(body, secret="whsec_other"), SECRET,
                           now=time.time())
    with pytest.raises(stripe_hook.SignatureError):
        stripe_hook.verify(body + b" ", _sign(body), SECRET, now=time.time())


def test_a_stale_signature_is_refused():
    body = json.dumps(CHARGE).encode()
    old = int(time.time()) - 301
    with pytest.raises(stripe_hook.SignatureError, match="tolerance"):
        stripe_hook.verify(body, _sign(body, t=old), SECRET, now=time.time())


def test_a_charge_becomes_a_payment_event():
    ev = stripe_hook.event_from_stripe(CHARGE, now=5.0)
    assert (ev.source, ev.account, ev.kind, ev.ref) == ("stripe", "acme", "payment", "evt_1")
    assert "9.00 GBP" in ev.summary and "a@example.com" in ev.summary


def test_an_unhandled_type_is_ignored():
    assert stripe_hook.event_from_stripe({"id": "evt_2", "type": "customer.created",
                                          "data": {"object": {}}}, now=1.0) is None


@pytest.fixture
def hook(tmp_path):
    store = events.Store(tmp_path / "events")
    store.save_routes([{"source": "stripe", "desk": "acme-lead"}])
    sent: list = []
    hub = events.Hub(store, deliver=lambda d, t: sent.append((d, t)) or "delivered")
    app = stripe_hook.build_app(hub=lambda: hub, secret=lambda: SECRET)
    events_api.THROTTLE.reset()
    return TestClient(app), sent


def test_the_hook_app_serves_only_the_webhook(hook):
    client, _ = hook
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert paths == {"/hooks/stripe"}
    assert client.get("/v1/agents").status_code == 404
    assert client.get("/").status_code == 404


def test_a_signed_payment_reaches_its_desk(hook):
    client, sent = hook
    body = json.dumps(CHARGE).encode()
    r = client.post("/hooks/stripe", content=body,
                    headers={"Stripe-Signature": _sign(body)})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "delivered"
    assert sent[0][0] == "acme-lead"


def test_an_unsigned_or_forged_post_reaches_nobody(hook):
    client, sent = hook
    body = json.dumps(CHARGE).encode()
    assert client.post("/hooks/stripe", content=body).status_code == 400
    forged = _sign(body, secret="whsec_attacker")
    assert client.post("/hooks/stripe", content=body,
                       headers={"Stripe-Signature": forged}).status_code == 400
    assert sent == []


def test_an_oversized_body_is_refused(hook):
    client, sent = hook
    body = b"x" * (stripe_hook.BODY_MAX + 1)
    r = client.post("/hooks/stripe", content=body, headers={"Stripe-Signature": _sign(body)})
    assert r.status_code == 413
    assert sent == []


def test_no_secret_configured_is_503(tmp_path):
    store = events.Store(tmp_path / "e")
    app = stripe_hook.build_app(hub=lambda: events.Hub(store, deliver=lambda d, t: ""),
                                secret=lambda: "")
    r = TestClient(app).post("/hooks/stripe", content=b"{}",
                             headers={"Stripe-Signature": "t=1,v1=00"})
    assert r.status_code == 503


def test_the_fallback_poller_turns_new_stripe_events_into_events():
    calls: list[str] = []

    def fetch(url: str, headers: dict) -> dict:
        calls.append(url)
        assert headers["Authorization"] == "Bearer sk_test_x"
        return {"data": [CHARGE, {"id": "evt_9", "type": "customer.created",
                                  "data": {"object": {}}}]}

    got, newest = stripe_hook.poll_events("sk_test_x", since=100, fetch=fetch, now=5.0)
    assert [e.ref for e in got] == ["evt_1"]
    assert "created%5Bgt%5D=100" in calls[0] or "created[gt]=100" in calls[0]
