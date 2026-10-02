"""`GET /v1/approvals` must publish the DESK that asked, not a session id.

THE DEFECT, measured on the live box. Four questions were pending, every desk
was stalled behind them, and the app drew nothing at all::

    {"id": "xgdtu", "agent": "bfffdc59-5f34-4b6b-b304-300c80cb3c25",
     "tool": "Bash", "subject": "pwd && ls -la",
     "cwd": ".../workspaces/new-hire-77ec17", "status": "pending"}

`agent` is the raw session UUID the `PreToolUse` hook happened to carry.
`Approval.swift` decodes that field into `agentName` and the app draws a card
only where `agentName == <the desk whose conversation is open>`, so a UUID
matches no desk, in no thread, ever. The card view, the poll and the answer
endpoint all worked; the JOIN was broken -- and `server/asking.py` already
declares the field as "the desk/session name that asked", so the contract was
right and the writer was not honouring it.

THE FAULT IS THE DECK'S. Nothing on the phone knows which session id belongs to
which desk; the deck holds both. It already did this join once, in
`Surface._answerable`, which matches a live question to a desk on NAME OR
FOLDER -- and that is exactly the rule `_approval` was not applying. One helper
now, called by both, so the two can never disagree about who asked.

THE GOOD SIGNAL, asserted here and never the absence of a bad one: a question
recorded against a desk -- by its name, by its session id, by the `session
<id>` form `ask_recorder._who` falls back to, or merely by being asked from
that desk's own workspace -- is PUBLISHED WITH THAT DESK'S NAME in `agent`, so
the app has something to hang the card on.

And the honest half: a question the deck genuinely cannot place must still
appear on the board, carrying the value the hook recorded and saying plainly
that no desk was resolved. A card filed under the wrong desk is worse than one
he has to go and find.
"""

import json

import pytest

from server import api as api_mod
from server import asking
from server import office
from server.sources import comms as comms_mod

CHIEF_SID = "8edb89dc-6be9-4daf-b90e-cf88c4630410"
HIRE_SID = "bfffdc59-5f34-4b6b-b304-300c80cb3c25"
STRANGER_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def hire_cwd(bus):
    """The hire's own workspace, exactly as `Surface._workspace` allocates it."""
    return str(bus / "workspaces" / "new-hire-77ec17")


@pytest.fixture
def roster_file(bus, hire_cwd):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None},
        {"name": "new-hire-77ec17", "cwd": hire_cwd, "engine": "claude",
         "mission": "be interviewed", "reports_to": "atlas"},
    ]}))
    return path


@pytest.fixture
def snapshot(hire_cwd):
    """Both desks seated, so the board holds each one's session id."""
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [
            {"session_id": CHIEF_SID, "pid": 41, "name": "atlas",
             "cwd": "/tmp/atlas", "project": "atlas", "state": "WORKING",
             "state_since": 1_756_000_000.0, "attention": None},
            {"session_id": HIRE_SID, "pid": 42, "name": "new-hire-77ec17",
             "cwd": hire_cwd, "project": "new-hire-77ec17", "state": "NEEDS_YOU",
             "state_since": 1_756_000_000.0, "attention": None},
        ],
    }


@pytest.fixture
def surface(bus, roster_file, snapshot):
    made = api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json",
    )
    made.refresh()  # the board the daemon's 1 Hz loop keeps warm
    return made


def only(surface):
    rows = surface.approvals()
    assert len(rows) == 1, f"expected one published question, got {rows!r}"
    return rows[0]


# -- the class, not the instance ---------------------------------------------
#
# Every shape a recorded ask arrives in. `hooks/cc-permission.js` sends no desk
# name, so `ask_recorder._who` falls through to the session id or to its
# `session <id>` form; a desk that DOES name itself is the first shape; and a
# question asked from a desk's own workspace by a session the board has never
# seen is the fourth -- which is the shape all three live blockers had.

@pytest.mark.parametrize("shape,agent,cwd,desk", [
    ("the desk named itself",
     "new-hire-77ec17", "/anywhere/else", "new-hire-77ec17"),
    ("a bare session uuid",
     HIRE_SID, "/anywhere/else", "new-hire-77ec17"),
    ("the `session <id>` fallback",
     f"session {HIRE_SID[:8]}", "/anywhere/else", "new-hire-77ec17"),
    ("an unknown session asking from a desk's workspace",
     STRANGER_SID, None, "new-hire-77ec17"),
    ("a second desk, so this is a join and not a constant",
     CHIEF_SID, "/anywhere/else", "atlas"),
])
def test_the_published_agent_is_the_desk_that_asked(
    surface, hire_cwd, shape, agent, cwd, desk
):
    """THE GOOD SIGNAL. Whatever the hook wrote down, the board publishes the
    desk NAME -- the one thing the app can match a conversation against."""
    asking.record(surface.asks_path, agent=agent, tool="Bash",
                  subject="pwd && ls -la",
                  cwd=hire_cwd if cwd is None else cwd)

    row = only(surface)

    assert row["agent"] == desk, (
        f"{shape}: published agent={row['agent']!r}, which is not a desk on "
        f"the board -- the app filters cards by this field against the desk "
        f"whose conversation is open, so nothing is drawn for {desk!r}")
    assert row["desk_known"] is True
    assert row["asked_by"] == agent, (
        "the raw value the hook recorded must survive on its own field")


def test_a_question_the_deck_cannot_place_still_reaches_the_board(surface):
    """The honest half. No desk owns this session id and no desk works in that
    folder, so the deck says so rather than inventing an owner -- and the row
    is still published, because a question he cannot see is a stalled desk."""
    asking.record(surface.asks_path, agent=STRANGER_SID, tool="Bash",
                  subject="pwd && ls -la", cwd="/tmp/nobodys-folder")

    row = only(surface)

    assert row["agent"] == STRANGER_SID, (
        "unresolvable must mean unchanged: guessing a desk files the card "
        "under someone who never asked")
    assert row["desk_known"] is False, (
        "the payload has to state that no desk was resolved, or the app "
        "cannot tell a real desk name from a fallback")
    assert row["asked_by"] == STRANGER_SID
    assert row["status"] == "pending"
    assert row["options"], "a published question must stay answerable"


def test_answering_still_goes_by_ask_id(surface, hire_cwd):
    """The answer path is addressed by id and must not have moved."""
    ask = asking.record(surface.asks_path, agent=HIRE_SID, tool="Bash",
                        subject="pwd && ls -la", cwd=hire_cwd)

    result = surface.answer_ask(ask.id, "once")

    assert result["ok"] is True
    assert result["ask"]["answered"] == "once"
