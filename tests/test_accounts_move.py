"""S8: move a desk to another Claude account -- never mid-turn, never twice.

`mover.Mover.move(desk, to)`, under the same per-desk lock the waker uses:

  1. gate: idle >= 20s, no subagent running, the job not mid-turn (its
     `tempo`) -- else `not_idle`. Asleep passes; an open ask does not hold.
  2. stop it in its account, and wait until the CLI says it is gone;
  3. make sure its transcript is readable in the target account;
  4. vouch for its folder in the target account's `.claude.json`;
  5. resume it there with its own flags and a one-line note;
  6. on success, the roster says the new account and the bus says
     `account_move`;
  7. on failure, resume it where it was (no flags: its job is still there),
     leave the roster alone, and answer `move_failed`.

MEASURED (probes P2/P2b, box, claude 2.1.286): `claude --bg --resume <sid>
[flags] <note>` in a second config dir continues the SAME session id, with its
memory, when that dir has no job for it; passing the flags there carries the
desk's name instead of losing it.
"""

from __future__ import annotations

import json
import pytest
from server import accounts, mover, roster, spawn, wake

SID = "c91c8d85-4bad-4529-a026-2f2ab6956b43"
NOW = 1_000_000.0


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)
    return path


class Rig:
    def __init__(self, *, card=None, live=True, job=True, stops=True,
                 resume_raises=None, resume_id="c91c8d85", job_state="idle",
                 target_has_job=False, tempo=None):
        self.calls: list = []
        self.saved: dict = {}
        self.events: list = []
        self.card = card if card is not None else (
            {"name": "atlas", "state": "IDLE", "state_since": NOW - 60,
             "agents_running": 0, "attention": None} if live else None)
        self._live = live
        self._running = live
        self._stops = stops
        self._job = wake.Job(short="c91c8d85", session_id=SID, cwd="/srv/atlas",
                             created_at="", account="main") if job else None
        self.m = mover.Mover(
            card_of=lambda name: self.card,
            desk_of=lambda name: roster.Desk(name="atlas", cwd="/srv/atlas",
                                             engine="claude", mission="m"),
            last_job=lambda name: self._job,
            live=lambda name: {SID} if self._live else set(),
            running=lambda sid: self._running,
            job_state=lambda sid: {"state": job_state, "tempo": tempo, "respawnFlags":
                                   ["--name", "atlas", "--permission-mode", "bypassPermissions"]},
            jobs_for=lambda name: ["c91c8d85"] if self._live else [],
            has_job=lambda sid, account: target_has_job,
            stop=self._stop, resume_into=self._resume_into, resume_back=self._resume_back,
            ensure_transcript=lambda src, dst, cwd, sid: self.calls.append(("transcript", dst.id)),
            vouch=lambda acct, cwd: self.calls.append(("vouch", acct.id)),
            save_account=lambda name, to: self.saved.update({name: to}),
            record=self.events.append, clock=lambda: NOW, sleep=lambda s: None)
        self.resume_raises, self.resume_id = resume_raises, resume_id

    def _stop(self, short, **kw):
        self.calls.append(("stop", short, kw.get("account")))
        if self._stops:
            self._running = False
            self._live = False
        return True

    def _resume_into(self, sid, *, cwd, flags, note, account):
        self.calls.append(("resume_into", sid, tuple(flags), account))
        if self.resume_raises:
            raise self.resume_raises
        return self.resume_id

    def _resume_back(self, sid, *, cwd, seed, account):
        self.calls.append(("resume_back", sid, account))
        return "c91c8d85"


def test_an_idle_desk_moves_in_order_and_the_roster_follows(reg):
    rig = Rig()
    got = rig.m.move("atlas", "work")
    assert got == {"ok": True, "moved": True, "desk": "atlas", "from": "main",
                   "to": "work", "session_id": SID}
    assert [c[0] for c in rig.calls] == ["stop", "transcript", "vouch", "resume_into"]
    assert rig.calls[0] == ("stop", "c91c8d85", "main")
    assert rig.calls[3] == ("resume_into", SID,
                            ("--name", "atlas", "--permission-mode", "bypassPermissions"),
                            "work")
    assert rig.saved == {"atlas": "work"}
    (event,) = rig.events
    assert event["event"] == "account_move" and event["from"] == "main" and event["to"] == "work"


@pytest.mark.parametrize("card", [
    {"name": "atlas", "state": "WORKING", "state_since": NOW - 600, "agents_running": 0},
    {"name": "atlas", "state": "IDLE", "state_since": NOW - 5, "agents_running": 0},
    {"name": "atlas", "state": "IDLE", "state_since": NOW - 600, "agents_running": 1},
    {"name": "atlas", "state": "NEEDS_YOU", "state_since": NOW - 600, "agents_running": 0},
])
def test_a_desk_that_is_not_idle_is_never_moved(reg, card):
    rig = Rig(card=card)
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work")
    assert (err.value.status, err.value.reason) == (409, "not_idle")
    assert rig.calls == [] and rig.saved == {}


def test_a_job_whose_turn_ended_moves_though_its_state_still_says_working(reg):
    """MEASURED on the box 2026-10-01: turn ended, board IDLE 19 min, job
    `state: working` + `tempo: idle`; refused every 30 s for hours at 94%."""
    rig = Rig(job_state="working", tempo="idle")
    assert rig.m.move("atlas", "work")["moved"] is True
    assert rig.saved == {"atlas": "work"}


def test_an_open_ask_does_not_hold_the_desk(reg):
    assert not hasattr(mover, "open_asks")
    assert Rig().m.move("atlas", "work")["moved"] is True


@pytest.mark.parametrize("tempo", ["active", "blocked"])
def test_a_job_mid_turn_by_its_tempo_holds_the_desk(reg, tempo):
    rig = Rig(job_state="working", tempo=tempo)
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work", at_boundary=True)
    assert err.value.reason == "not_idle" and rig.calls == []


def test_at_a_turn_boundary_the_board_lag_is_not_waited_for(reg):
    card = {"name": "atlas", "state": "WORKING", "state_since": NOW - 1, "agents_running": 0}
    rig = Rig(card=card, job_state="working", tempo="idle")
    with pytest.raises(mover.MoveError):
        rig.m.move("atlas", "work")  # the sweep still wants 20 s idle on the board
    assert rig.m.turn_over("atlas") is True
    assert rig.m.move("atlas", "work", at_boundary=True)["moved"] is True


def test_at_a_turn_boundary_a_running_subagent_still_holds_it(reg):
    card = {"name": "atlas", "state": "WORKING", "state_since": NOW - 1, "agents_running": 1}
    with pytest.raises(mover.MoveError):
        Rig(card=card, tempo="idle").m.move("atlas", "work", at_boundary=True)


def test_a_job_mid_turn_holds_the_desk_even_if_the_board_lags(reg):
    rig = Rig(job_state="working")
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work")
    assert err.value.reason == "not_idle" and rig.calls == []


def test_an_asleep_desk_only_changes_account_and_stays_asleep(reg):
    """MEASURED on the box, 2026-10-01 20:48: the first automatic failover
    moved 17 desks, and resuming each asleep one put 17 live sessions on an
    8 GB box (1.6 GB left) and spent a turn on each. An asleep desk now just
    changes account; its next wake resumes it there (`wake.Waker`)."""
    rig = Rig(live=False)
    got = rig.m.move("atlas", "work")
    assert got["moved"] is True and got["session_id"] == SID
    assert rig.calls == [] and rig.saved == {"atlas": "work"}


def test_a_desk_that_never_ran_only_changes_its_account(reg):
    rig = Rig(live=False, job=False)
    got = rig.m.move("atlas", "work")
    assert got["moved"] is True and got["session_id"] == ""
    assert rig.calls == [] and rig.saved == {"atlas": "work"}


def test_a_failed_resume_goes_back_where_it_was_and_the_roster_stays(reg):
    rig = Rig(resume_raises=spawn.SpawnError("resume_failed", "nope"))
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work")
    assert (err.value.status, err.value.reason) == (502, "move_failed")
    assert rig.calls[-1] == ("resume_back", SID, "main")
    assert rig.saved == {} and rig.events == []


def test_a_fork_in_the_new_account_is_stopped_and_the_desk_goes_back(reg):
    """A copy under a new id is a second brain. Stop it there, go home."""
    rig = Rig(resume_id="0b697cee")
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work")
    assert err.value.reason == "move_failed"
    assert ("stop", "0b697cee", "work") in rig.calls
    assert rig.calls[-1] == ("resume_back", SID, "main")
    assert rig.saved == {}


def test_a_session_that_will_not_stop_is_not_resumed_anywhere_else(reg):
    rig = Rig(stops=False)
    with pytest.raises(mover.MoveError) as err:
        rig.m.move("atlas", "work")
    assert err.value.reason == "move_failed"
    assert [c[0] for c in rig.calls] == ["stop"], "never two brains"
    assert rig.saved == {}


def test_unknown_account_and_same_account(reg):
    with pytest.raises(mover.MoveError) as err:
        Rig().m.move("atlas", "nope")
    assert (err.value.status, err.value.reason) == (404, "no_account")
    rig = Rig()
    assert rig.m.move("atlas", "main")["moved"] is False and rig.calls == []


def test_back_to_an_account_that_still_holds_its_job_passes_no_flags(reg):
    """MEASURED on the box, 2026-10-01, wake-probe: main -> work moved the
    session; work -> main answered "the CLI started a copy as 52e43823",
    because main still held the (stopped) job for that session, and a resume
    WITH flags where a job exists forks it -- the same rule `spawn.
    resume_background` was built on. So flags go only where there is no job."""
    rig = Rig(target_has_job=True)
    rig.m.move("atlas", "work")
    assert ("resume_into", SID, (), "work") in rig.calls
    assert rig.saved == {"atlas": "work"}
