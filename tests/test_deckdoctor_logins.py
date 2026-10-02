"""S11: the box says a Claude login is about to die THREE DAYS before it does.

MEASURED on the box (2026-10-01): main's `refreshTokenExpiresAt` is
2026-10-26. That, not the 8-hour access token, is the real login deadline:
after it, every desk on that account dies on start and no daemon can refresh
it. deckdoctor (every 10 minutes, deploy/deckdoctor.timer) goes red at T-3
days for each account and names it, its desks, and the exact command. A login
already gone while desks still run on it is the same check, louder.

It reads ONE number per account -- `refreshTokenExpiresAt` -- and never a
token: nothing about a login leaves the file.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400.0
NOW = 1_790_000_000.0


def load():
    loader = importlib.machinery.SourceFileLoader("deckdoctor", str(ROOT / "bin" / "deckdoctor"))
    spec = importlib.util.spec_from_loader("deckdoctor", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _creds(folder: Path, refresh_in_days: float) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "SECRET-ACCESS", "refreshToken": "SECRET-REFRESH",
        "expiresAt": (NOW + 3600) * 1000,
        "refreshTokenExpiresAt": (NOW + refresh_in_days * DAY) * 1000}}))


def _box(tmp_path, main_days: float, work_days: float | None = None):
    home = tmp_path / ".claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    _creds(home, main_days)
    rows = []
    if work_days is not None:
        _creds(tmp_path / ".claude-accounts" / "work", work_days)
        rows.append({"id": "work", "label": "Work", "kind": "subscription",
                     "config_dir": str(tmp_path / ".claude-accounts" / "work")})
    (bus / "accounts.json").write_text(json.dumps(rows))
    (bus / "roster.json").write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/x", "engine": "claude", "mission": "m"},
        {"name": "scout", "cwd": "/x", "engine": "claude", "mission": "m",
         "account": "work"}]}))
    return home


def test_green_with_more_than_three_days_left(tmp_path):
    dd = load()
    got = dd.check_logins({"logins": lambda: dd._logins(_box(tmp_path, 25.0, 300.0)),
                           "now": lambda: NOW})
    assert got["ok"] is True


def test_red_at_three_days_naming_the_account_its_desks_and_the_command(tmp_path):
    dd = load()
    home = _box(tmp_path, 2.5)
    got = dd.check_logins({"logins": lambda: dd._logins(home), "now": lambda: NOW})
    assert got["ok"] is False
    assert "main" in got["detail"] and "atlas" in got["detail"]
    assert "deckctl login" in got["detail"] and "--account" not in got["detail"]
    assert "SECRET" not in json.dumps(got)


def test_the_second_account_gets_its_own_command(tmp_path):
    dd = load()
    home = _box(tmp_path, 25.0, 1.0)
    got = dd.check_logins({"logins": lambda: dd._logins(home), "now": lambda: NOW})
    assert got["ok"] is False
    assert "work" in got["detail"] and "scout" in got["detail"]
    assert "deckctl login --account work" in got["detail"]


def test_an_expired_login_with_desks_on_it_says_they_are_dead(tmp_path):
    dd = load()
    home = _box(tmp_path, -1.0)
    got = dd.check_logins({"logins": lambda: dd._logins(home), "now": lambda: NOW})
    assert got["ok"] is False and "expired" in got["detail"]


def test_a_box_without_the_seam_is_not_red(tmp_path):
    dd = load()
    assert dd.check_logins({"now": lambda: NOW})["ok"] is True


def test_the_check_is_in_every_run_and_has_a_runbook_line():
    dd = load()
    assert "logins" in dd.RUNBOOK
    assert "check_logins" in Path(dd.__file__).read_text().split("def run(")[1]


def test_the_wake_refusal_names_the_account(monkeypatch, tmp_path):
    """A desk on `work` that cannot log in must not read as main's login."""
    from server import accounts, spawn

    reg = tmp_path / "accounts.json"
    reg.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                "config_dir": str(tmp_path / "w"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", reg)

    class _Ran:
        returncode, stdout, stderr = 1, "", "Not logged in. Please run /login"

    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: _Ran())
    try:
        spawn.resume_background("s-1", cwd="/nonexistent", seed="N", account="work")
    except spawn.SpawnError as exc:
        assert exc.reason == "oauth_expired"
        assert "account work" in exc.detail
    else:
        raise AssertionError("a refused login must raise")
