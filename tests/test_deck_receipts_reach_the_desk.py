"""The deck's receipts to a desk must reach it, not sit on the queue.

MEASURED on the box, 2026-10-01: deckdoctor's `stuck_messages` was red with two
messages from `deck`, unread for 60 and 80 minutes:

    music-ops      'Your routines in the deck:\\n(none)'          (desk OFFLINE)
    channel-watch  'Deleted routine 41cc549bf57e. ...'           (desk IDLE)

Both are replies to a `YOS_ROUTINE` line. The harvester reads that line AFTER the
desk's turn has ended, then `office.send`s the answer onto the queue -- and
nothing else. No inject into the live session, no wake of a sleeping one. A
queued record is only read at the start of the desk's next turn, and an idle
desk has none, so the answer never arrives. Every other deck-to-desk path
(owner messages, routines, the Mac bridge's `_mac_tell`) queues AND delivers.

The fix: the harvester takes a `tell` callable; the daemon hands it
`app._deck_tell`, which queues, then injects or wakes, and acks what it carried.
"""
from __future__ import annotations

import inspect
import json

import pytest

from server import app as app_mod
from server import harvest, office
from server.harvest import Harvester
from server.roster import Desk, save_roster

ATLAS = "atlas"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    save_roster(tmp_path / "roster.json", [Desk(name=ATLAS, cwd="/tmp", engine="claude",
                                                mission="m", label="COS", charter="c",
                                                reports_to=None)])
    return tmp_path


class _Routines:
    def routine_rows(self):
        return []


def _said(text):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}]}}) + "\n"


def test_a_routine_receipt_goes_through_tell_not_only_onto_the_queue(bus):
    told = []
    h = Harvester(bus / "roster.json", bus / "off.json", routines=_Routines(),
                  tell=lambda name, text: told.append((name, text)))
    transcript = bus / "t.jsonl"
    transcript.write_text(_said('YOS_ROUTINE {"list": true}'))
    h.poll([{"session_id": "sid-a", "name": ATLAS, "transcript": str(transcript)}])
    assert told and told[0][0] == ATLAS
    assert told[0][1].startswith("Your routines in the deck:")


def test_without_tell_the_harvester_still_queues(bus):
    h = Harvester(bus / "roster.json", bus / "off.json", routines=_Routines())
    transcript = bus / "t.jsonl"
    transcript.write_text(_said('YOS_ROUTINE {"list": true}'))
    h.poll([{"session_id": "sid-a", "name": ATLAS, "transcript": str(transcript)}])
    rows = [json.loads(r) for r in (bus / "messages.jsonl").read_text().splitlines() if r]
    assert any(r.get("to") == ATLAS and r.get("from") == "deck" for r in rows)


def test_deck_tell_queues_then_delivers_and_acks_what_it_carried(bus, monkeypatch):
    carried = []
    monkeypatch.setattr(app_mod, "_deliver_or_wake",
                        lambda target, text, reason="": carried.append((target, reason)) or True)
    assert app_mod._deck_tell(ATLAS, "Deleted routine r1.") is True
    assert carried == [(ATLAS, "deck_receipt")]
    lines = [json.loads(r) for r in (bus / "messages.jsonl").read_text().splitlines() if r]
    sent = [r for r in lines if r.get("to") == ATLAS]
    assert sent and any(r.get("ack") == sent[0]["id"] for r in lines)


def test_deck_tell_leaves_it_queued_when_only_a_wake_was_possible(bus, monkeypatch):
    monkeypatch.setattr(app_mod, "_deliver_or_wake", lambda target, text, reason="": False)
    assert app_mod._deck_tell(ATLAS, "Deleted routine r1.") is True
    lines = [json.loads(r) for r in (bus / "messages.jsonl").read_text().splitlines() if r]
    assert not any(r.get("ack") for r in lines), "a wake carries it and acks it, not us"


def test_the_daemon_hands_the_harvester_deck_tell():
    src = inspect.getsource(app_mod)
    start = src.index("harvest_mod.Harvester(")
    assert "tell=_deck_tell" in src[start:start + 900]


def test_the_wake_reason_deck_tell_uses_is_one_the_waker_accepts(bus, monkeypatch):
    # Deployed 2026-10-01 with reason "deck_receipt" missing from wake.REASONS:
    # Waker.ensure_awake raised ValueError, so a SLEEPING desk was never woken
    # (only a live socket got the receipt). The mock above hid it.
    from server import wake
    reasons = []
    monkeypatch.setattr(app_mod, "_deliver_or_wake",
                        lambda target, text, reason="": reasons.append(reason) or False)
    app_mod._deck_tell(ATLAS, "Deleted routine r1.")
    assert reasons and all(r in wake.REASONS for r in reasons), reasons
