"""The `agent_state` SSE frame must carry `blocked`, in both directions.

THE GAP. `GET /v1/agents` computes `blocked` live off the card (see
`tests/test_stuck_desk.py`). `GET /v1/stream` does not fetch -- it diffs one
collector tick against the last and emits `{"type": "agent_state", "name",
"state"}` only when `state` (or `unread`) changed. A desk that freezes on a
permission dialog *while a stream client is already connected* -- the normal
case, since freezing happens mid-session, not at connect time -- pushes that
frame with no `blocked` on it. The connected client updates `state` and has
nothing to hang the reason on; the badge only appears on the next full roster
fetch or a reconnect. That is the staleness window this file closes.

THE OTHER DIRECTION. `blocked` clears itself the instant the dialog is
answered (`tests/test_stuck_desk.py::test_the_flag_clears_itself_when_the_
dialog_goes`). A frame that only ever *adds* a reason and never retracts one
leaves the badge stuck on a desk that recovered -- the same lie, inverted. So
the fix has to diff `blocked` itself, not just piggyback on a `state` diff:
a desk can plausibly flip from "unanswerable dialog" to "no live block" while
`state` stays `NEEDS_YOU` throughout (the pending ask on the board gets
answered while the terminal dialog itself is still up), and that transition
must also produce a frame.

THE GOOD SIGNAL, always: a connected stream client receiving the reason when a
desk freezes, and receiving its clearance when the desk recovers. Never the
absence of an error.
"""

import json

import pytest

from server import api as api_mod
from server import asking
from server import office
from server.sources import comms as comms_mod

CHIEF = {
    "name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF]}))
    return path


@pytest.fixture
def asks_file(bus):
    return bus / "asks.json"


@pytest.fixture
def snapshot():
    """One session seated at `chief`, working normally."""
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp/p", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0, "attention": None,
        }],
    }


@pytest.fixture
def surface(bus, roster_file, asks_file, snapshot):
    return api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=asks_file,
    )


def stall(snapshot, kind="permission_prompt",
          message="Claude needs your permission to use Bash"):
    """Freeze the seated session on a dialog nobody on the board can answer."""
    snapshot["sessions"][0]["state"] = "NEEDS_YOU"
    snapshot["sessions"][0]["attention"] = {
        "kind": kind, "message": message, "since": 1_756_000_050.0}


def unstall(snapshot):
    """The dialog goes: the session sits back down."""
    snapshot["sessions"][0]["state"] = "WORKING"
    snapshot["sessions"][0]["attention"] = None


def agent_state_event(events, name="chief"):
    return next(
        (e for e in events if e["type"] == "agent_state" and e["name"] == name),
        None,
    )


def test_a_desk_that_freezes_mid_stream_hands_the_connected_client_the_reason(
    surface, snapshot
):
    """THE GOOD SIGNAL, direction one. A client that has been connected since
    before the freeze must be able to read `blocked` straight off the frame
    that reports the state change -- not have to reconnect or re-fetch."""
    surface.refresh()  # baseline: connected, chief is WORKING, nothing pending

    stall(snapshot)
    events = surface.refresh()

    event = agent_state_event(events)
    assert event is not None, "no agent_state frame at all on the freeze"
    assert event.get("blocked") is not None, (
        "the frame changed `state` to NEEDS_YOU but carries no `blocked` -- "
        "a client that was already connected has nothing to hang the reason "
        "on and shows the desk as merely busy until its next full refetch")
    assert event["blocked"]["reason"] == api_mod.STUCK_ON_DIALOG


def test_a_desk_that_recovers_mid_stream_hands_the_connected_client_the_clearance(
    surface, snapshot
):
    """THE GOOD SIGNAL, direction two. A frame that only ever adds a reason and
    never retracts one is the same staleness bug pointed the other way: the
    badge outlives the dialog for every client that is already connected."""
    surface.refresh()
    stall(snapshot)
    surface.refresh()  # client now shows blocked

    unstall(snapshot)
    events = surface.refresh()

    event = agent_state_event(events)
    assert event is not None, "no agent_state frame at all on the recovery"
    assert "blocked" in event, (
        "the frame carries no `blocked` key at all -- a client that only ever "
        "learns a reason from this frame and never learns a retraction leaves "
        "the badge stuck on a desk that is fine again")
    assert event["blocked"] is None, (
        "the desk sat back down but the frame that reported it still carries "
        "the old block -- a connected client's badge is stuck on a desk that "
        "is fine again")


def test_blocked_can_flip_while_state_does_not_move(surface, snapshot, asks_file):
    """The subtler half of the same class. `state` stays NEEDS_YOU the whole
    time here -- only whether the board holds a matching pending ask changes --
    so a diff keyed on `state` alone would never even emit a frame. `blocked`
    itself has to be part of what gets diffed."""
    surface.refresh()
    ask = asking.record(asks_file, agent="chief", tool="Bash",
                        subject="npx some-brand-new-thing", cwd="/tmp/p")
    stall(snapshot)
    events = surface.refresh()
    assert agent_state_event(events)["blocked"] is None, (
        "an answerable question must not read as blocked")

    asking.answer(asks_file, ask.id, "never")
    events = surface.refresh()  # state is still NEEDS_YOU throughout

    event = agent_state_event(events)
    assert event is not None, (
        "the desk went from answerable to stuck on the same dialog and "
        "`state` never moved -- a diff keyed on `state` alone drops this "
        "transition on the floor")
    assert event["blocked"] is not None
    assert event["blocked"]["reason"] == api_mod.STUCK_ON_DIALOG


def test_a_desk_that_stays_fine_emits_nothing(surface, snapshot):
    """Not every tick is a frame. A quiet desk must not thrash the stream."""
    surface.refresh()
    assert surface.refresh() == [], "a quiet tick must emit nothing at all"
