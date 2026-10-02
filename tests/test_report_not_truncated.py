"""An agent's report must reach the owner whole, or say out loud that it did not.

The defect these pin, measured on a live run. A hired agent surveyed every git
branch across the owner's repos and wrote a 6,047-character report. He received
**2,000 characters of it**, cut mid-sentence in the middle of a markdown table,
with no ellipsis, no flag on the record and no event anywhere. The 4,047
characters he lost carried the single most valuable sentence in the survey --
that six of the stalled branches are not abandoned but blocked on decisions he
is holding.

The cause is one expression: `server/office.py`'s `send()` does `text[:2000]`,
and 2000 is `server/manager.py`'s `MAX_TEXT` -- the byte ceiling Claude Code's
messaging socket enforces on a message *injected into* a session. That is a real
limit and it belongs on that path. It does not belong here:

* `harvest._say` is documented as "a record of something already said". Nothing
  is ever injected from it, so no socket ever sees those bytes.
* Even owner->agent never benefited. `app._try_inject` and `Surface.send` both
  hand `manager.inject` the ORIGINAL text, not the clipped record, and
  `manager.inject` raises `InjectError("too_long")` itself. So the clip has
  never once protected a socket; it has only ever damaged the ledger.

What the fix must get right, and what each test here holds it to:

1. **The whole report arrives.** Not "no error" -- the last sentence of a
   6,047-character report, read back out of the API, in the desk's own thread.
2. **A cut, if one ever happens, is visible.** There is still a ceiling, because
   `hooks/cc-office.js` reads only the last 256 KB of this ledger and one
   runaway record would silently push every other message out of that window.
   But the ceiling is this path's own (`office.RECORD_MAX`), it is ten times the
   largest report ever measured, and crossing it stamps a sentence into the text
   AND a `truncated` count onto the record.
3. **The socket keeps its own limit.** Deleting the clip must not turn into
   deleting the real ceiling: `manager.inject` must still refuse 2001 bytes.

Hermetic: bus, queue and transcript all under tmp_path.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import manager as manager_mod
from server import office
from server.harvest import Harvester
from server.roster import Desk, save_roster
from server.sources import comms as comms_mod

TOKEN = "t-not-a-real-credential"
SESSION = "sid-survey"
DESK = "branch-survey"

#: The sentence he lost. Deliberately the LAST thing in the report, because a
#: truncation only ever eats the end -- a check against the opening line passes
#: on a message that was cut and is therefore not a detector at all.
THE_LOST_SENTENCE = (
    "Six of these are not abandoned: they are blocked on a decision only you "
    "can make, and they are listed above in the order I would unblock them."
)


def long_report(total=6047):
    """A report the shape of the real one: a table, then the finding.

    Padded with table rows rather than a repeated character so that a naive
    "did some text arrive" assertion cannot pass by accident, and sized to the
    measured 6,047 characters of the live survey.
    """
    head = "# Branch survey\n\n| repo | branch | last commit | state |\n"
    tail = "\n\n" + THE_LOST_SENTENCE
    rows = []
    n = 0
    while len(head) + len("".join(rows)) + len(tail) < total:
        n += 1
        rows.append(f"| repo-{n:03d} | slice/thing-{n:03d} | 2026-0{n % 9 + 1}-1"
                    f"{n % 9} | stalled, waiting on you |\n")
    return head + "".join(rows) + tail


REPORT = long_report()


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_path(bus, tmp_path):
    path = bus / "roster.json"
    save_roster(path, [Desk(name=DESK, cwd=str(tmp_path / "work"),
                            engine="claude", mission="Survey the branches.",
                            label="Survey", charter="You own branch rot.",
                            reports_to=None)])
    return path


@pytest.fixture
def transcript(tmp_path):
    return tmp_path / "transcript.jsonl"


@pytest.fixture
def sessions(transcript):
    return [{"session_id": SESSION, "name": DESK, "pid": 4242,
             "cwd": "/tmp/work", "project": "work", "state": "WORKING",
             "state_since": 1_756_000_000.0,
             "transcript": str(transcript)}]


@pytest.fixture
def harvester(roster_path, tmp_path):
    return Harvester(roster_path, tmp_path / "harvest-offsets.json")


@pytest.fixture
def surface(roster_path, bus, sessions):
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": sessions},
        comms=comms_mod.CommsIndex(),
        roster_path=roster_path,
        prefs_path=bus / "agent_prefs.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def assistant(text):
    return {
        "type": "assistant", "uuid": "u-1", "sessionId": SESSION,
        "isSidechain": False, "timestamp": "2026-09-01T15:11:00.000Z",
        "message": {"id": "msg-1", "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}]},
    }


def queued(bus):
    records = []
    for line in (bus / "messages.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not record.get("ack"):
            records.append(record)
    return records


# -- 1. the whole report arrives ---------------------------------------------

def test_the_report_reaches_the_owners_thread_whole(
    transcript, harvester, surface, sessions, client, bus
):
    """THE detector. The finding he lost must be readable in his own thread."""
    transcript.write_text(json.dumps(assistant(REPORT)) + "\n")
    harvester.poll(sessions)
    surface.refresh()

    res = client.get(f"/v1/threads/direct:{DESK}/messages", headers=auth())
    assert res.status_code == 200, res.text
    said = [m for m in res.json()["messages"] if m["author"] == DESK]
    assert len(said) == 1, f"the report is not in the thread: {said!r}"

    text = said[0]["text"]
    assert THE_LOST_SENTENCE in text, (
        f"the report was cut: {len(text)} of {len(REPORT)} characters arrived, "
        f"ending {text[-80:]!r}. The finding he needed is not in it.")
    assert len(text) == len(REPORT)
    assert not said[0].get("truncated")


def test_the_queued_record_holds_every_character(
    transcript, harvester, sessions, bus
):
    """Read at the ledger, not the API: nothing downstream can un-cut a record."""
    transcript.write_text(json.dumps(assistant(REPORT)) + "\n")
    harvester.poll(sessions)

    records = queued(bus)
    assert len(records) == 1, records
    assert len(records[0]["text"]) == len(REPORT), (
        f"office.send clipped the record to {len(records[0]['text'])} "
        f"characters of {len(REPORT)}")


# -- 2. a cut, if one happens, says so ---------------------------------------

def test_a_message_over_the_ceiling_says_it_was_cut(bus):
    """A cut is a product decision only when the message admits to it."""
    huge = "x" * (office.RECORD_MAX + 5_000)
    office.send("owner", huge, sender=DESK)

    record = queued(bus)[0]
    assert record["truncated"] == len(huge), (
        "a truncated record must carry the original length, so a client can "
        f"tell it was cut: {record!r}")
    assert office.CUT_MARK in record["text"], (
        "the cut is invisible in the text -- the exact defect, moved to a "
        f"bigger number: {record['text'][-200:]!r}")
    assert str(len(huge) - office.RECORD_MAX) in record["text"], (
        "the message must say how much was lost")


def test_the_ceiling_is_far_larger_than_a_real_report():
    """6,047 characters was a real report. The ceiling must not be near it."""
    assert office.RECORD_MAX >= 10 * len(REPORT), (
        f"RECORD_MAX={office.RECORD_MAX} is within reach of a real "
        f"{len(REPORT)}-character report; it will cut one again")


def test_the_api_carries_the_truncated_flag(bus, surface, client):
    """The client can tell. Without this the flag exists and nobody sees it."""
    office.send("owner", "y" * (office.RECORD_MAX + 42), sender=DESK)
    surface.refresh()

    res = client.get(f"/v1/threads/direct:{DESK}/messages", headers=auth())
    said = [m for m in res.json()["messages"] if m["author"] == DESK]
    assert said and said[0]["truncated"] == office.RECORD_MAX + 42, (
        f"the API does not report the cut: {said!r}")


# -- 3. the socket keeps its own limit ---------------------------------------

def test_the_socket_still_refuses_an_oversized_inject():
    """Deleting the borrowed clip must not delete the real ceiling."""
    assert manager_mod.MAX_TEXT == 2000
    with pytest.raises(manager_mod.InjectError) as caught:
        manager_mod.inject(1, "z" * (manager_mod.MAX_TEXT + 1))
    assert caught.value.reason == "too_long"
