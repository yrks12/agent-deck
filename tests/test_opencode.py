"""OpenCode session discovery and card generation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from server.collector import Collector
from server.sources import opencode
from server.sources.opencode import OpencodeScanner, OpencodeSession


class _TmpOpencode:
    """A temp log file and DB plus helpers to populate them."""

    def __init__(self, tmp_path: Path):
        self.log = tmp_path / "opencode.log"
        self.db = tmp_path / "opencode.db"

    def make_db(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                directory TEXT NOT NULL,
                title TEXT NOT NULL,
                version TEXT NOT NULL,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                tokens_cache_read INTEGER DEFAULT 0,
                tokens_cache_write INTEGER DEFAULT 0,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                time_archived INTEGER,
                agent TEXT,
                model TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def insert_session(self, **kw) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            INSERT INTO session (
                id, project_id, directory, title, version,
                tokens_input, tokens_output, tokens_cache_read, tokens_cache_write,
                time_created, time_updated, agent, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kw["id"],
                kw.get("project_id", "pid"),
                kw["directory"],
                kw["title"],
                kw.get("version", "1.0"),
                kw.get("tokens_input", 0),
                kw.get("tokens_output", 0),
                kw.get("tokens_cache_read", 0),
                kw.get("tokens_cache_write", 0),
                kw["time_created"],
                kw["time_updated"],
                kw.get("agent", "build"),
                kw.get("model", '{"id":"kimi-k2.7-code"}'),
            ),
        )
        conn.commit()
        conn.close()


@pytest.fixture
def tmp_opencode(tmp_path: Path):
    return _TmpOpencode(tmp_path)


def _fake_process(cwd: str, pid: int = 99999):
    return opencode._ProcessInfo(pid=pid, cwd=cwd)


def _monkeypatch_processes(monkeypatch, procs):
    monkeypatch.setattr(
        opencode, "_discover_opencode_processes", lambda: procs
    )
    monkeypatch.setattr(opencode, "_pid_alive", lambda pid: True)


def test_scanner_reads_db_and_log(tmp_opencode, monkeypatch):
    tmp_opencode.make_db()
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
    tmp_opencode.insert_session(
        id="ses_abc",
        directory="/tmp/proj",
        title="Test session",
        time_created=1780000000000,
        time_updated=1780000000000,
        tokens_input=100,
        tokens_output=50,
        tokens_cache_read=20,
        tokens_cache_write=5,
        model=json.dumps({"id": "kimi-k2.7-code"}),
    )
    tmp_opencode.log.write_text(
        f'timestamp={ts} level=INFO run=run1 message=loop session.id=ses_abc\n'
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/proj", 99999)])

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    result = scanner.scan()

    assert len(result) == 1
    s = result[0]
    assert s.session_id == "ses_abc"
    assert s.pid == 99999
    assert s.cwd == "/tmp/proj"
    assert s.name == "Test session"
    assert s.status == "busy"
    assert s.model == "kimi-k2.7-code"
    assert s.version == "1.0"
    assert s.agent == "build"
    assert s.tokens_input == 100
    assert s.tokens_cache_read == 20
    assert s.last_log_at > now - 10


def test_scanner_degrades_when_db_locked(tmp_opencode, monkeypatch):
    """If the DB is unreadable, the scanner still surfaces sessions from log + process."""
    ts = "2026-08-13T15:55:44.755Z"
    tmp_opencode.log.write_text(
        f'timestamp={ts} level=INFO run=run1 message=loop session.id=ses_from_log cwd=/tmp/fromlog\n'
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/fromlog", 88888)])

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    result = scanner.scan()

    assert len(result) == 1
    s = result[0]
    assert s.session_id == "ses_from_log"
    assert s.pid == 88888
    assert s.cwd == "/tmp/fromlog"
    assert s.source == "opencode+log"
    assert s.name == "opencode-from_log"


def test_scanner_skips_dead_session(tmp_opencode, monkeypatch):
    tmp_opencode.make_db()
    tmp_opencode.insert_session(
        id="ses_dead",
        directory="/tmp/dead",
        title="Dead",
        time_created=1780000000000,
        time_updated=1780000000000,
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/alive", 77777)])

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    assert scanner.scan() == []


def test_scanner_model_falls_back_to_log(tmp_opencode, monkeypatch):
    tmp_opencode.make_db()
    tmp_opencode.insert_session(
        id="ses_nodbmodel",
        directory="/tmp/proj",
        title="No model",
        time_created=1780000000000,
        time_updated=1780000000000,
        model="not-json",
    )
    tmp_opencode.log.write_text(
        'timestamp=2026-08-13T15:55:44.755Z level=INFO run=run1 '
        'message=stream modelID=kimi-k2.7-code session.id=ses_nodbmodel\n'
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/proj", 66666)])

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    result = scanner.scan()
    assert result[0].model == "kimi-k2.7-code"


def test_scanner_idle_when_log_old(tmp_opencode, monkeypatch):
    tmp_opencode.make_db()
    tmp_opencode.insert_session(
        id="ses_idle",
        directory="/tmp/proj",
        title="Idle",
        time_created=1780000000000,
        time_updated=1780000000000,
    )
    old = "2026-08-10T12:00:00.000Z"
    tmp_opencode.log.write_text(
        f'timestamp={old} level=INFO run=run1 message=loop session.id=ses_idle\n'
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/proj", 55555)])

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    result = scanner.scan()
    assert result[0].status == "idle"


def test_log_tail_is_incremental(tmp_opencode, monkeypatch):
    tmp_opencode.make_db()
    tmp_opencode.insert_session(
        id="ses_inc",
        directory="/tmp/proj",
        title="Incremental",
        time_created=1780000000000,
        time_updated=1780000000000,
    )
    _monkeypatch_processes(monkeypatch, [_fake_process("/tmp/proj", 44444)])
    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)

    tmp_opencode.log.write_text(
        'timestamp=2026-08-13T15:55:00.000Z level=INFO run=r1 message=loop session.id=ses_inc\n'
    )
    r1 = scanner.scan()
    assert r1[0].last_log_at > 0

    # Append a second line; the scanner should not re-read the first.
    with tmp_opencode.log.open("a") as fh:
        fh.write(
            'timestamp=2026-08-13T15:55:05.000Z level=INFO run=r1 message=loop session.id=ses_inc\n'
        )
    r2 = scanner.scan()
    assert r2[0].last_log_at == pytest.approx(
        opencode._parse_timestamp("2026-08-13T15:55:05.000Z"), abs=0.1
    )


def test_collector_build_opencode_card_shape():
    collector = Collector()
    session = OpencodeSession(
        pid=62164,
        session_id="ses_0042db",
        cwd="/Users/samcarter/Projects/claude-dashbaord",
        name="Enable Claude in new worktree dashboard",
        status="busy",
        kind="interactive",
        started_at=1786636292029,
        updated_at=1786636839383,
        version="1.18.18",
        model="kimi-k2.7-code",
        agent="build",
        tokens_input=30945,
        tokens_output=7137,
        tokens_cache_read=370534,
        tokens_cache_write=0,
        last_log_at=time.time(),
    )
    card = collector._build_opencode(session, time.time())

    assert card["session_id"] == "ses_0042db"
    assert card["pid"] == 62164
    assert card["name"] == "Enable Claude in new worktree dashboard"
    assert card["cwd"] == "/Users/samcarter/Projects/claude-dashbaord"
    assert card["project"] == "claude-dashbaord"
    assert card["source"] == "opencode"
    assert card["state"] == "WORKING"
    assert card["raw_status"] == "busy"
    assert card["started_at"] == pytest.approx(1786636292029 / 1000, abs=0.1)
    assert card["model"] == "kimi-k2.7-code"
    assert card["version"] == "1.18.18"
    assert card["activity"] == {"tool": "build", "detail": "", "running": True}
    assert card["usage"] == {
        "input": 30945 + 370534 + 0,
        "output": 7137,
        "cache_read": 370534,
        "cache_write": 0,
    }
    assert card["subagents"] == []
    assert card["agents_running"] == 0
    assert card["turns"] == 0
    assert card["last_prompt"] == ""
    assert card["last_assistant"] == ""
    assert card["attention"] is None
    # The KEY, not the value. The value is asserted properly in the test below,
    # against a checkout this test makes. See that test for why.
    assert "git_branch" in card


def test_an_opencode_card_names_the_branch_the_session_is_actually_on(tmp_path):
    """The branch on the card is what tells two sessions in one repo apart from
    two sessions in separate worktrees, so it has to be the RIGHT branch.

    It used to be `assert card["git_branch"]` -- truthy -- against a hard-coded
    `/Users/samcarter/Projects/claude-dashbaord`. That is not a check, it is a
    reading of whoever's machine the suite happens to be on: MEASURED on the
    Linux box, where that path does not exist, the field came back `''` and the
    assertion failed for a reason that had nothing to do with the collector. The
    same shape would have passed on the Mac while publishing the wrong branch
    entirely, because any non-empty string is truthy.

    So the checkout is made here, on a branch named by this test, and the card
    has to say that name. Host-independent, and it now actually fails if the
    collector stops resolving the branch.
    """
    checkout = tmp_path / "a-repo"
    checkout.mkdir()
    # The commit is not optional. `office.branch_for` asks for
    # `rev-parse --abbrev-ref HEAD`, and on an unborn HEAD git exits 128 and the
    # branch comes back "" -- which is the same empty answer this test was
    # written to stop accepting. Identity is passed on the command line so the
    # test never depends on, or touches, anybody's git config.
    for argv in (["git", "init", "-q"],
                 ["git", "checkout", "-q", "-b", "slice/known-branch"],
                 ["git", "-c", "user.name=deck-suite",
                  "-c", "user.email=suite@example.invalid",
                  "commit", "-q", "--allow-empty", "-m", "root"]):
        done = subprocess.run(argv, cwd=checkout, capture_output=True,
                              text=True, timeout=20)
        assert done.returncode == 0, done.stderr

    card = Collector()._build_opencode(
        OpencodeSession(pid=62164, session_id="ses_branch",
                        cwd=str(checkout), name="on a known branch",
                        status="busy", kind="interactive",
                        started_at=1786636292029, updated_at=1786636839383,
                        version="1.18.18", model="kimi-k2.7-code", agent="build",
                        tokens_input=0, tokens_output=0, tokens_cache_read=0,
                        tokens_cache_write=0, last_log_at=time.time()),
        time.time())

    assert card["git_branch"] == "slice/known-branch"


def test_scanner_limits_sessions_to_live_processes_per_dir(tmp_opencode, monkeypatch):
    """A directory with old DB rows and fewer live processes must not show
    every stale session as live.
    """
    tmp_opencode.make_db()
    for i, (title, updated) in enumerate(
        [
            ("current", 1780000001000),
            ("subagent-old", 1780000000000),
            ("subagent-older", 1770000000000),
        ]
    ):
        tmp_opencode.insert_session(
            id=f"ses_{i}",
            directory="/tmp/proj",
            title=title,
            time_created=updated,
            time_updated=updated,
        )
    # Only two live processes in this directory.
    _monkeypatch_processes(
        monkeypatch,
        [
            _fake_process("/tmp/proj", 11111),
            _fake_process("/tmp/proj", 22222),
        ],
    )

    scanner = OpencodeScanner(log_path=tmp_opencode.log, db_path=tmp_opencode.db)
    result = scanner.scan()
    titles = {s.name for s in result}
    assert len(result) == 2, f"expected 2 sessions, got {len(result)}: {titles}"
    assert "current" in titles
    assert "subagent-older" not in titles
