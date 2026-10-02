"""S1: which Claude account a desk runs under, and where that account keeps its state.

The single-account deck must not change by one byte: `main` is implicit, lives
at `paths.CLAUDE_HOME`, and `env_for(main)` adds NOTHING to a subprocess call
(no `env=` keyword at all), so every existing spawn/resume/stop stays the call
it is today. A second account is a second `CLAUDE_CONFIG_DIR` -- MEASURED on
the box (claude 2.1.286, probe P1): that variable relocates `.credentials.json`,
`projects/`, `sessions/`, `jobs/` AND `.claude.json` into the directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server import accounts, paths, pretrust


def _write_registry(path: Path, rows) -> None:
    path.write_text(json.dumps(rows))
    path.chmod(0o600)


def test_main_is_implicit_and_lives_at_the_claude_home(tmp_path):
    found = accounts.registry(tmp_path / "accounts.json")
    assert [a.id for a in found] == ["main"]
    main = found[0]
    assert main.is_default
    assert main.kind == "subscription"
    assert main.config_dir == paths.CLAUDE_HOME


def test_main_dirs_are_todays_paths(tmp_path):
    d = accounts.dirs(accounts.get("main", tmp_path / "accounts.json"))
    assert d.config_dir == paths.CLAUDE_HOME
    assert d.sessions == paths.SESSIONS_DIR
    assert d.projects == paths.PROJECTS_DIR
    assert d.jobs == paths.CLAUDE_HOME / "jobs"
    assert d.credentials == paths.CLAUDE_HOME / ".credentials.json"
    # Today's pretrust target, whatever the conftest redirected it to.
    assert d.global_config == pretrust.DEFAULT_CONFIG
    assert d.keychain_service == "Claude Code-credentials"


def test_a_second_account_keeps_everything_inside_its_own_dir(tmp_path):
    reg = tmp_path / "accounts.json"
    work = tmp_path / "work"
    _write_registry(reg, [{"id": "work", "label": "Work", "kind": "subscription",
                           "config_dir": str(work), "added_at": 1.0}])
    acct = accounts.get("work", reg)
    assert acct is not None and not acct.is_default and acct.label == "Work"
    d = accounts.dirs(acct)
    assert d.config_dir == work
    assert d.sessions == work / "sessions"
    assert d.projects == work / "projects"
    assert d.jobs == work / "jobs"
    assert d.credentials == work / ".credentials.json"
    # MEASURED (P1): `.claude.json` moves INTO the config dir. Writing trust to
    # ~/.claude.json for this account would be the pretrust bug the plan names.
    assert d.global_config == work / ".claude.json"


def test_registry_order_is_main_first_then_the_file(tmp_path):
    reg = tmp_path / "accounts.json"
    _write_registry(reg, [
        {"id": "work", "label": "Work", "kind": "subscription",
         "config_dir": str(tmp_path / "w"), "added_at": 2.0},
        {"id": "api", "label": "API", "kind": "api",
         "config_dir": str(tmp_path / "a"), "added_at": 3.0},
    ])
    assert [a.id for a in accounts.registry(reg)] == ["main", "work", "api"]


@pytest.mark.parametrize("bad", [
    {"id": "main", "label": "x", "kind": "subscription", "config_dir": "/tmp/x"},
    {"id": "../evil", "label": "x", "kind": "subscription", "config_dir": "/tmp/x"},
    {"id": "ok", "label": "x", "kind": "bitcoin", "config_dir": "/tmp/x"},
    {"id": "ok", "label": "x", "kind": "subscription", "config_dir": "relative/dir"},
    {"id": "ok", "label": "x", "kind": "subscription"},
    "not an object",
])
def test_a_bad_row_is_skipped_never_fatal(tmp_path, bad):
    reg = tmp_path / "accounts.json"
    _write_registry(reg, [bad])
    assert [a.id for a in accounts.registry(reg)] == ["main"]


def test_an_unreadable_registry_is_just_main(tmp_path):
    reg = tmp_path / "accounts.json"
    reg.write_text("{not json")
    assert [a.id for a in accounts.registry(reg)] == ["main"]


def test_unknown_account_is_none(tmp_path):
    assert accounts.get("nope", tmp_path / "accounts.json") is None


def test_for_desk_falls_back_to_main(tmp_path):
    class Desk:  # a roster.Desk before S5, and one that names a gone account
        name = "d"
    reg = tmp_path / "accounts.json"
    assert accounts.for_desk(Desk(), reg).id == "main"
    Desk.account = "gone"
    assert accounts.for_desk(Desk(), reg).id == "main"
    _write_registry(reg, [{"id": "gone", "label": "G", "kind": "subscription",
                           "config_dir": str(tmp_path / "g"), "added_at": 1.0}])
    assert accounts.for_desk(Desk(), reg).id == "gone"


# -- the env: byte-identical for main, isolated for every other account ------


def test_main_adds_no_env_keyword_at_all(tmp_path):
    main = accounts.get("main", tmp_path / "accounts.json")
    assert accounts.env_kw(main) == {}
    assert accounts.env_for(main) is None


def test_a_second_account_gets_its_dir_the_bus_pin_and_no_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-main-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    reg = tmp_path / "accounts.json"
    _write_registry(reg, [{"id": "work", "label": "Work", "kind": "subscription",
                           "config_dir": str(tmp_path / "w"), "added_at": 1.0}])
    env = accounts.env_for(accounts.get("work", reg))
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "w")
    # The hooks find the bus through CLAUDE_CONFIG_DIR unless this is set, and
    # MEASURED (P2) it reaches the --bg worker's environ.
    assert env["DECK_BUS_DIR"] == str(paths.BUS_DIR)
    # Main's token in the env would sign account B's desks in as main.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert accounts.env_kw(accounts.get("work", reg)) == {"env": env}
    # The process env itself is untouched.
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-main-should-not-leak"


def test_registry_path_is_under_the_bus(tmp_path):
    assert accounts.REGISTRY == paths.BUS_DIR / "accounts.json"
