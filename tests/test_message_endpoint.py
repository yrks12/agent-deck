"""What /api/message promises about delivery.

A message to a live session must arrive exactly once. The endpoint injects it
into the session's socket for an immediate reply, but the office hook is also
watching the queue -- so unless the endpoint acks the queued record, the same
text lands a second time on that session's next turn. These tests pin the ack,
because a double-delivered message is the failure the deck actually produced.

Everything asserts the *presence* of the good signal: the frame on the socket,
the ack record in the queue, the delivered flag on the response.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import manager
from server import office
from server import opencode_sock


@pytest.fixture
def bus(short_tmp, monkeypatch):
    """Point the office queue at a throwaway file."""
    messages = short_tmp / "messages.jsonl"
    monkeypatch.setattr(office, "BUS_DIR", short_tmp)
    monkeypatch.setattr(office, "MESSAGES_FILE", messages)
    return messages


@pytest.fixture
def client(bus):
    return TestClient(app_mod.app)


def _records(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _wait_for(received, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received:
            return True
        time.sleep(0.05)
    return False


def _seat(monkeypatch, *, pid, name="alpha", session_id="sid-a", source="claude"):
    monkeypatch.setitem(
        app_mod._state,
        "sessions",
        [{"session_id": session_id, "name": name, "pid": pid, "source": source}],
    )


# --- the good signal: injected AND acked -------------------------------------

def test_live_session_gets_the_frame_on_its_socket(client, fake_socket, monkeypatch):
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid)

    res = client.post("/api/message", json={"to": "alpha", "text": "ship it"})

    assert res.status_code == 200
    assert res.json()["delivered"] is True
    assert _wait_for(received), "nothing reached the session socket"
    frame = json.loads(received[0].decode())
    # His words, under his mark. This route is him typing at the deck's own
    # board, and the socket skips `hooks/cc-office.js` -- so the frame that
    # says who sent it has to be on the wire itself, or the session reads an
    # unattributed line and (measured, on the box) treats it as a peer relay.
    assert frame["message"]["content"] == office.attribute("ship it", "owner")
    assert "ship it" in frame["message"]["content"]


def test_injected_message_is_acked_so_the_hook_will_not_resend(
    client, bus, fake_socket, monkeypatch
):
    """The regression that mattered: delivered once, not twice."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid)

    client.post("/api/message", json={"to": "alpha", "text": "ship it"})
    assert _wait_for(received)

    recs = _records(bus)
    queued = [r for r in recs if r.get("id")]
    acked = {r["ack"] for r in recs if r.get("ack")}
    assert len(queued) == 1, "the message should still be recorded for the graph"
    assert queued[0]["id"] in acked, "injected message was left for the hook to resend"
    assert office.pending_counts().get("alpha", 0) == 0


def test_session_addressable_by_session_id_too(client, bus, fake_socket, monkeypatch):
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid, session_id="sid-a", name="alpha")

    res = client.post("/api/message", json={"to": "sid-a", "text": "ship it"})

    assert res.json()["delivered"] is True
    assert _wait_for(received)


# --- the class: opencode, broadcast, no socket, bad input --------------------

def test_opencode_session_is_injected_through_its_proxy(client, bus, monkeypatch):
    """OpenCode has no Claude Code socket; it must still be reached."""
    sent = {}

    def fake_inject(pid, text):
        sent["pid"] = pid
        sent["text"] = text
        return {"ok": True}

    monkeypatch.setattr(opencode_sock, "inject", fake_inject)
    monkeypatch.setattr(manager, "sock_dir", lambda: None)
    _seat(monkeypatch, pid=999001, name="oc", session_id="sid-oc", source="opencode")

    res = client.post("/api/message", json={"to": "oc", "text": "hello"})

    assert res.json()["delivered"] is True
    # The proxy carries the mark too: an OpenCode desk is no less entitled to
    # know it is the owner speaking than a Claude Code one.
    assert sent == {"pid": 999001, "text": office.attribute("hello", "owner")}
    recs = _records(bus)
    acked = {r["ack"] for r in recs if r.get("ack")}
    assert [r for r in recs if r.get("id")][0]["id"] in acked


def test_broadcast_stays_queued_for_the_hook(client, bus, fake_socket, monkeypatch):
    """`*` reaches everyone via the hook; injecting would hit one session only."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid)

    res = client.post("/api/message", json={"to": "*", "text": "standup"})

    assert res.status_code == 200
    assert res.json().get("delivered") is not True
    assert office.pending_counts().get("*") == 1
    assert not received, "a broadcast must not be injected into one session"


def test_try_inject_refuses_a_broadcast_outright(monkeypatch):
    """Pins the guard itself: even a session literally named `*` is not injected."""
    called = []
    monkeypatch.setattr(manager, "inject", lambda pid, text: called.append(pid))
    monkeypatch.setitem(
        app_mod._state, "sessions", [{"session_id": "sid-x", "name": "*", "pid": 1234}]
    )

    assert app_mod._try_inject("*", "standup") is False
    assert called == []


def test_unreachable_session_falls_back_to_the_queue(client, bus, short_tmp, monkeypatch):
    """No socket is not an error -- the hook delivers it on the next turn."""
    monkeypatch.setattr(manager, "sock_dir", lambda: short_tmp / "empty-socks")
    _seat(monkeypatch, pid=999002)

    res = client.post("/api/message", json={"to": "alpha", "text": "later"})

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json().get("delivered") is not True
    assert office.pending_counts().get("alpha") == 1


def test_unknown_recipient_stays_queued(client, bus, monkeypatch):
    monkeypatch.setitem(app_mod._state, "sessions", [])

    res = client.post("/api/message", json={"to": "ghost", "text": "hi"})

    assert res.status_code == 200
    assert res.json().get("delivered") is not True
    assert office.pending_counts().get("ghost") == 1


@pytest.mark.parametrize(
    "payload", [{"to": "", "text": "hi"}, {"to": "alpha", "text": "  "}, {}]
)
def test_missing_fields_are_refused_before_anything_is_queued(client, bus, payload):
    res = client.post("/api/message", json=payload)

    assert res.status_code == 400
    assert _records(bus) == []
