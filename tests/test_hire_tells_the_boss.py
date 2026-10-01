"""Hiring must staff, and the boss must end up knowing it has reports.

Measured live. `hire.brief` is one-directional: the new hire learns its boss's
name and a stable address; **the boss learns nothing**. No message, no brief
update -- and its own system prompt was frozen at the moment it spawned, so
there is no later edition of it to add to. Both new desks came up `OFFLINE` and
stayed there. Asked for a status the manager did the work itself and never
mentioned either of them. Only an out-of-band `POST /v1/agents/<name>/start`
made them real, after which it dispatched to them correctly and unprompted.

THE DECISION, and the argument for it:

* *Should the manager be told to start its hire?* No. It would have to make an
  authenticated call to a localhost port it was never given, with a token it
  was never handed. That is a second capability no prompt on this machine
  describes -- the exact defect class this is inside.
* *Should the board just surface "hired but never started"?* Not on its own.
  That fixes the owner's view and leaves the manager blind, and a manager that
  cannot see its own team keeps doing the work itself. Which is what happened.
* *Should the hire start the desk?* Yes -- but not in `hire.hire()`, which is
  deliberately not a spawn, and whose purity is what lets this whole suite run
  without opening Terminal windows. It belongs one layer up, in the wiring that
  already owns both HTTP start doors. So the harvester takes a `seat` callback
  and `app.py` hands it `Surface.start_agent` -- the same `_start` the interview
  door uses, so the workspace-trust gate cannot be skipped by this path either.

And either way the boss is told, in its own thread, in one line: what landed,
under what name, and whether anybody is sitting there. A refusal is told too --
a desk told it can hire and then silently refused is the same defect wearing
different clothes.

Hermetic: roster, ledger, message queue and transcripts all under `tmp_path`.
Nothing spawns; `seat` is a recording stub.
"""

import json

import pytest

from server import office
from server.harvest import Harvester
from server.roster import Desk, load_roster, save_roster

BOSS = "cos"


def desk(name, reports_to=None):
    return Desk(name=name, cwd="/tmp", engine="claude", mission="m",
                label="Admin", charter="You run the office.",
                reports_to=reports_to)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """A private agent-bus, so no test touches the owner's live queue."""
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_path(bus):
    path = bus / "roster.json"
    save_roster(path, [desk(BOSS)])
    return path


def said(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": text}]}}) + "\n"


def hire_line(name, cwd, label="Growth", charter="You own paid acquisition."):
    return "YOS_HIRE " + json.dumps(
        {"name": name, "label": label, "charter": charter, "cwd": str(cwd)})


def run(tmp_path, roster_path, line, *, seat=None):
    """Harvest one line said by BOSS. Returns the records it produced."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(line))
    harvester = Harvester(roster_path, tmp_path / "offsets.json", seat=seat)
    return harvester.poll([{"session_id": "s1", "name": BOSS, "cwd": "/tmp",
                            "state": "WORKING", "transcript": str(transcript)}])


def to_boss(bus) -> list[str]:
    """Every message queued FOR the boss, oldest first."""
    out = []
    for raw in (bus / "messages.jsonl").read_text().splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        if record.get("to") == BOSS:
            out.append(record.get("text") or "")
    return out


def test_the_boss_is_told_the_hire_landed(tmp_path, bus, roster_path):
    """THE test. A manager that is never told it has reports keeps doing the
    work itself -- measured, with two OFFLINE desks under it at the time."""
    run(tmp_path, roster_path, hire_line("growth-scout", tmp_path))
    assert to_boss(bus), "the boss was told nothing about its own hire"


def test_the_boss_can_name_the_report_from_what_it_was_told(tmp_path, bus,
                                                            roster_path):
    """The GOOD signal: the name in the message is the name on the roster, so
    the manager can address the desk it now has."""
    run(tmp_path, roster_path, hire_line("growth-scout", tmp_path))
    hired = [d for d in load_roster(roster_path) if d.reports_to == BOSS]
    assert [d.name for d in hired] == ["growth-scout"]
    assert any("growth-scout" in text for text in to_boss(bus)), to_boss(bus)


def test_the_hire_is_seated(tmp_path, bus, roster_path):
    """Hiring must staff. The desk that lands gets a session put at it, once,
    by name -- through the wiring, not through `hire.hire()`."""
    seated = []
    run(tmp_path, roster_path, hire_line("growth-scout", tmp_path),
        seat=seated.append)
    assert seated == ["growth-scout"]


def test_a_refused_hire_is_told_to_the_boss_and_seats_nobody(tmp_path, bus,
                                                             roster_path):
    """A silent refusal is the same defect in different clothes: the manager
    reports the hire it asked for as a hire that happened."""
    seated = []
    records = run(tmp_path, roster_path,
                  hire_line("ghost", "/no/such/directory"), seat=seated.append)
    assert [(r["result"], r["reason"]) for r in records] == [
        ("refused", "no_such_cwd")]
    assert seated == []
    assert any("no_such_cwd" in text for text in to_boss(bus)), to_boss(bus)


def test_a_seat_that_fails_still_tells_the_boss(tmp_path, bus, roster_path):
    """Opening a window can fail -- a busy Mac, a trust dialog, an unknown
    engine. The boss must still learn the desk exists, and must be told nobody
    is at it, or it will address a desk that cannot answer."""
    def explode(name):
        raise RuntimeError("Terminal said no")

    records = run(tmp_path, roster_path, hire_line("growth-scout", tmp_path),
                  seat=explode)
    assert [r["result"] for r in records] == ["hired"]
    told = " ".join(to_boss(bus))
    assert "growth-scout" in told
    assert "OFFLINE" in told, told


def test_no_seat_wired_is_not_a_crashed_tick(tmp_path, bus, roster_path):
    """The default. Every existing caller builds a Harvester with two
    arguments, and must keep working."""
    records = run(tmp_path, roster_path, hire_line("growth-scout", tmp_path))
    assert [r["result"] for r in records] == ["hired"]
    assert "growth-scout" in [d.name for d in load_roster(roster_path)]
