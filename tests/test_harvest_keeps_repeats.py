"""A woken desk that says "pong" twice has said it twice.

Wakes keep the same session id, so identical short replies ("ok", "done",
"pong") recur in one transcript. Dedupe keyed on the reply text dropped every
one after the first; it must key on the transcript entry (uuid), so a real
replay of the same entry is still dropped but a new entry with the same words
is not.
"""

import json

from server import office
from server.harvest import Harvester
from server.roster import Desk, save_roster


def _turn(uuid, text):
    return json.dumps({
        "type": "assistant", "uuid": uuid,
        "message": {"role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path / "bus")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "bus" / "messages.jsonl")
    roster = tmp_path / "roster.json"
    save_roster(roster, [Desk(name="acme", cwd="/tmp", engine="claude", mission="",
                              label="", charter="", reports_to=None)])
    transcript = tmp_path / "s1.jsonl"
    card = {"session_id": "s1", "name": "acme", "cwd": "/tmp",
            "state": "WORKING", "transcript": str(transcript)}
    return Harvester(roster, tmp_path / "offsets.json"), transcript, card


def _said(applied):
    return [r for r in applied if r.get("kind") == "said"]


def test_a_repeated_identical_reply_from_a_new_entry_is_posted(tmp_path, monkeypatch):
    h, transcript, card = _setup(tmp_path, monkeypatch)
    transcript.write_text(_turn("u1", "pong"))
    assert len(_said(h.poll([card]))) == 1
    with transcript.open("a") as fh:
        fh.write(_turn("u2", "pong"))
    assert len(_said(h.poll([card]))) == 1, "second identical 'pong' was swallowed"


def test_the_same_entry_replayed_is_still_dropped(tmp_path, monkeypatch):
    h, transcript, card = _setup(tmp_path, monkeypatch)
    transcript.write_text(_turn("u1", "pong"))
    assert len(_said(h.poll([card]))) == 1
    with transcript.open("a") as fh:
        fh.write(_turn("u1", "pong"))
    assert _said(h.poll([card])) == []
