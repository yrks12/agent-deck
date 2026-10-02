"""Detector 1: `delivered: true` must mean a human can read it.

THE LIE. `app._try_inject` returns True the moment `manager.inject()` does not
raise. Raising is a property of the *socket*, not of the *screen* -- so with a
permission modal drawn, the bytes go down the socket, land in the composer
UNDERNEATH the dialog where nothing will ever submit them, and `/api/message`
answers `delivered: true`. The deck states as fact something it never
established. A confident wrong answer is worse than silence here, because it
stops anyone looking for the message again.

THE SWEEP, not the one state. `attention.kind` is a closed set, and it is
closed in exactly one place: `sources/bus.ATTENTION_TYPES`. Today that is
`permission_prompt` and `agent_needs_input`; the collector copies the kind
through verbatim (`collector.py:199-202`) and can invent no other. Both mean the
same geometry -- Claude Code has a dialog up and the dialog owns the keyboard --
which is *why* both are in that set and why `idle_prompt` is deliberately not.
So the rule is the class, not the member: an injected message is unreadable
whenever the card carries attention at all, and
`test_every_attention_kind_the_collector_can_emit_is_covered` fails if a third
kind is ever added without someone deciding about it.

THE GOOD SIGNAL, asserted below, is three things together and not the absence of
an error:

  * `delivered: false` -- which `docs/client-api.md` already defines as "queued,
    not lost", so no new vocabulary is needed;
  * the message still in the office queue, unacked, which is what makes the hook
    deliver it on the session's next turn -- when the dialog is gone;
  * nothing on the socket. Injecting anyway and merely reporting false would put
    a copy in the dead composer AND a copy through the hook: the owner gets the
    same message twice, once as leftover text he has to delete.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import manager
from server import office
from server.sources import bus as bus_mod


@pytest.fixture
def bus(short_tmp, monkeypatch):
    """Office queue and event ledger, both under a throwaway dir."""
    monkeypatch.setattr(office, "BUS_DIR", short_tmp)
    monkeypatch.setattr(office, "MESSAGES_FILE", short_tmp / "messages.jsonl")
    monkeypatch.setattr(app_mod, "BUS_FILE", short_tmp / "events.jsonl")
    return short_tmp


@pytest.fixture
def client(bus):
    return TestClient(app_mod.app)


def _seat(monkeypatch, *, pid, attention=None, name="alpha"):
    """One live card, optionally sitting on a dialog."""
    monkeypatch.setitem(app_mod._state, "sessions", [{
        "session_id": "sid-a", "name": name, "pid": pid, "source": "claude",
        "state": "NEEDS_YOU" if attention else "IDLE",
        "attention": attention,
    }])


def _attention(kind):
    return {"kind": kind, "message": f"Claude needs your permission ({kind})",
            "since": 1_756_000_000.0}


def _wait_for(received, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received:
            return True
        time.sleep(0.05)
    return False


# -- the lie ------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(bus_mod.ATTENTION_TYPES))
def test_a_session_sitting_on_a_dialog_is_never_reported_delivered(
    client, bus, fake_socket, monkeypatch, kind
):
    """The whole point. A live socket is not a readable screen."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid, attention=_attention(kind))

    body = client.post("/api/message",
                       json={"to": "alpha", "text": "ship it"}).json()

    assert body["ok"] is True
    assert body.get("delivered") is not True, (
        f"the deck claimed delivery to a session on a {kind} dialog; the text "
        "is in the composer under the modal and nobody will submit it")


@pytest.mark.parametrize("kind", sorted(bus_mod.ATTENTION_TYPES))
def test_the_undeliverable_message_is_still_queued_for_the_next_turn(
    client, bus, fake_socket, monkeypatch, kind
):
    """`delivered: false` is only honest if it means "queued, not lost"."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid, attention=_attention(kind))

    client.post("/api/message", json={"to": "alpha", "text": "ship it"})

    assert office.pending_counts().get("alpha") == 1, (
        "the message was acked or never queued -- the office hook will not "
        "deliver it on the session's next turn, so it really is lost")


@pytest.mark.parametrize("kind", sorted(bus_mod.ATTENTION_TYPES))
def test_nothing_is_written_into_the_composer_under_the_modal(
    client, bus, fake_socket, monkeypatch, kind
):
    """One delivery, not two. Bytes now + hook later = the owner reads it twice
    and deletes stale text out of his composer by hand."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid, attention=_attention(kind))

    client.post("/api/message", json={"to": "alpha", "text": "ship it"})

    assert not _wait_for(received, timeout=0.6), (
        "bytes went into the composer behind the dialog; the hook will deliver "
        "the same text again next turn")


# -- the sweep ----------------------------------------------------------------


def test_every_attention_kind_the_collector_can_emit_is_covered():
    """`attention.kind` is a closed set with exactly one definition site.

    This is the guard on the CLASS. A new kind added to `ATTENTION_TYPES` gets a
    decision made about it here rather than silently inheriting `delivered:
    true`, which is how this bug shipped in the first place.
    """
    assert bus_mod.ATTENTION_TYPES == {"permission_prompt", "agent_needs_input"}, (
        "the set of attention kinds changed. Decide, for the new one, whether "
        "an injected message could actually be read in that state, then widen "
        "this assertion and the parametrised cases above.")
    assert "idle_prompt" not in bus_mod.ATTENTION_TYPES, (
        "idle_prompt is a session at its own prompt with no dialog -- if it "
        "ever becomes attention, injection stops working for ordinary sessions")


# -- the other half: do not break delivery that does work ---------------------


def test_a_session_with_no_dialog_is_still_delivered_now(
    client, bus, fake_socket, monkeypatch
):
    """The failure mode of over-correcting. A card with `attention: null` is at
    its own composer and the injection is genuinely read -- it must still say
    so, and still ack, or every message on the deck routes the slow way."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    _seat(monkeypatch, pid=pid, attention=None)

    body = client.post("/api/message",
                       json={"to": "alpha", "text": "ship it"}).json()

    assert body["delivered"] is True
    assert _wait_for(received), "nothing reached the session socket"
    wire = json.loads(received[0].decode())["message"]["content"]
    # Under his mark, since the socket skips the framing hook. The words are
    # asserted separately: a frame delivered without the message it frames
    # would be this defect wearing the fix's clothes.
    assert wire == office.attribute("ship it", "owner")
    assert "ship it" in wire
    assert office.pending_counts().get("alpha", 0) == 0, "delivered but unacked"
