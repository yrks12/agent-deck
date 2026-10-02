"""Event wake-ups (Feature C): `/v1/events` on the REAL app, and the wake.

The inbound door takes a per-deck events token (separate from the owner's
token, so a website holding it can post events and read nothing) or normal
deck auth. It is capped and rate-limited. A routed event for a desk with no
live socket must wake that desk with reason `event` -- not sit on a queue.
"""

from __future__ import annotations

import os
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import events, events_api, office, wake
from server.roster import Desk

TOKEN = "deck-token"
EV_TOKEN = "events-token-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EV_AUTH = {"Authorization": f"Bearer {EV_TOKEN}"}
EVENT = {"source": "signup", "account": "example-site", "kind": "signup",
         "summary": "new sign-up: a@b.c", "ref": "u-1"}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(events_api.TOKEN_ENV, EV_TOKEN)
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "bus.jsonl")
    store = events.Store(tmp_path / "events")
    store.save_routes([{"source": "signup", "desk": "bravo-growth"}])
    sent: list[tuple[str, str]] = []
    hub = events.Hub(store, deliver=lambda d, t: sent.append((d, t)) or "delivered")
    monkeypatch.setattr(app_mod, "_event_hub", hub)
    events_api.THROTTLE.reset()
    return TestClient(app_mod.app), sent, store


def test_posting_needs_a_token_and_refuses_a_wrong_one(setup):
    client, sent, _ = setup
    assert client.post("/v1/events", json=EVENT).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/events", json=EVENT, headers=bad).status_code == 401
    assert sent == []


def test_the_events_token_posts_and_the_desk_gets_it(setup):
    client, sent, _ = setup
    r = client.post("/v1/events", json=EVENT, headers=EV_AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"ok": True, "state": "delivered",
                                    "desk": "bravo-growth", "ref": "u-1"}]
    assert sent[0][0] == "bravo-growth" and "new sign-up: a@b.c" in sent[0][1]


def test_normal_deck_auth_also_posts(setup):
    client, sent, _ = setup
    assert client.post("/v1/events", json=EVENT, headers=AUTH).status_code == 200
    assert len(sent) == 1


def test_the_events_token_cannot_read_anything(setup):
    client, _, _ = setup
    assert client.get("/v1/events", headers=EV_AUTH).status_code == 401
    assert client.get("/v1/events/routes", headers=EV_AUTH).status_code == 401
    assert client.get("/v1/agents", headers=EV_AUTH).status_code == 401


def test_a_body_over_the_cap_is_refused(setup):
    client, sent, _ = setup
    big = {**EVENT, "summary": "x" * (events_api.BODY_MAX + 10)}
    r = client.post("/v1/events", json=big, headers=EV_AUTH)
    assert r.status_code == 413
    assert sent == []


def test_a_malformed_event_says_why(setup):
    client, _, _ = setup
    r = client.post("/v1/events", json={"source": "signup"}, headers=EV_AUTH)
    assert r.status_code == 400
    assert r.json()["reason"] == "bad_event"


def test_a_caller_over_its_rate_is_throttled(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.setattr(events_api.THROTTLE, "limit", 2)
    codes = [client.post("/v1/events", json={**EVENT, "ref": f"r{i}"},
                         headers=EV_AUTH).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_recent_events_and_the_routing_table_are_readable_and_editable(setup):
    client, _, _ = setup
    client.post("/v1/events", json=EVENT, headers=EV_AUTH)
    assert client.get("/v1/events").status_code == 401
    rows = client.get("/v1/events", headers=AUTH).json()["events"]
    assert rows[0]["ref"] == "u-1" and rows[0]["desk"] == "bravo-growth"
    assert rows[0]["state"] == "delivered"
    new = [{"source": "stripe", "account": "acme*", "desk": "acme-lead"}]
    r = client.put("/v1/events/routes", json={"routes": new}, headers=AUTH)
    assert r.status_code == 200
    got = client.get("/v1/events/routes", headers=AUTH).json()["routes"]
    assert got == [{"source": "stripe", "account": "acme*", "kind": "*",
                    "desk": "acme-lead"}]
    bad = client.put("/v1/events/routes", json={"routes": [{"source": "x"}]},
                     headers=AUTH)
    assert bad.status_code == 400


def test_event_is_a_wake_reason():
    assert "event" in wake.REASONS


def test_a_sleeping_desk_is_woken_with_reason_event(monkeypatch):
    queued: list[tuple] = []
    monkeypatch.setattr(office, "send", lambda to, text, sender="", **k:
                        queued.append((to, sender)) or {"ok": True, "id": "q1"})
    monkeypatch.setattr(app_mod, "_try_inject", lambda target, text: False)
    monkeypatch.setattr(app_mod, "_find_desk",
                        lambda name: Desk(name=name, cwd="/srv/w", engine="claude", mission=""))
    woke = threading.Event()
    reasons: list[str] = []

    class FakeWaker:
        def ensure_awake(self, name, *, reason):
            reasons.append(reason)
            woke.set()

    monkeypatch.setattr(app_mod, "_waker", FakeWaker())
    state = app_mod._deliver_event("acme-lead", "1 event arrived")
    assert woke.wait(5)
    assert reasons == ["event"]
    assert queued == [("acme-lead", office.EVENT)]
    assert state == "waking"


def test_a_live_desk_gets_it_on_its_socket_and_the_queue_is_acked(monkeypatch):
    acked: list[str] = []
    monkeypatch.setattr(office, "send", lambda *a, **k: {"ok": True, "id": "q2"})
    monkeypatch.setattr(office, "ack", lambda i: acked.append(i))
    monkeypatch.setattr(app_mod, "_find_desk", lambda name: Desk(name=name, cwd="/w", engine="claude", mission=""))
    framed: list[str] = []
    monkeypatch.setattr(app_mod, "_try_inject",
                        lambda target, text: framed.append(text) or True)
    assert app_mod._deliver_event("acme-lead", "paid") == "delivered"
    assert acked == ["q2"]
    assert framed[0].startswith(office.EVENT_MARK)


def test_an_unknown_desk_is_a_failed_delivery_not_a_queue(monkeypatch):
    monkeypatch.setattr(app_mod, "_find_desk", lambda name: None)
    sent: list = []
    monkeypatch.setattr(office, "send", lambda *a, **k: sent.append(a) or {"ok": True})
    assert app_mod._deliver_event("ghost", "x").startswith("failed")
    assert sent == []


def test_the_events_token_is_generated_once_owner_only(monkeypatch, tmp_path):
    monkeypatch.delenv(events_api.TOKEN_ENV, raising=False)
    path = tmp_path / "events-token.txt"
    assert events_api.events_token(path) == ""      # a request never makes one
    assert not path.exists()
    first = events_api.events_token(path, create=True)
    assert len(first) >= 32
    assert events_api.events_token(path) == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
