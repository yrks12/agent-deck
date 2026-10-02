"""A desk's schedule must live in the deck, not inside its own session.

Measured on the box, 2026-09-28. Desk `atlas` was asked for a daily morning
Shorts report and made it with Claude Code's built-in `CronCreate`:

    Scheduled recurring job c10073f2 (Every day at 11:57 AM). Session-only
    (not written to disk, dies when Claude exits). Auto-expires after 7 days.

The owner asked "why i dont see the routine it just created?" -- because
`GET /v1/routines` answered `{"routines": []}`. A session-only job is invisible
to the board, dies on the next desk restart, and expires in a week. The deck
has had a real routines feature all along; no desk used it, because nothing
told a desk it existed and nothing gave a desk a way to reach it.

The way in follows `YOS_HIRE`, not an HTTP call: a desk holds no bearer token
and is never handed one (see `test_hire_tells_the_boss.py`). A desk emits one
`YOS_ROUTINE` line, the harvester applies it through the SAME `Surface` methods
the app's Routines panel uses, and the desk is told the result by the deck.
The owner of the routine is the desk that spoke -- never a name on the line --
so a desk cannot schedule work into, or delete, a colleague's session.

Hermetic: tmp bus, tmp roster, tmp routines file, tmp transcripts.
"""

import json

import pytest

from server import api as api_mod
from server import app as app_mod
from server import approval, deskperms, harvest, hire, office
from server.harvest import Harvester
from server.roster import Desk, save_roster
from server.sources import comms as comms_mod

ATLAS = "atlas"
OTHER = "shorts-lead"


def desk(name):
    return Desk(name=name, cwd="/tmp", engine="claude", mission="m",
                label="COS", charter="You run the office.", reports_to=None)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def surface(bus):
    roster_path = bus / "roster.json"
    save_roster(roster_path, [desk(ATLAS), desk(OTHER)])
    snapshot = {"generated_at": 1.0, "sessions": [
        {"session_id": f"sid-{n}", "pid": 1, "name": n, "cwd": "/tmp",
         "project": "p", "state": "IDLE", "state_since": 0.0}
        for n in (ATLAS, OTHER)]}
    built = api_mod.Surface(snapshot=lambda: snapshot,
                            comms=comms_mod.CommsIndex(),
                            roster_path=roster_path,
                            prefs_path=bus / "agent_prefs.json",
                            routines_path=bus / "routines.json")
    built.refresh()
    return built


def said(text):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}]}}) + "\n"


def run(bus, surface, text, *, actor=ATLAS, n=[0]):
    n[0] += 1
    transcript = bus / f"t{n[0]}.jsonl"
    transcript.write_text(said(text))
    harvester = Harvester(bus / "roster.json", bus / f"off{n[0]}.json",
                          routines=surface)
    return harvester.poll([{"session_id": f"s{n[0]}", "name": actor,
                            "cwd": "/tmp", "state": "WORKING",
                            "transcript": str(transcript)}])


def routine_line(**payload):
    return "YOS_ROUTINE " + json.dumps(payload)


def told(bus, name):
    rows = [json.loads(r) for r in (bus / "messages.jsonl").read_text().splitlines()
            if r.strip()]
    return [r for r in rows if r.get("to") == name]


MORNING = dict(cron="57 7 * * *", tz="America/New_York",
               prompt="Morning Shorts report for Sam.")


def test_a_desk_schedule_lands_in_the_deck_routines_panel(bus, surface):
    """THE test: what the app's Routines panel reads now has the row."""
    records = run(bus, surface, routine_line(**MORNING))
    rows = surface.routine_rows()
    assert [(r["agent"], r["prompt"], r["trigger"]) for r in rows] == [
        (ATLAS, "Morning Shorts report for Sam.",
         {"kind": "cron", "spec": "57 7 * * *", "tz": "America/New_York"})]
    assert rows[0]["next_run_at"] is not None
    assert [(r["kind"], r["result"]) for r in records] == [("routine", "scheduled")]


def test_the_routine_belongs_to_the_desk_that_spoke(bus, surface):
    """A line naming another agent must not schedule work into its session."""
    run(bus, surface, routine_line(agent=OTHER, **MORNING))
    assert [r["agent"] for r in surface.routine_rows()] == [ATLAS]


def test_the_desk_is_told_the_id_by_the_deck(bus, surface):
    run(bus, surface, routine_line(**MORNING))
    rid = surface.routine_rows()[0]["id"]
    receipts = told(bus, ATLAS)
    assert receipts and receipts[-1]["from"] == harvest.DECK_SENDER
    assert rid in receipts[-1]["text"]


def test_a_desk_deletes_its_own_routine_and_not_a_colleagues(bus, surface):
    run(bus, surface, routine_line(**MORNING), actor=OTHER)
    theirs = surface.routine_rows()[0]["id"]
    records = run(bus, surface, routine_line(delete=theirs))
    assert [(r["result"], r["reason"]) for r in records] == [("refused", "not_yours")]
    assert [r["id"] for r in surface.routine_rows()] == [theirs]

    records = run(bus, surface, routine_line(delete=theirs), actor=OTHER)
    assert [r["result"] for r in records] == ["deleted"]
    assert surface.routine_rows() == []


def test_a_desk_can_list_its_routines(bus, surface):
    run(bus, surface, routine_line(**MORNING))
    rid = surface.routine_rows()[0]["id"]
    records = run(bus, surface, routine_line(list=True))
    assert [r["result"] for r in records] == ["listed"]
    assert rid in told(bus, ATLAS)[-1]["text"]


def test_a_bad_schedule_is_refused_out_loud_and_writes_nothing(bus, surface):
    records = run(bus, surface, routine_line(cron="every morning",
                                             tz="America/New_York", prompt="x"))
    assert [(r["result"], r["reason"]) for r in records] == [("refused", "bad_cron")]
    assert surface.routine_rows() == []
    assert "bad_cron" in told(bus, ATLAS)[-1]["text"]

    records = run(bus, surface, routine_line(cron="57 7 * * *", prompt="x"))
    assert records[0]["result"] == "refused" and "tz" in records[0]["reason"]


def test_the_marker_is_not_posted_to_the_owner_or_logged_as_a_hire(bus, surface):
    run(bus, surface, routine_line(**MORNING))
    assert not told(bus, office.OWNER_INBOX), "raw marker JSON reached his chat"
    events = (bus / "events.jsonl").read_text() if (bus / "events.jsonl").exists() else ""
    assert "hire_refused" not in events


def test_the_brief_says_schedules_go_in_the_deck(bus, surface):
    """And its example line is real: it lands a routine when harvested."""
    text = hire.brief(desk(ATLAS))
    assert "YOS_ROUTINE" in text and "CronCreate" in text
    example = [ln for ln in text.splitlines() if ln.startswith("YOS_ROUTINE ")]
    assert example
    run(bus, surface, "\n".join(example))
    assert len(surface.routine_rows()) == 1


def test_a_hired_desk_cannot_use_session_bound_cron():
    document = approval.settings_document("/usr/bin/node")
    assert "CronCreate" in document["permissions"]["deny"]
    assert deskperms.document()["permissions"]["deny"] == ["CronCreate"]


def test_the_daemon_hands_the_harvester_the_routines_surface(monkeypatch, tmp_path):
    seen = {}

    class Recording:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def poll(self, sessions):
            return []

    monkeypatch.setattr(app_mod, "_harvester", None)
    monkeypatch.setattr(app_mod.harvest_mod, "Harvester", Recording)
    app_mod._harvest([])
    assert seen.get("routines") is app_mod._client_surface
