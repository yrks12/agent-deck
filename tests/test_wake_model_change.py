"""A desk whose roster model changed is started fresh on its next wake.

MEASURED on the box, 2026-10-02 (token diet): a wake resumes a job WITH ITS
SAVED OPTIONS, `--model` included (`spawn.resume_background`), and passing a
new `--model` on a resume forks a copy. So setting `model: sonnet` on a
routine desk changed the roster and nothing else: the desk woke on Opus again
and again. When the model the roster asks for differs from the one the job
was saved with, the waker skips the resume and starts the desk fresh -- the
same fallback a refused resume already takes.
"""

from __future__ import annotations

import pytest

from server import office, wake
from server.roster import Desk

SID = "de8b1457-afd1-4b8f-919b-cb2bf935bb4e"


@pytest.fixture(autouse=True)
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")


def waker(*, model: str, saved: str | None, log: list):
    desk = Desk(name="social", cwd="/srv/w/social", engine="claude", mission="m",
                model=model, created_at=0.0)
    job = wake.Job(short=SID[:8], session_id=SID, cwd="/srv/w/social",
                   created_at="2026-10-02T00:00:00Z")
    return wake.Waker(
        live=lambda name: set(), running=lambda sid: False,
        last=lambda name: job, desk_of=lambda name: desk,
        resume=lambda sid, *, cwd, seed: log.append("resume") or sid[:8],
        restart=lambda d: log.append("restart") or {"agent_id": "newjob01"},
        saved_model=lambda j: saved,
        pending=lambda name: [], ack=lambda mid: None, record=lambda e: None,
        free_bytes=lambda: 10**12, clock=lambda: 1000.0)


def test_a_changed_model_starts_the_desk_fresh():
    log: list = []
    got = waker(model="sonnet", saved="", log=log).ensure_awake("social", reason="routine")
    assert log == ["restart"] and got.state == "restarted"
    assert "model" in got.detail


@pytest.mark.parametrize("model,saved", [
    ("sonnet", "sonnet"),  # already on it
    ("", ""),              # no model set: the CLI default, as always
    ("sonnet", None),      # saved options unreadable: resume, never guess
])
def test_an_unchanged_or_unknown_model_resumes_as_itself(model, saved):
    log: list = []
    waker(model=model, saved=saved, log=log).ensure_awake("social", reason="routine")
    assert log == ["resume"]


def test_the_saved_model_is_read_from_the_jobs_respawn_flags(monkeypatch):
    job = wake.Job(short=SID[:8], session_id=SID, cwd="/w", created_at="")
    states = {SID: {"respawnFlags": ["--name", "s", "--model", "sonnet", "--x"]}}
    monkeypatch.setattr(wake, "_job_state", lambda sid: states.get(sid))
    assert wake.saved_model(job) == "sonnet"
    states[SID] = {"respawnFlags": ["--name", "s"]}
    assert wake.saved_model(job) == ""
    states.clear()
    assert wake.saved_model(job) is None
