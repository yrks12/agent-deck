"""S4: every reader that asks "is a session there?" looks in EVERY account.

A desk under a second account writes its `<pid>.json`, its peer key and its
job into THAT account's dir. A reader that looks only at main's would say
nobody is there -- and the wake that follows resumes the same session a second
time: the same brain twice. The plan's ordering rule: S4 lands before any desk
runs outside `main`.

Readers swept: the session scanner (the board), `app._desk_of` (approval
attribution), `wake.running`, `wake.last_job`, `manager.key_file` (the socket
token), and the Waker's resume, which must go back to the account it found.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server import accounts, manager, paths, roster, spawn, wake
from server import app as app_mod
from server.sources import sessions

PID = os.getpid()


@pytest.fixture
def work(tmp_path, monkeypatch):
    reg = tmp_path / "accounts.json"
    root = tmp_path / "work"
    for sub in ("sessions", "jobs", "projects"):
        (root / sub).mkdir(parents=True)
    reg.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                "config_dir": str(root), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", reg)
    main = tmp_path / "main"
    for sub in ("sessions", "jobs", "projects"):
        (main / sub).mkdir(parents=True)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", main / "sessions")
    monkeypatch.setattr(app_mod, "SESSIONS_DIR", main / "sessions")
    monkeypatch.setattr(wake, "SESSIONS_DIR", main / "sessions")
    monkeypatch.setattr(wake, "JOBS_DIR", main / "jobs")
    monkeypatch.setattr(wake, "PROJECTS_DIR", main / "projects")
    monkeypatch.setattr(manager, "SESSIONS_DIR", main / "sessions")
    wake._job_cache.clear()
    return {"root": root, "main": main}


def _session_file(folder: Path, sid: str, name: str = "atlas") -> None:
    (folder / f"{PID}.json").write_text(json.dumps(
        {"pid": PID, "sessionId": sid, "cwd": "/srv/atlas", "name": name,
         "status": "idle", "kind": "background", "startedAt": 1}))


def _job(jobs: Path, projects: Path, short: str, sid: str, *, state="idle",
         cwd="/srv/atlas", name="atlas", transcript=True) -> Path:
    (jobs / short).mkdir()
    path = jobs / short / "state.json"
    path.write_text(json.dumps({"daemonShort": short, "sessionId": sid, "name": name,
                                "cwd": cwd, "state": state, "createdAt": "2026-10-01"}))
    if transcript:
        slug = projects / paths.slug_for(cwd)
        slug.mkdir(exist_ok=True)
        (slug / f"{sid}.jsonl").write_text("{}\n")
    return path


# -- the board -----------------------------------------------------------------


def test_the_scanner_sees_a_session_in_the_second_account(work, monkeypatch):
    monkeypatch.setattr(sessions.SessionScanner, "_refresh_cli", lambda self: None)
    _session_file(work["root"] / "sessions", "s-work")
    found = sessions.SessionScanner().scan()
    assert [(s.session_id, s.account) for s in found] == [("s-work", "work")]


def test_a_main_session_is_tagged_main(work, monkeypatch):
    monkeypatch.setattr(sessions.SessionScanner, "_refresh_cli", lambda self: None)
    _session_file(work["main"] / "sessions", "s-main")
    found = sessions.SessionScanner().scan()
    assert [(s.session_id, s.account) for s in found] == [("s-main", "main")]


def test_approval_attribution_finds_a_second_account_session(work, monkeypatch):
    monkeypatch.setattr(app_mod, "_state", {"sessions": []})
    _session_file(work["root"] / "sessions", "s-work", name="atlas")
    assert app_mod._desk_of({"session_id": "s-work"}) == "atlas"


# -- the wake --------------------------------------------------------------------


def test_running_sees_a_session_in_the_second_account(work):
    _session_file(work["root"] / "sessions", "s-work")
    assert wake.running("s-work") is True
    assert wake.running("s-none") is False


def test_last_job_finds_a_job_in_the_second_account(work):
    _job(work["root"] / "jobs", work["root"] / "projects", "w1", "s-w")
    job = wake.last_job("atlas")
    assert job is not None and job.session_id == "s-w" and job.account == "work"


def test_last_job_in_main_is_tagged_main(work):
    _job(work["main"] / "jobs", work["main"] / "projects", "m1", "s-m")
    job = wake.last_job("atlas")
    assert job is not None and job.account == "main"


def test_after_a_move_the_live_copy_wins(work):
    """Same session, same transcript (the projects symlink), a job in each
    account. MEASURED (P2): the short is the same in both. The job written
    last is the desk's -- a stop and a resume both rewrite state.json."""
    projects = work["main"] / "projects"
    old = _job(work["main"] / "jobs", projects, "x1", "s-x", state="stopped")
    new = _job(work["root"] / "jobs", projects, "x1", "s-x", state="idle",
               transcript=False)
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    # B's projects is a symlink to main's in a real install.
    (work["root"] / "projects").rmdir()
    (work["root"] / "projects").symlink_to(projects)
    job = wake.last_job("atlas")
    assert job is not None and job.account == "work"


def test_the_waker_resumes_in_the_account_it_found_the_job(work):
    calls = []
    job = wake.Job(short="w1", session_id="s-w", cwd="/srv/atlas", created_at="",
                   account="work")
    waker = wake.Waker(
        live=lambda name: set(), running=lambda sid: False, last=lambda name: job,
        desk_of=lambda name: roster.Desk(name="atlas", cwd="/srv/atlas",
                                         engine="claude", mission="m", account="work"),
        resume=lambda sid, **kw: calls.append(kw) or "w1",
        pending=lambda name: [], ack=lambda mid: None, record=lambda e: None,
        free_bytes=lambda: 1 << 40)
    assert waker.ensure_awake("atlas", reason="owner_message").state == "woken"
    assert calls[-1]["account"] == "work"


def test_the_waker_passes_no_account_for_main(work):
    """Byte-identical: the resume call for main is the call it always was."""
    calls = []
    job = wake.Job(short="m1", session_id="s-m", cwd="/srv/atlas", created_at="")
    waker = wake.Waker(
        live=lambda name: set(), running=lambda sid: False, last=lambda name: job,
        desk_of=lambda name: roster.Desk(name="atlas", cwd="/srv/atlas",
                                         engine="claude", mission="m"),
        resume=lambda sid, **kw: calls.append(kw) or "m1",
        pending=lambda name: [], ack=lambda mid: None, record=lambda e: None,
        free_bytes=lambda: 1 << 40)
    waker.ensure_awake("atlas", reason="owner_message")
    assert set(calls[-1]) == {"cwd", "seed"}


# -- the socket token -------------------------------------------------------------


def test_main_sessions_dir_follows_the_claude_home():
    """It was hardcoded to ~/.claude/sessions while every other reader honoured
    CLAUDE_CONFIG_DIR."""
    import importlib
    fresh = importlib.reload(manager)
    try:
        assert fresh.SESSIONS_DIR == paths.SESSIONS_DIR
    finally:
        importlib.reload(manager)


def test_key_file_finds_the_peer_key_in_the_second_account(work):
    sock = Path("/tmp/does-not-matter.sock")
    import hashlib
    digest = hashlib.sha256(str(sock).encode()).hexdigest()
    key = work["root"] / "sessions" / f"{PID}.{digest}.key"
    key.write_text(json.dumps({"peerToken": "0" * 32}))
    assert manager.key_file(PID, sock) == key
