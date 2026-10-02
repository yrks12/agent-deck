"""A long message to an idle desk must start a turn, not wait for a "hello".

MEASURED on the box, 2026-09-30 22:36-22:40. The owner sent Atlas a 1,332
character brief ("Atlas -- Dana-Sam Edits is yours now ..."). Atlas was live
and idle at its composer. Nothing happened. He sent it again at 22:38:15 --
which is the "duplicate" in his thread: two POSTs, from two devices, both
stored. Nothing happened again. Then he typed "hello", and Atlas acted on the
brief at 22:40:26, reading it as hook `additionalContext` on the hello's turn.

THE CAUSE. The live fast path puts `office.attribute(text)` down the session's
socket, and the owner's frame (mark + envelope note + reply shape) adds about
1,166 characters. 1,332 + frame = 2,498 > `manager.MAX_TEXT` (2,000), so
`manager.inject` raised `too_long`, `_try_inject` swallowed it as "no socket",
and the waker saw a live session and did nothing. The record sat queued for a
"next turn" that only a short message could start. Any owner message over
about 834 characters hit this, and so did any long desk-to-desk message
(`deck_mcp._inject_live`, same socket, same limit).

THE GOOD SIGNAL, asserted below: a message too long for the socket still puts
a turn-starting line down it, AND the full text stays queued (unacked) so the
office hook attaches it to exactly that turn -- the path the "hello" proved.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import deck_mcp, manager, office

#: Multi-paragraph and about 1,500 characters: shaped like the brief that stalled.
LONG = "\n\n".join(
    f"{n}. " + ("Free a seat, hire the team, read the handoff first. " * 5).strip()
    for n in range(1, 7))


@pytest.fixture
def bus(short_tmp, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", short_tmp)
    monkeypatch.setattr(office, "MESSAGES_FILE", short_tmp / "messages.jsonl")
    monkeypatch.setattr(office, "OFFICE_FILE", short_tmp / "office.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", short_tmp / "events.jsonl")
    return short_tmp


def _wait_for(received, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received:
            return True
        time.sleep(0.05)
    return False


def _wire(received) -> str:
    return json.loads(received[0].decode().splitlines()[-1])["message"]["content"]


def test_the_brief_really_is_too_long_for_the_socket_once_framed():
    """The premise, pinned: otherwise the tests below prove nothing."""
    assert 1300 < len(LONG) < 1700 and "\n\n" in LONG
    assert len(office.attribute(LONG, "owner")) > manager.MAX_TEXT


def test_a_long_owner_message_to_an_idle_desk_starts_a_turn_now(
        bus, fake_socket, monkeypatch):
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    monkeypatch.setitem(app_mod._state, "sessions", [{
        "session_id": "sid-a", "name": "alpha", "pid": pid,
        "source": "claude", "state": "IDLE", "attention": None}])

    body = TestClient(app_mod.app).post(
        "/api/message", json={"to": "alpha", "text": LONG}).json()

    assert body["ok"] is True
    assert _wait_for(received), (
        "nothing reached the idle session's socket: the long message waits for "
        "a turn that only a short 'hello' will start")
    wire = _wire(received)
    assert len(wire) <= manager.MAX_TEXT
    # The full text is NOT lost and NOT acked: the office hook attaches it to
    # the turn the line above just started.
    assert office.pending_counts().get("alpha") == 1, (
        "the long message was acked on the strength of a nudge; the hook will "
        "never hand the desk its text")
    queued = [json.loads(line) for line in
              (bus / "messages.jsonl").read_text().splitlines() if line.strip()]
    assert any(r.get("text") == LONG for r in queued), "the full text was not kept"


def test_a_short_message_still_goes_down_whole(bus, fake_socket, monkeypatch):
    """Do not over-correct: a message that fits is delivered as itself."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    monkeypatch.setitem(app_mod._state, "sessions", [{
        "session_id": "sid-a", "name": "alpha", "pid": pid,
        "source": "claude", "state": "IDLE", "attention": None}])

    body = TestClient(app_mod.app).post(
        "/api/message", json={"to": "alpha", "text": "hello"}).json()

    assert body["delivered"] is True
    assert _wait_for(received)
    assert _wire(received) == office.attribute("hello", "owner")
    assert office.pending_counts().get("alpha", 0) == 0


def test_a_long_desk_to_desk_message_starts_the_peers_turn_too(
        bus, fake_socket, monkeypatch):
    """The sweep: `deck_mcp._inject_live` is the other door onto the socket."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    (bus / "office.json").write_text(json.dumps({"sessions": {"sid-b": {
        "name": "beta", "pid": pid, "state": "IDLE", "started_at": 1.0,
        "address": f"uds:{sock_dir}/{pid}.sock"}}}))

    text = office.attribute(LONG + "\n\n" + LONG, "alpha")
    assert len(text) > manager.MAX_TEXT
    delivered = deck_mcp._inject_live("beta", text)

    assert delivered is False, "only the text itself counts as delivered"
    assert _wait_for(received), "the idle peer was never given a turn"
    assert len(_wire(received)) <= manager.MAX_TEXT


def test_the_nudge_fits_and_says_why(bus):
    assert 0 < len(manager.NUDGE) <= manager.MAX_TEXT
    assert "Agent Deck" in manager.NUDGE
