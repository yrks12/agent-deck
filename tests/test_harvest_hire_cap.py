"""An agent hiring an agent must be refused for the size of the ORG, not the Mac.

`Harvester.poll` is handed `sessions` -- the raw card list `app._harvest` gets
from the collector tick, which is **every Claude Code session running on this
machine**: the owner's own terminals, every agent worktree, every unrelated
project window. It passed `live=len(sessions)` straight into
`onboard.apply_hire`, which is `hire.hire`'s `live_count`, which is checked
against `hire.MAX_LIVE = 8`.

So the cap on the size of the owner's company was really a cap on how many
terminal windows he had open. The identical fault on the owner-facing path was
measured and fixed: three desks and ten sessions produced
`409 too_many_live -- "10 live sessions; the cap is 8"`, and his `+` button was
refused for a reason that had nothing to do with how many agents he had.

**This path is the worse one, because it fails quietly.** When an agent emits
`YOS_HIRE` the refusal goes to the ledger as a `hire_refused` record and
nothing tells the owner that his manager tried to build a team and was turned
down. Agents hiring agents is the claim the product rests on, and on a machine
with a handful of terminals open it could not happen at all.

Both halves are asserted here, because a fix that merely raised the cap would
pass the first and destroy the second:

1. A hire SUCCEEDS with a small org and many unrelated sessions.
2. The cap still REFUSES when the org genuinely is full.

Hermetic: roster, bus and transcripts under tmp_path.
"""

import json

import pytest

from server import hire, office
from server.harvest import Harvester
from server.roster import Desk, load_roster, save_roster

BOSS = "chief"
BOSS_SESSION = "sid-chief"


@pytest.fixture(autouse=True)
def private_bus(tmp_path, monkeypatch):
    """A bus of this test's own.

    Not optional and not cosmetic: a turn-final line is also *speech*, so
    `Harvester._say` posts it through `office.send` -- and the conftest guard
    caught this file writing a message into the owner's live queue on the first
    run. The hire being tested here rides in on exactly such a line.
    """
    monkeypatch.setattr(office, "BUS_DIR", tmp_path / "bus")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "bus" / "messages.jsonl")


def desk(name, cwd, boss=None):
    return Desk(name=name, cwd=str(cwd), engine="claude", mission="work",
                label="Desk", charter="Own it.", reports_to=boss)


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "hire-target"
    path.mkdir()
    return path


@pytest.fixture
def roster_path(tmp_path, workspace):
    path = tmp_path / "roster.json"
    save_roster(path, [desk(BOSS, workspace)])
    return path


@pytest.fixture
def harvester(roster_path, tmp_path):
    return Harvester(roster_path, tmp_path / "offsets.json")


def hire_line(name, cwd):
    return json.dumps({"name": name, "label": "Researcher",
                       "charter": "Find things.",
                       "description": "Finds things.", "cwd": str(cwd)})


def transcript_for(tmp_path, text, session_id=BOSS_SESSION):
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "assistant", "uuid": "u-1", "sessionId": session_id,
        "isSidechain": False, "timestamp": "2026-09-01T15:11:00.000Z",
        "message": {"id": "m-1", "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n")
    return path


def card(session_id, name, cwd, state="WORKING"):
    return {"session_id": session_id, "name": name, "pid": 1000 + len(session_id),
            "cwd": str(cwd), "project": "p", "state": state,
            "state_since": 1_756_000_000.0}


def bystanders(tmp_path, count):
    """Sessions that are nobody's desk: the owner's terminals, worktrees."""
    return [card(f"sid-other-{i}", f"unrelated-{i}", tmp_path / "elsewhere")
            for i in range(count)]


# -- 1. the hire lands -------------------------------------------------------

def test_a_manager_can_hire_with_many_unrelated_sessions_open(
    harvester, roster_path, tmp_path, workspace
):
    """THE detector. One desk, one hire, and a Mac full of other windows."""
    transcript = transcript_for(
        tmp_path, "Bringing someone in.\nYOS_HIRE " + hire_line("seeker", workspace))
    sessions = [
        {**card(BOSS_SESSION, BOSS, workspace), "transcript": str(transcript)},
        *bystanders(tmp_path, 12),
    ]

    applied = harvester.poll(sessions)

    hires = [r for r in applied if r["kind"] == "hire"]
    assert hires and hires[0]["result"] == "hired", (
        "an agent's hire was refused because of sessions that are not desks: "
        f"{hires!r}")
    assert "seeker" in {d.name for d in load_roster(roster_path)}


def test_the_refusal_reason_is_never_a_count_of_terminals(
    harvester, tmp_path, workspace
):
    """The failure is silent, so the reason on the ledger is the only witness."""
    transcript = transcript_for(
        tmp_path, "YOS_HIRE " + hire_line("seeker", workspace))
    sessions = [
        {**card(BOSS_SESSION, BOSS, workspace), "transcript": str(transcript)},
        *bystanders(tmp_path, 12),
    ]
    refusals = [r for r in harvester.poll(sessions) if r["result"] == "refused"]
    assert not any(r["reason"] == "too_many_live" for r in refusals), (
        f"refused for the machine's session count: {refusals!r}")


# -- 2. the cap still bites --------------------------------------------------

def test_the_cap_still_refuses_a_genuinely_full_org(
    roster_path, tmp_path, workspace
):
    """Raising the cap would pass the tests above and delete the guard.

    `MAX_LIVE` desks, every one of them seated at a live session, and the next
    hire must still be refused -- with the words that say why.
    """
    seated = [desk(BOSS, workspace)] + [
        desk(f"desk-{i}", workspace, boss=BOSS) for i in range(hire.MAX_LIVE - 1)
    ]
    save_roster(roster_path, seated)
    harvester = Harvester(roster_path, tmp_path / "offsets-full.json")

    transcript = transcript_for(
        tmp_path, "YOS_HIRE " + hire_line("one-too-many", workspace))
    sessions = [
        {**card(BOSS_SESSION, BOSS, workspace), "transcript": str(transcript)},
        *[card(f"sid-{d.name}", d.name, workspace) for d in seated[1:]],
    ]

    applied = harvester.poll(sessions)
    hires = [r for r in applied if r["kind"] == "hire"]
    assert hires and hires[0]["result"] == "refused", (
        f"a full org accepted another desk: {hires!r}")
    assert hires[0]["reason"] == "too_many_live", hires[0]["reason"]
    assert "one-too-many" not in {d.name for d in load_roster(roster_path)}


def test_a_dead_card_does_not_hold_a_seat(roster_path, tmp_path, workspace):
    """A DEAD card lingers for the fade-out and is not a running agent."""
    seated = [desk(BOSS, workspace)] + [
        desk(f"desk-{i}", workspace, boss=BOSS) for i in range(hire.MAX_LIVE - 1)
    ]
    save_roster(roster_path, seated)
    harvester = Harvester(roster_path, tmp_path / "offsets-dead.json")

    transcript = transcript_for(
        tmp_path, "YOS_HIRE " + hire_line("replacement", workspace))
    sessions = [
        {**card(BOSS_SESSION, BOSS, workspace), "transcript": str(transcript)},
        *[card(f"sid-{d.name}", d.name, workspace) for d in seated[1:-1]],
        card(f"sid-{seated[-1].name}", seated[-1].name, workspace, state="DEAD"),
    ]

    hires = [r for r in harvester.poll(sessions) if r["kind"] == "hire"]
    assert hires and hires[0]["result"] == "hired", (
        f"a desk whose session is gone still held the seat: {hires!r}")
