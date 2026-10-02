"""A desk that just answered him is not "idle since it started".

THE OWNER'S COMPLAINT, 2026-09-06, about the line under his own conversation:

    Idle -- Its session is up and nothing is running.

It appears the instant the agent finishes answering him. It is technically
true and it is useless: it reads as "nothing happened" at exactly the moment
something did.

THE QUESTION THIS FILE SETTLES is not the wording -- that is the app's, and the
app is right to own it. It is whether the DECK gives the app enough to say
anything better, because a client cannot invent what it was not sent.

MEASURED, before this change, on the `/v1/agents` row: `state` is one of
`NEEDS_YOU / WORKING / DONE / SHELL / IDLE / DEAD / OFFLINE` and comes from the
session's own disposition, which knows nothing about the conversation.
`last_activity_at` is a timestamp with no author. `preview` is one line of text
with no author either -- `Surface._preview` prefixes peer traffic with
"Messaged X:" but leaves a desk's own words bare, which is indistinguishable
from the owner's own words being previewed back at him. And the `agent_state`
SSE frame carries `{type, ts, name, state, blocked}` and nothing else.

So the answer was NO: three different situations -- it answered you and is
waiting, you asked it something and it has not answered yet, and it has been
sitting there since it started and nobody has ever said anything -- all
published as the single word `IDLE` with a bare timestamp. The app could only
say "Idle", and it did.

WHAT IS ADDED is the one fact the deck holds and was not publishing: who spoke
last in this desk's conversation. `state` is untouched, because the session's
disposition is a real and separate thing -- a desk can be IDLE and have
answered, or WORKING and have answered, and conflating the two would trade
this defect for a worse one.

THE GOOD SIGNAL IS THE DISCRIMINATION. "The field is present" would be true of
a field hard-coded to `"agent"`, and that would let the app say "it answered
you" about a desk nobody has ever spoken to -- which is the same class of lie,
pointing the other way. So all three cases are asserted, against one deck, in
one refresh.
"""

import json

import pytest

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

#: Three desks, one board. `answered` spoke last, `asked` was spoken to and has
#: not replied, `untouched` has never been in a conversation at all.
DESKS = [
    {"name": n, "cwd": "/tmp/p", "engine": "claude", "mission": "m",
     "label": "L", "charter": "c.", "reports_to": None}
    for n in ("answered", "asked", "untouched")
]


@pytest.fixture
def surface(tmp_path, monkeypatch):
    """All three desks IDLE with a live session, and a conversation each."""
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")

    office.send("answered", "where are we?", sender="owner")
    office.send(office.OWNER_INBOX, "Three repos are behind.", sender="answered")
    office.send("asked", "where are we?", sender="owner")

    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"version": 1, "agents": DESKS}))
    snapshot = {"generated_at": 1_756_000_100.0, "sessions": [
        {"session_id": f"sid-{d['name']}", "pid": 4000 + i, "name": d["name"],
         "cwd": "/tmp/p", "project": "p", "state": "IDLE",
         "state_since": 1_756_000_000.0}
        for i, d in enumerate(DESKS)]}

    built = api_mod.Surface(
        snapshot=lambda: snapshot, comms=comms_mod.CommsIndex(),
        roster_path=roster_path, prefs_path=tmp_path / "agent_prefs.json")
    built.refresh()
    return built


def rows(surface):
    return {row["name"]: row for row in surface.agents()}


def test_the_deck_says_who_spoke_last_so_idle_can_stop_meaning_nothing(surface):
    """THE test, and all three cases in one assertion because the value is the
    discrimination and not any one of them.

    `IDLE` + `agent` is "it answered you and is waiting"; `IDLE` + `owner` is
    "you asked it something and it has not come back"; `IDLE` + `""` is the
    only one that is genuinely "idle since it started". Same `state` on all
    three -- which is the whole demonstration that `state` was never going to
    be able to carry this.
    """
    board = rows(surface)
    assert {n: row["state"] for n, row in board.items()} == {
        "answered": "IDLE", "asked": "IDLE", "untouched": "IDLE"}, (
        "the fixture no longer isolates the thing under test: the three desks "
        "must differ ONLY in their conversation")

    spoke_last = {n: row.get("last_activity_by") for n, row in board.items()}
    assert spoke_last == {"answered": "agent", "asked": "owner",
                          "untouched": ""}, (
        "the deck publishes nothing that tells a desk which just answered him "
        "apart from one that has been sitting there since it started, so the "
        f"app can only say 'Idle' about both: {spoke_last}")


def test_a_desk_with_no_session_still_says_whether_it_ever_answered(tmp_path,
                                                                    monkeypatch):
    """An OFFLINE desk is the case a restart passes through, and his thread
    outlives the session. So the field must be read from the CONVERSATION, not
    from anything the session carries -- or a desk that answered him and was
    then re-seated would go blank the moment it did.
    """
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    office.send(office.OWNER_INBOX, "Done -- three repos are behind.",
                sender="answered")
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"version": 1, "agents": DESKS[:1]}))

    built = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=roster_path,
        prefs_path=tmp_path / "agent_prefs.json")
    built.refresh()

    row = rows(built)["answered"]
    assert row["state"] == "OFFLINE", "the fixture seated a session after all"
    assert row["last_activity_by"] == "agent", (
        "a desk that answered him and was then re-seated forgets that it ever "
        f"spoke, so his own thread reads as empty activity: {row}")
