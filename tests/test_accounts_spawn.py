"""S3: every claude subprocess for a desk runs as that desk's ACCOUNT.

Before this, spawn/resume/stop called `subprocess.run` with no `env` and read
one jobs dir. A desk under a second account would be started, stopped and
resumed under `main` -- the wrong login, the wrong daemon, the wrong jobs.

The other half is the regression pin: for `main` the call carries NO `env`
keyword at all, so the single-account deck is byte-identical.

MEASURED (probe P2/P2b, box, claude 2.1.286): a session resumed into a second
config dir keeps its session id AND its daemonShort. So the same short can sit
in two jobs dirs after a move -- the one that is not `stopped` is the live one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import accounts, office, roster, spawn


class _Ran:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


BANNER = "backgrounded · ab12cd34 · atlas\n"


@pytest.fixture
def two(tmp_path, monkeypatch):
    """`main` with its jobs in tmp, and `work` registered beside it."""
    reg = tmp_path / "accounts.json"
    work = tmp_path / "work"
    (work / "jobs").mkdir(parents=True)
    reg.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                "config_dir": str(work), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", reg)
    monkeypatch.setattr(spawn, "JOBS_DIR", tmp_path / "main-jobs")
    (tmp_path / "main-jobs").mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-main")
    return {"main_jobs": tmp_path / "main-jobs", "work": work}


def _job(root: Path, short: str, **state) -> None:
    (root / short).mkdir(parents=True)
    (root / short / "state.json").write_text(json.dumps(
        {"daemonShort": short, **state}))


def _capture(monkeypatch, result: _Ran) -> list:
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return result

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    return seen


DESK = dict(name="atlas", cwd="/nonexistent/atlas", engine="claude", mission="m")


def _quiet_gates(monkeypatch):
    monkeypatch.setattr(spawn, "_vouch", lambda desk, roster_path: spawn.pretrust.Trust(True, "ok"))
    monkeypatch.setattr(spawn, "_approval_settings", lambda desk: "")
    monkeypatch.setattr(spawn, "_boss_address", lambda desk: "")
    monkeypatch.setattr(spawn.rules, "mark_seen", lambda name: None)


# -- start ---------------------------------------------------------------------


def test_main_desk_starts_with_no_env_keyword(two, monkeypatch, tmp_path):
    _quiet_gates(monkeypatch)
    seen = _capture(monkeypatch, _Ran(0, BANNER))
    spawn.spawn_background(roster.Desk(**DESK), roster_path=tmp_path / "roster.json")
    assert "env" not in seen[-1][1], "main must stay byte-identical"


def test_work_desk_starts_in_its_own_config_dir_without_mains_token(two, monkeypatch, tmp_path):
    _quiet_gates(monkeypatch)
    monkeypatch.setattr(accounts, "for_desk",
                        lambda desk, path=None: accounts.get("work"))
    seen = _capture(monkeypatch, _Ran(0, BANNER))
    spawn.spawn_background(roster.Desk(**DESK), roster_path=tmp_path / "roster.json")
    env = seen[-1][1]["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(two["work"])
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_terminal_window_cannot_carry_a_second_account(two, monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "for_desk",
                        lambda desk, path=None: accounts.get("work"))
    monkeypatch.setattr(spawn.shutil, "which", lambda name: "/usr/bin/osascript")
    with pytest.raises(spawn.SpawnError) as err:
        spawn.spawn_terminal(roster.Desk(**DESK), roster_path=tmp_path / "roster.json")
    assert err.value.reason == "account_needs_background"


# -- stop ----------------------------------------------------------------------


def test_stop_of_a_main_job_has_no_env_keyword(two, monkeypatch):
    _job(two["main_jobs"], "aaaa1111", sessionId="s-a", state="idle")
    seen = _capture(monkeypatch, _Ran(0))
    assert spawn.stop_job("aaaa1111")
    assert seen[-1][0] == ["claude", "stop", "aaaa1111"]
    assert "env" not in seen[-1][1]


def test_stop_routes_to_the_account_whose_job_is_live(two, monkeypatch):
    # After a move: same short in both dirs, main's copy stopped.
    _job(two["main_jobs"], "bbbb2222", sessionId="s-b", state="stopped")
    _job(two["work"] / "jobs", "bbbb2222", sessionId="s-b", state="idle")
    seen = _capture(monkeypatch, _Ran(0))
    spawn.stop_job("bbbb2222")
    assert seen[-1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(two["work"])


def test_stop_with_an_explicit_account_uses_it(two, monkeypatch):
    seen = _capture(monkeypatch, _Ran(0))
    spawn.stop_job("cccc3333", account="work")
    assert seen[-1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(two["work"])


# -- resume --------------------------------------------------------------------


def test_resume_of_a_main_session_has_no_env_keyword(two, monkeypatch):
    _job(two["main_jobs"], "dddd4444", sessionId="s-d", state="idle")
    seen = _capture(monkeypatch, _Ran(0, BANNER))
    spawn.resume_background("s-d", cwd="/nonexistent", seed="N")
    assert seen[-1][0] == ["claude", "--bg", "--resume", "s-d", "N"]
    assert "env" not in seen[-1][1]


def test_resume_finds_the_account_holding_the_session(two, monkeypatch):
    _job(two["work"] / "jobs", "eeee5555", sessionId="s-e", state="done")
    seen = _capture(monkeypatch, _Ran(0, BANNER))
    spawn.resume_background("s-e", cwd="/nonexistent", seed="N")
    assert seen[-1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(two["work"])


def test_resume_into_an_explicit_account(two, monkeypatch):
    _job(two["main_jobs"], "ffff6666", sessionId="s-f", state="stopped")
    seen = _capture(monkeypatch, _Ran(0, BANNER))
    spawn.resume_background("s-f", cwd="/nonexistent", seed="N", account="work")
    assert seen[-1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(two["work"])


# -- the job registry is read across accounts ----------------------------------


def test_jobs_for_sees_a_live_session_in_the_second_account(two, monkeypatch):
    _job(two["work"] / "jobs", "abab7777", sessionId="s-live", name="atlas", state="idle")
    monkeypatch.setattr(office, "live_session_ids", lambda name: {"s-live"})
    assert spawn.jobs_for("atlas") == ["abab7777"]


def test_jobs_of_desk_sees_both_accounts_once_each(two, monkeypatch):
    _job(two["main_jobs"], "m1111111", sessionId="s-1", name="atlas", state="done")
    _job(two["work"] / "jobs", "w2222222", sessionId="s-2", name="atlas", state="idle")
    _job(two["work"] / "jobs", "w3333333", sessionId="s-3", name="atlas", state="stopped")
    monkeypatch.setattr(office, "live_session_ids", lambda name: set())
    assert sorted(spawn.jobs_of_desk("atlas")) == ["m1111111", "w2222222"]


def test_the_connector_reloader_finds_a_job_in_the_second_account(two):
    """The class sweep: `connectors._job_state` (the reload's stop/resume
    lookup) read only main's jobs dir, so a reload of a desk under `work`
    would find no job and silently skip the stop."""
    from server import connectors

    _job(two["main_jobs"], "abcd0000", sessionId="s-r", state="stopped",
         respawnFlags=["--old"])
    _job(two["work"] / "jobs", "abcd0000", sessionId="s-r", state="idle",
         respawnFlags=["--name", "atlas"])
    state = connectors._job_state("s-r")
    assert state is not None and state["respawnFlags"] == ["--name", "atlas"]
