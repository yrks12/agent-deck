"""K3 -- a message to a sleeping desk wakes it, as the SAME session.

MEASURED on the box (2026-09-28, `~/.claude/daemon.log`): Claude Code's
background daemon retires an idle desk after about an hour --

    [bg] bg retire 25adf776: idle-prompt, idle 61m

-- and nothing in the deck ever brought one back. `Surface.send` queued the
owner's message and tried a socket that was no longer there; the record then
waited for a session that nobody started. His "hello" to atlas on 09-30 sat
from 04:20 until an engineer restarted the desk by hand at 04:37, and the
restart that did come was `spawn.start` -- a NEW session handed a 24,000-char
replay, which is where "the 91 oldest messages didn't reach me" came from.

MEASURED on the box (2026-09-30, throwaway desk `wake-probe`, claude 2.1.285):
`claude stop <job>` then `claude --bg --resume <session-id> "<note>"` with NO
other flags answered `woke session de8b1457 with its saved options` and the
board showed the same session id afterwards. The office hook did NOT hand the
queued message to that woken turn (the collector had not yet published the
session's name when the hook ran), so the waiting messages ride IN the wake
seed and are acked here -- see `tests/test_wake_resumes_same_session.py` for
the argv half.

Every seam is injected, so nothing here runs `claude`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from server import office, spawn, wake
from server.roster import Desk


SID = "de8b1457-afd1-4b8f-919b-cb2bf935bb4e"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    return tmp_path


def a_desk(name: str = "wake-probe", engine: str = "claude") -> Desk:
    return Desk(name=name, cwd="/srv/w/" + name, engine=engine, mission="probe",
                model="", created_at=0.0)


class Rig:
    """A Waker with every impure edge replaced by a recorder."""

    def __init__(self, *, live=(), job=True, desk=True, free=10**12,
                 resume_error=None, restart_error=None, now=1000.0,
                 running=False):
        self.live = set(live)
        self.running = running
        self.job = wake.Job(short=SID[:8], session_id=SID, cwd="/srv/w/wake-probe",
                            created_at="2026-09-30T04:54:09Z") if job else None
        self.desk = a_desk() if desk else None
        self.resumed: list[tuple] = []
        self.restarted: list[str] = []
        self.events: list[dict] = []
        self.resume_error = resume_error
        self.restart_error = restart_error
        self.now = now
        self.gate = None  # a threading.Event a resume waits on, for races
        self.waker = wake.Waker(
            live=lambda name: set(self.live),
            running=lambda session_id: self.running,
            last=lambda name: self.job,
            desk_of=lambda name: self.desk if self.desk and name == self.desk.name else None,
            resume=self._resume,
            restart=self._restart,
            record=self.events.append,
            free_bytes=lambda: free,
            clock=lambda: self.now,
        )

    def _resume(self, session_id, *, cwd, seed):
        if self.gate is not None:
            self.gate.wait(2)
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed.append((session_id, cwd, seed))
        return session_id[:8]

    def _restart(self, desk):
        if self.restart_error is not None:
            raise self.restart_error
        self.restarted.append(desk.name)
        return {"ok": True, "channel": "background", "detail": "",
                "agent_id": "newjob01", "pretrust": {}}


def records(bus: Path) -> list[dict]:
    path = bus / "messages.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- the four states ----------------------------------------------------------


def test_a_desk_with_a_live_session_is_left_alone(bus):
    rig = Rig(live={SID})
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "live"
    assert got.session_id == SID
    assert rig.resumed == [] and rig.restarted == []


def test_a_session_the_cli_still_runs_is_not_resumed_into_a_copy(bus):
    """MEASURED on the box: `claude --bg --resume <sid>` on a session that is
    still running printed "session 4ccfd598 is already running in the
    background, so this started a copy as 89bad41e". The board lags the CLI by
    a collector tick (and by a whole restart of the deck), so "not on the
    board" is not "not running" -- and a wake in that gap forks the desk."""
    rig = Rig(running=True)
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "live"
    assert got.session_id == SID
    assert rig.resumed == [] and rig.restarted == []


def test_running_reads_the_cli_session_files(tmp_path):
    import os
    (tmp_path / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": SID}))
    (tmp_path / "999999999.json").write_text(json.dumps(
        {"pid": 999999999, "sessionId": "dead-one"}))
    (tmp_path / "junk.json").write_text("{not json")
    assert wake.running(SID, sessions_dir=tmp_path) is True
    assert wake.running("dead-one", sessions_dir=tmp_path) is False
    assert wake.running("nobody", sessions_dir=tmp_path) is False
    assert wake.running(SID, sessions_dir=tmp_path / "missing") is False


def test_a_sleeping_desk_is_resumed_as_the_same_session(bus):
    queued = office.send("wake-probe", "reply with the word pong")
    rig = Rig()

    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")

    assert got.state == "woken"
    assert got.session_id == SID, "a wake must keep the desk's session id"
    [(session_id, cwd, seed)] = rig.resumed
    assert session_id == SID
    assert cwd == "/srv/w/wake-probe"
    assert seed.startswith(wake.WAKE_NOTE)
    assert "reply with the word pong" in seed
    assert rig.restarted == []
    # Handed over in the seed, so the hook must not hand it over again.
    assert queued["id"] in {r["ack"] for r in records(bus) if r.get("ack")}


def test_a_woken_desk_is_not_told_it_was_restarted(bus):
    office.send("wake-probe", "what is the codeword?")
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="owner_message")
    [(_, _, seed)] = rig.resumed
    everything = seed + "".join(r.get("text", "") for r in records(bus))
    assert spawn.RESTART_MARK not in everything
    assert spawn.ALREADY_SAID not in everything, "a wake replays nothing"


def test_the_owners_words_arrive_framed_as_his(bus):
    office.send("wake-probe", "ship it")
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="owner_message")
    [(_, _, seed)] = rig.resumed
    assert office.attribute("ship it", "owner") in seed


def test_a_peer_message_arrives_framed_as_a_peer(bus):
    office.send("wake-probe", "can you look at this", sender="atlas")
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="peer_message")
    [(_, _, seed)] = rig.resumed
    assert office.peer_mark("atlas") in seed
    assert office.OWNER_MARK not in seed


def test_only_this_desks_unacked_mail_is_in_the_seed(bus):
    old = office.send("wake-probe", "already handled")
    office.ack(old["id"])
    office.send("someone-else", "not yours")
    office.send("*", "a broadcast is the hook's")
    office.send("wake-probe", "the live one")
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="owner_message")
    [(_, _, seed)] = rig.resumed
    assert "the live one" in seed
    for absent in ("already handled", "not yours", "a broadcast is the hook's"):
        assert absent not in seed


def test_the_seed_is_capped_and_what_did_not_fit_stays_queued(bus):
    first = office.send("wake-probe", "a" * (wake.SEED_MAX - 100))
    second = office.send("wake-probe", "b" * 5000)
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="owner_message")
    [(_, _, seed)] = rig.resumed
    acked = {r["ack"] for r in records(bus) if r.get("ack")}
    assert first["id"] in acked
    assert second["id"] not in acked, "an uncarried message must stay queued"
    assert "b" * 5000 not in seed


def test_no_resumable_session_falls_back_to_a_restart(bus):
    rig = Rig(job=False)
    got = rig.waker.ensure_awake("wake-probe", reason="routine")
    assert got.state == "restarted"
    assert rig.restarted == ["wake-probe"]
    assert rig.resumed == []


def test_a_cli_that_refuses_the_resume_falls_back_to_a_restart(bus):
    rig = Rig(resume_error=spawn.SpawnError("resume_failed", "No conversation found"))
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "restarted"
    assert "resume_failed" in got.detail
    assert rig.restarted == ["wake-probe"]


def test_an_expired_login_is_refused_not_restarted(bus):
    queued = office.send("wake-probe", "hello?")
    rig = Rig(resume_error=spawn.SpawnError("oauth_expired", "Please run /login"))
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "refused"
    assert got.detail.startswith("oauth_expired")
    assert rig.restarted == [], "a restart cannot log in either"
    assert queued["id"] not in {r.get("ack") for r in records(bus)}


def test_a_full_disk_is_refused(bus):
    rig = Rig(free=10)
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "refused"
    assert got.detail.startswith("disk_full")
    assert rig.resumed == [] and rig.restarted == []


def test_a_name_that_is_not_a_desk_is_refused(bus):
    rig = Rig(desk=False)
    got = rig.waker.ensure_awake("some-session", reason="owner_message")
    assert got.state == "refused"
    assert got.detail.startswith("not_a_desk")
    assert rig.resumed == [] and rig.restarted == []


def test_a_restart_that_fails_is_refused_with_its_reason(bus):
    rig = Rig(job=False, restart_error=spawn.SpawnError("spawn_failed", "boom"))
    got = rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert got.state == "refused"
    assert got.detail.startswith("spawn_failed")


def test_an_unknown_reason_is_a_programming_error(bus):
    with pytest.raises(ValueError):
        Rig().waker.ensure_awake("wake-probe", reason="because")


# -- one wake per desk ---------------------------------------------------------


def test_concurrent_callers_produce_exactly_one_wake(bus):
    rig = Rig()
    rig.gate = threading.Event()
    results: list = []

    def call():
        results.append(rig.waker.ensure_awake("wake-probe", reason="owner_message"))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    rig.gate.set()
    for t in threads:
        t.join(3)

    assert len(rig.resumed) == 1
    states = sorted(r.state for r in results)
    assert states == ["live"] * 7 + ["woken"]


def test_a_desk_mid_resume_reads_live_for_thirty_seconds(bus):
    rig = Rig()
    assert rig.waker.ensure_awake("wake-probe", reason="owner_message").state == "woken"
    rig.now += wake.MID_RESUME_SECONDS - 1
    again = rig.waker.ensure_awake("wake-probe", reason="routine")
    assert again.state == "live" and again.session_id == SID
    rig.now += 2
    assert rig.waker.ensure_awake("wake-probe", reason="routine").state == "woken"
    assert len(rig.resumed) == 2


# -- the record ----------------------------------------------------------------


def test_every_wake_is_recorded_in_the_event_log(bus):
    rig = Rig()
    rig.waker.ensure_awake("wake-probe", reason="peer_message")
    [event] = rig.events
    assert event["type"] == "wake"
    assert event["desk"] == "wake-probe"
    assert event["state"] == "woken"
    assert event["session_id"] == SID
    assert event["reason"] == "peer_message"
    assert isinstance(event["ts"], float)


def test_a_live_no_op_is_not_recorded(bus):
    rig = Rig(live={SID})
    rig.waker.ensure_awake("wake-probe", reason="owner_message")
    assert rig.events == []


# -- where the last session comes from ---------------------------------------


def write_job(jobs: Path, short: str, *, name: str, session_id: str, cwd: str,
              created: str) -> None:
    (jobs / short).mkdir(parents=True)
    (jobs / short / "state.json").write_text(json.dumps({
        "name": name, "sessionId": session_id, "daemonShort": short, "cwd": cwd,
        "createdAt": created, "state": "blocked"}))


def write_transcript(projects: Path, cwd: str, session_id: str) -> None:
    folder = projects / "".join(c if c.isalnum() else "-" for c in cwd)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text("{}\n")


def test_the_last_job_is_the_newest_one_at_that_desk(tmp_path):
    jobs, projects = tmp_path / "jobs", tmp_path / "projects"
    for short, created in (("aaaa1111", "2026-09-28T06:00:00Z"),
                           ("bbbb2222", "2026-09-30T04:37:50Z")):
        sid = short + "-0000-0000-0000-000000000000"
        write_job(jobs, short, name="atlas", session_id=sid, cwd="/w/atlas",
                  created=created)
        write_transcript(projects, "/w/atlas", sid)
    write_job(jobs, "cccc3333", name="acme-lead", session_id="cccc3333-x",
              cwd="/w/acme", created="2026-09-30T05:00:00Z")

    job = wake.last_job("atlas", jobs_dir=jobs, projects_dir=projects)
    assert job is not None and job.short == "bbbb2222"
    assert job.cwd == "/w/atlas"


def test_the_last_job_is_the_one_that_talked_last_not_the_one_made_last(tmp_path):
    """MEASURED on the box. `wake-probe` had two jobs: de8b1457 (created 04:54,
    the desk's own session, idle-retired at 05:56) and b3f54a1c (created 04:55,
    a copy made once and stopped a minute later). Ordering by creation woke the
    copy -- the board had shown de8b1457 all along, so the "same session" was
    the wrong one. The session that spoke last is the transcript written last."""
    import os
    jobs, projects = tmp_path / "jobs", tmp_path / "projects"
    for short, created, mtime in (("de8b1457", "2026-09-30T04:54:09Z", 2000.0),
                                  ("b3f54a1c", "2026-09-30T04:55:32Z", 1000.0)):
        sid = short + "-0000-0000-0000-000000000000"
        write_job(jobs, short, name="wake-probe", session_id=sid, cwd="/w/p",
                  created=created)
        write_transcript(projects, "/w/p", sid)
        os.utime(projects / "-w-p" / f"{sid}.jsonl", (mtime, mtime))
    job = wake.last_job("wake-probe", jobs_dir=jobs, projects_dir=projects)
    assert job is not None and job.short == "de8b1457"


def test_a_job_with_no_transcript_is_not_resumable(tmp_path):
    jobs, projects = tmp_path / "jobs", tmp_path / "projects"
    write_job(jobs, "aaaa1111", name="atlas", session_id="aaaa1111-x",
              cwd="/w/atlas", created="2026-09-30T04:00:00Z")
    assert wake.last_job("atlas", jobs_dir=jobs, projects_dir=projects) is None


def test_no_jobs_directory_means_nothing_to_resume(tmp_path):
    assert wake.last_job("atlas", jobs_dir=tmp_path / "nope",
                         projects_dir=tmp_path) is None


# -- the fact A2 renders as ASLEEP ---------------------------------------------


def test_asleep_means_no_live_session_and_a_resumable_one(tmp_path, monkeypatch):
    jobs, projects = tmp_path / "jobs", tmp_path / "projects"
    write_job(jobs, "aaaa1111", name="atlas", session_id="aaaa1111-x",
              cwd="/w/atlas", created="2026-09-30T04:00:00Z")
    write_transcript(projects, "/w/atlas", "aaaa1111-x")
    monkeypatch.setattr(wake, "JOBS_DIR", jobs)
    monkeypatch.setattr(wake, "PROJECTS_DIR", projects)

    monkeypatch.setattr(office, "live_session_ids", lambda name: set())
    assert wake.is_asleep("atlas") is True
    assert wake.is_asleep("never-started") is False

    monkeypatch.setattr(office, "live_session_ids", lambda name: {"aaaa1111-x"})
    assert wake.is_asleep("atlas") is False
