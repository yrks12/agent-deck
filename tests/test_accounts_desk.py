"""S5: a desk names its account, and the trust it needs is written THERE.

`roster.Desk.account` ("" = the default account) is what spawn resolves an
account from. MEASURED (probe P1, claude 2.1.286): under CLAUDE_CONFIG_DIR the
CLI reads `.claude.json` from INSIDE that dir, so the workspace trust and the
bypass-permissions acceptance a second-account desk needs must be written to
`<account dir>/.claude.json`. Writing them to `~/.claude.json` (what pretrust
did regardless) leaves that desk on the trust dialog with nobody to answer.

The board and the API carry it: a card says which account its session is on,
and every `/v1/agents` row gains `account` and `running_account`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import accounts, collector, pretrust, roster, spawn
from server.sources.sessions import RawSession


@pytest.fixture
def work(tmp_path, monkeypatch):
    reg = tmp_path / "accounts.json"
    root = tmp_path / "work"
    root.mkdir()
    reg.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                "config_dir": str(root), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", reg)
    return root


def test_the_roster_keeps_a_desks_account(tmp_path):
    path = tmp_path / "roster.json"
    roster.save_roster(path, [
        roster.Desk(name="a", cwd="/w", engine="claude", mission="m", account="work"),
        roster.Desk(name="b", cwd="/w", engine="claude", mission="m"),
    ])
    loaded = {d.name: d.account for d in roster.load_roster(path)}
    assert loaded == {"a": "work", "b": ""}


def test_a_roster_written_before_accounts_loads_as_default(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        {"name": "old", "cwd": "/w", "engine": "claude", "mission": "m"}]}))
    (desk,) = roster.load_roster(path)
    assert desk.account == "" and accounts.for_desk(desk).id == "main"


def _seat(tmp_path) -> Path:
    cwd = tmp_path / "desk-ws"
    cwd.mkdir()
    return cwd


def test_a_work_desk_is_vouched_for_in_the_work_accounts_config(work, tmp_path):
    cwd = _seat(tmp_path)
    desk = roster.Desk(name="w", cwd=str(cwd), engine="claude", mission="m",
                       account="work")
    verdict = spawn._vouch(desk, tmp_path / "roster.json")
    assert verdict.ok, verdict
    theirs = json.loads((work / ".claude.json").read_text())
    assert theirs["projects"][str(cwd.resolve())]["hasTrustDialogAccepted"] is True
    assert theirs[pretrust.BYPASS_KEY] is True
    mine = pretrust.DEFAULT_CONFIG
    assert not mine.exists() or str(cwd.resolve()) not in mine.read_text()


def test_a_main_desk_is_still_vouched_for_in_the_default_config(work, tmp_path):
    cwd = _seat(tmp_path)
    desk = roster.Desk(name="m", cwd=str(cwd), engine="claude", mission="m")
    assert spawn._vouch(desk, tmp_path / "roster.json").ok
    assert str(cwd.resolve()) in pretrust.DEFAULT_CONFIG.read_text()
    assert not (work / ".claude.json").exists()


def test_a_resume_into_work_vouches_in_works_config(work, tmp_path, monkeypatch):
    cwd = _seat(tmp_path)

    class _Ran:
        returncode, stdout, stderr = 0, "backgrounded · ab12cd34 · w\n", ""

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: _Ran())
    monkeypatch.setattr(roster, "DEFAULT_PATH", tmp_path / "roster.json")
    spawn.resume_background("s-1", cwd=str(cwd), seed="N", account="work")
    theirs = json.loads((work / ".claude.json").read_text())
    assert str(cwd.resolve()) in theirs["projects"]


def test_the_board_card_says_which_account_its_session_is_on():
    raw = RawSession(pid=1, session_id="s", cwd="/w", name="w", status="idle",
                     kind="background", started_at=0, account="work")
    card = _card_for(raw)
    assert card["account"] == "work"


def _card_for(raw: RawSession) -> dict:
    col = collector.Collector.__new__(collector.Collector)
    # Only the pieces `_build` touches; every reader returns "nothing".
    from types import SimpleNamespace
    tail = SimpleNamespace(poll=lambda: SimpleNamespace(
        usage=SimpleNamespace(as_dict=lambda: {}), git_branch="", model="",
        version="", last_assistant_full="", last_assistant_id="",
        last_prompt="", last_assistant="", turns=0,
        activity=SimpleNamespace(tool="", detail="", running=False)))
    col._track = lambda s: SimpleNamespace(tail=tail, agents=SimpleNamespace(
        poll=lambda forced_states=None: []))
    col._derive = lambda s, sig, now: ("IDLE", 0.0)
    col.bus = SimpleNamespace(clear_attention=lambda sid: None)
    col.speaker = SimpleNamespace(check=lambda *a, **k: None)
    return col._build(raw, {}, 0.0)


def test_agent_rows_carry_account_and_running_account(tmp_path, monkeypatch):
    from server import api as api_mod, office
    from server.sources import comms as comms_mod

    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        {"name": "on-work", "cwd": "/w", "engine": "claude", "mission": "m",
         "account": "work"},
        {"name": "on-main", "cwd": "/w", "engine": "claude", "mission": "m"}]}))
    snap = {"generated_at": 1.0, "sessions": [
        {"session_id": "s-w", "pid": 7, "name": "on-work", "cwd": "/w",
         "project": "w", "state": "IDLE", "state_since": 0.0, "account": "work"}]}
    surface = api_mod.Surface(snapshot=lambda: snap, comms=comms_mod.CommsIndex(),
                              roster_path=path, prefs_path=tmp_path / "prefs.json")
    surface.refresh()
    rows = {a["name"]: a for a in surface.agents()}
    assert rows["on-work"]["account"] == "work"
    assert rows["on-work"]["running_account"] == "work"
    assert rows["on-main"]["account"] == "main"
    assert rows["on-main"]["running_account"] is None
