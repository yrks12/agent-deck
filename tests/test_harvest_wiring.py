"""The harvester wired into the daemon: an agent can build its own team.

`server/harvest.py` has been tested and uncalled since it was written, which is
precisely why an agent cannot hire another agent today -- nothing on this
machine reads what a session says about itself. These tests are about the
wiring, and about the three ways wiring it can go wrong:

1. **The daemon must actually poll it**, off the collector tick it already has.
2. **A bad poll must cost a tick, not the daemon.** The board is the one thing
   that must keep painting; a harvester that raises must be logged and stepped
   over.
3. **The caps still apply to a hire an agent asked for.** `hire.hire()` is the
   only door -- depth, live count, duplicate name, unknown boss -- and a breach
   must be *refused and logged*, while a legitimate hire really lands. A path
   that refused everything would pass half of that pair, which is why both are
   here.

Plus the privilege rule on the wired path: the actor handed to
`onboard.apply_patch` is the session's own desk name, never the name the line
claimed. Trusting the line disarms the guard while looking like it works.

Hermetic: tmp roster, tmp offsets, tmp ledger, tmp transcripts. Nothing spawns
and nothing reads ~/.claude.
"""

import asyncio
import json

import pytest

from server import app as app_mod
from server import hire, office
from server.harvest import Harvester
from server.roster import Desk, load_roster, save_roster


@pytest.fixture(autouse=True)
def private_message_queue(tmp_path, monkeypatch):
    """A message queue under `tmp_path`, for every test in this file.

    The harvester sends the hiring desk a one-line receipt for each hire it
    applies or refuses, so hiring is no longer a silent write to the roster.
    Autouse rather than opt-in: `tests/conftest.py` fails any test that reaches
    the owner's live `~/.claude/agent-bus/`, and every hire here would.
    """
    monkeypatch.setattr(office, "BUS_DIR", tmp_path / "bus")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "bus" / "messages.jsonl")

COS_CHARTER = "You run the office and you are the only desk the owner talks to."
PALM_CHARTER = "You own the Acme & Line reading product itself."


def desk(name, reports_to=None, label="", charter="", cwd="/tmp"):
    return Desk(name=name, cwd=cwd, engine="claude", mission=charter,
                label=label, charter=charter, reports_to=reports_to)


@pytest.fixture
def roster_path(tmp_path):
    path = tmp_path / "roster.json"
    save_roster(path, [
        desk("cos", None, "admin", COS_CHARTER),
        desk("acme", "cos", "product", PALM_CHARTER),
    ])
    return path


@pytest.fixture
def offsets_path(tmp_path):
    return tmp_path / "harvest-offsets.json"


def said(text):
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def session(name, transcript, *, session_id="s1"):
    return {"session_id": session_id, "name": name, "cwd": "/tmp",
            "state": "WORKING", "transcript": str(transcript)}


def hire_line(name, *, cwd, charter="You own paid acquisition.", label="growth"):
    return "YOS_HIRE " + json.dumps(
        {"name": name, "label": label, "charter": charter, "cwd": str(cwd)})


def desk_line(**fields):
    return "YOS_DESK " + json.dumps(fields)


def ledger(roster_path):
    log = roster_path.parent / "events.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def names(path):
    return sorted(d.name for d in load_roster(path))


# ── 1. the daemon polls it ─────────────────────────────────────────────────


def test_the_daemon_hands_every_tick_to_the_harvester(monkeypatch):
    """Off the collector tick, with the session list the tick just produced --
    a second scanner would double the deck's I/O and could disagree with the
    board about who is live."""
    cards = [{"session_id": "s1", "name": "cos", "cwd": "/tmp", "state": "WORKING"}]
    polled: list[list[dict]] = []

    class FakeHarvester:
        def poll(self, sessions):
            polled.append(sessions)
            return []

    monkeypatch.setattr(app_mod, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(app_mod, "_harvester", FakeHarvester())
    monkeypatch.setattr(app_mod.collector, "tick",
                        lambda: {"generated_at": 1.0, "sessions": cards})

    asyncio.run(_run_loop_briefly())

    assert polled, "the daemon never polled the harvester"
    assert polled[0] == cards


def test_a_harvester_that_raises_leaves_the_board_painting(monkeypatch, capsys):
    """One bad poll must be logged and stepped over. The board is the whole
    product; a harvest failure that stops the tick blanks it."""
    ticks = {"n": 0}

    def tick():
        ticks["n"] += 1
        return {"generated_at": float(ticks["n"]), "sessions": []}

    class Exploding:
        def poll(self, sessions):
            raise RuntimeError("harvest exploded")

    monkeypatch.setattr(app_mod, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(app_mod, "_harvester", Exploding())
    monkeypatch.setattr(app_mod.collector, "tick", tick)

    asyncio.run(_run_loop_briefly())

    assert ticks["n"] >= 2, "the loop stopped after the first bad harvest"
    assert app_mod._state["generated_at"] >= 2.0
    printed = capsys.readouterr().out
    assert "harvest" in printed and "harvest exploded" in printed


async def _run_loop_briefly(seconds: float = 0.12) -> None:
    task = asyncio.create_task(app_mod._loop())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── 2. an agent may only describe itself ───────────────────────────────────


def test_the_wired_path_passes_the_true_actor_not_the_claimed_one(
    tmp_path, roster_path, offsets_path
):
    """`acme` naming `cos` is an attempt to rewrite its own boss. The guard
    only engages if the harvester hands over who really spoke."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(desk_line(name="cos", charter="I am the boss now.")))

    results = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [r["result"] for r in results] == ["refused"]
    assert results[0]["reason"] == "not_yours"
    assert results[0]["actor"] == "acme"
    cos = next(d for d in load_roster(roster_path) if d.name == "cos")
    assert cos.charter == COS_CHARTER


def test_a_refusal_is_logged_to_the_one_ledger(tmp_path, roster_path, offsets_path):
    """`onboard`'s docstring promises "refused and logged". Until now it only
    returned: an agent could try to rewrite its boss every minute for a week
    and nothing on this machine would ever say so."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(desk_line(name="cos", charter="I am the boss now.")))

    Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    refusals = [e for e in ledger(roster_path) if e.get("result") == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["event"] == "desk"
    assert refusals[0]["actor"] == "acme"
    assert refusals[0]["reason"] == "not_yours"
    assert isinstance(refusals[0]["ts"], float)


def test_an_applied_patch_is_logged_too(tmp_path, roster_path, offsets_path):
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(desk_line(label="Palmist", charter="I read palms.")))

    Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    applied = [e for e in ledger(roster_path)
               if e.get("event") == "desk" and e.get("result") == "patched"]
    assert len(applied) == 1
    assert applied[0]["actor"] == "acme"
    assert applied[0]["name"] == "acme"
    assert next(d for d in load_roster(roster_path) if d.name == "acme").label == "Palmist"


# ── 3. an agent's hire obeys the caps ──────────────────────────────────────


def test_a_hire_an_agent_asked_for_really_lands(tmp_path, roster_path, offsets_path):
    """The other half of the pair. A harvester that refused everything would
    pass every cap test on this page and still be useless."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-growth", cwd=tmp_path)))

    results = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [r["result"] for r in results] == ["hired"]
    assert "acme-growth" in names(roster_path)
    hired = next(d for d in load_roster(roster_path) if d.name == "acme-growth")
    assert hired.reports_to == "acme"
    assert [e for e in ledger(roster_path)
            if e.get("event") == "hire" and e.get("name") == "acme-growth"]


def test_a_hire_that_breaches_the_depth_cap_is_refused_and_logged(
    tmp_path, roster_path, offsets_path
):
    """An agent that can hire past the caps fills the machine while nobody is
    watching. Depth 2 is the floor of the org; a hire under it is depth 3."""
    save_roster(roster_path, load_roster(roster_path) + [
        desk("acme-growth", "acme", "growth", "You own paid acquisition."),
    ])
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-growth-intern", cwd=tmp_path)))

    results = Harvester(roster_path, offsets_path).poll(
        [session("acme-growth", transcript)])

    assert [r["result"] for r in results] == ["refused"]
    assert results[0]["reason"] == "too_deep"
    assert "acme-growth-intern" not in names(roster_path)

    refusals = [e for e in ledger(roster_path)
                if e.get("event") == "hire_refused"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "too_deep"
    assert refusals[0]["actor"] == "acme-growth"
    assert refusals[0]["name"] == "acme-growth-intern"


def test_a_hire_past_the_live_cap_is_refused_and_logged(
    tmp_path, roster_path, offsets_path
):
    """The cap bites when the ORG is full -- eight desks with people at them.

    This used to build eight sessions called `other0..other7`, which are on
    nobody's roster, and assert the hire was refused. That is the defect
    written down as a requirement: the crowd was the owner's own terminals and
    unrelated worktrees, and `len(sessions)` counted them, so his manager could
    not hire a colleague on any machine with a few windows open. The crowd here
    is `MAX_LIVE` real desks, each seated.
    """
    seated = [desk("cos", None, "admin", COS_CHARTER),
              desk("acme", "cos", "product", PALM_CHARTER)]
    seated += [desk(f"staff{i}", "cos", "staff", "You own a thing.")
               for i in range(hire.MAX_LIVE - len(seated))]
    save_roster(roster_path, seated)

    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-growth", cwd=tmp_path)))
    # Their own, empty: sharing `acme`'s transcript would have every desk on
    # the board emit the same YOS_HIRE and refuse it once each.
    quiet = tmp_path / "quiet.jsonl"
    quiet.write_text("")
    crowd = [session("acme", transcript)] + [
        session(d.name, quiet, session_id=f"s-{d.name}")
        for d in seated if d.name != "acme"
    ]

    results = Harvester(roster_path, offsets_path).poll(crowd)

    assert [r["result"] for r in results] == ["refused"]
    assert results[0]["reason"] == "too_many_live"
    assert "acme-growth" not in names(roster_path)
    assert [e for e in ledger(roster_path)
            if e.get("event") == "hire_refused" and e.get("reason") == "too_many_live"]


def test_sessions_that_are_nobodys_desk_do_not_fill_the_org(
    tmp_path, roster_path, offsets_path
):
    """The other half, and the one that was broken. Eight unrelated windows.

    Measured before the fix: `result: refused, reason: too_many_live` -- an
    agent hiring an agent, the claim this product rests on, could not happen on
    an ordinary working machine, and the refusal was silent.
    """
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-growth", cwd=tmp_path)))
    crowd = [session("acme", transcript)] + [
        session(f"other{i}", transcript, session_id=f"s{i + 2}") for i in range(8)
    ]

    results = Harvester(roster_path, offsets_path).poll(crowd)

    assert [r["result"] for r in results] == ["hired"], results
    assert "acme-growth" in names(roster_path)


def test_a_duplicate_name_from_an_agent_is_refused_and_logged(
    tmp_path, roster_path, offsets_path
):
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("cos", cwd=tmp_path)))

    results = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [r["result"] for r in results] == ["refused"]
    assert results[0]["reason"] == "name_taken"
    assert next(d for d in load_roster(roster_path) if d.name == "cos").charter == COS_CHARTER
    assert [e for e in ledger(roster_path)
            if e.get("event") == "hire_refused" and e.get("reason") == "name_taken"]


def test_the_daemon_gives_the_harvester_a_way_to_seat_a_hire(monkeypatch, tmp_path):
    """D3's other half, wired. A hire writes a desk and nothing sits at it;
    measured live, two desks hired under one manager came up OFFLINE and stayed
    there, and the manager did the work itself rather than use them.

    The daemon supplies the surface's own `start_agent` -- the same `_start`
    the interview door goes through, so the workspace-trust gate cannot be
    skipped by this path the way it was once skipped by the other one.
    """
    from server import api, roster as roster_mod

    monkeypatch.setattr(app_mod, "_harvester", None)
    monkeypatch.setattr(app_mod, "HARVEST_OFFSETS", tmp_path / "offsets.json")
    monkeypatch.setattr(roster_mod, "DEFAULT_PATH", tmp_path / "roster.json")

    app_mod._harvest([])

    seat = app_mod._harvester.seat
    assert seat is not None, (
        "the daemon built a harvester that cannot seat what it hires; every "
        "hire becomes a desk nobody is at")
    assert getattr(seat, "__func__", None) is api.Surface.start_agent, seat
