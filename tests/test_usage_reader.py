"""C4 usage reader: token sources, plan map, every limits[] window, no token leaks."""

from __future__ import annotations

import io
import json
import logging
import time
import urllib.error
from pathlib import Path

import pytest

from server.sources import usage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "oauth_usage.json").read_text())
TOKEN = "sk-ant-oat01-SECRET-TOKEN-FIXTURE"


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


def _creds(path: Path, *, token=TOKEN, expires_ms=None, sub="max",
           tier="default_claude_max_20x") -> Path:
    if expires_ms is None:
        expires_ms = (time.time() + 3600) * 1000
    path.mkdir(parents=True, exist_ok=True)
    f = path / ".credentials.json"
    f.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "REFRESH-NEVER-USED",
        "expiresAt": expires_ms, "subscriptionType": sub, "rateLimitTier": tier}}))
    return f


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CACHE_FILE", tmp_path / "usage-cache.json")
    monkeypatch.setattr(usage, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(usage, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(usage, "_keychain_payload", lambda: None)
    return tmp_path


def _serve(monkeypatch, body=FIXTURE, seen=None):
    def fake(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return FakeResponse(body)
    monkeypatch.setattr(usage.urllib.request, "urlopen", fake)


# ── token sources ───────────────────────────────────────────────────────────

def test_token_comes_from_config_dir_credentials_file_first(isolated):
    _creds(isolated / "cfg", token="from-config")
    _creds(isolated / "home" / ".claude", token="from-home")
    assert usage._read_token() == "from-config"


def test_token_falls_back_to_home_claude_credentials(isolated):
    _creds(isolated / "home" / ".claude", token="from-home")
    assert usage._read_token() == "from-home"


def test_keychain_is_the_last_resort(isolated, monkeypatch):
    monkeypatch.setattr(usage, "_keychain_payload",
                        lambda: {"claudeAiOauth": {"accessToken": "from-keychain"}})
    assert usage._read_token() == "from-keychain"
    _creds(isolated / "home" / ".claude", token="from-home")
    assert usage._read_token() == "from-home"


def test_no_login_anywhere_is_no_login(isolated, monkeypatch):
    _serve(monkeypatch)
    out = usage.UsageReader().poll()
    assert out["available"] is False
    assert out["reason_code"] == "no_login"


def test_request_uses_the_token_and_beta_header(isolated, monkeypatch):
    _creds(isolated / "cfg")
    seen: list = []
    _serve(monkeypatch, seen=seen)
    usage.UsageReader().poll()
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert seen[0].get_header("Anthropic-beta") == "oauth-2025-04-20"


# ── plan map ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sub,tier,name", [
    ("pro", "default_claude_pro", "Pro"),
    ("max", "default_claude_max_5x", "Max 5×"),
    ("max", "default_claude_max_20x", "Max 20×"),
    ("team", None, "Team"),
    ("enterprise", None, "Enterprise"),
    ("weird_plan", None, "Weird Plan"),
])
def test_plan_name_closed_map(sub, tier, name):
    assert usage.plan_info(sub, tier)["name"] == name


def test_plan_info_carries_raw_fields():
    assert usage.plan_info("max", "default_claude_max_20x") == {
        "name": "Max 20×", "subscription": "max", "tier": "default_claude_max_20x"}


def test_unknown_login_has_no_plan():
    assert usage.plan_info(None, None) is None


# ── windows ─────────────────────────────────────────────────────────────────

def test_every_limits_kind_becomes_a_window_and_unknown_kinds_are_dropped(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    out = usage.UsageReader().poll()
    by = {w["key"]: w for w in out["limits"]}
    assert list(by) == ["session", "weekly_all", "weekly_scoped:Fable"]
    assert by["session"]["percent"] == 8.0 and by["session"]["label"] == "5-hour session"
    assert by["weekly_all"]["severity"] == "warning" and by["weekly_all"]["label"] == "Weekly"
    scoped = by["weekly_scoped:Fable"]
    assert scoped["label"] == "Weekly · Fable" and scoped["scope"] == "Fable"
    assert scoped["resets_at"] == "2026-10-06T22:00:00+00:00"


def test_legacy_windows_stay_session_and_weekly_for_the_old_board(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    out = usage.UsageReader().poll()
    assert [(w["key"], w["percent"]) for w in out["windows"]] == [("session", 8.0), ("weekly", 41.0)]
    assert out["available"] is True and out["stale"] is False
    assert out["extra_usage_enabled"] is False


def test_flat_fallback_when_limits_absent(isolated, monkeypatch):
    _creds(isolated / "cfg")
    body = {k: v for k, v in FIXTURE.items() if k != "limits"}
    _serve(monkeypatch, body)
    out = usage.UsageReader().poll()
    assert [(w["key"], w["percent"]) for w in out["limits"]] == [("session", 8.0), ("weekly_all", 41.0)]


def test_breakdown_and_extra_usage_and_plan(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    out = usage.UsageReader().poll()
    assert out["breakdown"] == {"as_of": "2026-09-30T20:21:09.083345+00:00", "rows": [
        {"key": "claude_code", "label": "Claude Code", "percent": 37},
        {"key": "chat", "label": "Chats", "percent": 0}]}
    assert out["extra_usage"] == {"enabled": False, "spend_limit_reached": False}
    assert out["subscription"]["name"] == "Max 20×"
    assert out["fetched_at"] and out["next_refresh_at"] >= out["fetched_at"] + usage.REFRESH_SECONDS - 1


def test_no_breakdown_is_null(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch, {k: v for k, v in FIXTURE.items() if k != "seven_day_breakdown"})
    assert usage.UsageReader().poll()["breakdown"] is None


# ── staleness, never refresh ────────────────────────────────────────────────

def test_expired_token_is_not_sent_and_keeps_last_numbers_stale(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    reader = usage.UsageReader()
    reader.poll()
    _creds(isolated / "cfg", expires_ms=(time.time() - 60) * 1000)
    seen: list = []
    _serve(monkeypatch, seen=seen)
    reader._fetched_at = float("-inf")
    out = reader.poll()
    assert seen == []  # never call with a dead token, never refresh
    assert out["reason_code"] == "token_expired" and out["stale"] is True
    assert out["available"] is True and len(out["limits"]) == 3


def test_expired_token_with_no_history_is_unavailable(isolated, monkeypatch):
    _creds(isolated / "cfg", expires_ms=(time.time() - 60) * 1000)
    out = usage.UsageReader().poll()
    assert out["available"] is False and out["reason_code"] == "token_expired"
    assert out["subscription"]["name"] == "Max 20×"


def test_a_cli_refresh_is_picked_up_on_the_next_poll(isolated, monkeypatch):
    _creds(isolated / "cfg", expires_ms=(time.time() - 60) * 1000)
    reader = usage.UsageReader()
    assert reader.poll()["reason_code"] == "token_expired"
    _creds(isolated / "cfg", token="refreshed-by-cli")
    _serve(monkeypatch)
    reader._fetched_at = float("-inf")
    assert reader.poll()["stale"] is False


def test_rate_limit_keeps_numbers_and_backs_off(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    reader = usage.UsageReader()
    reader.poll()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 429, "rl", {"Retry-After": "900"}, io.BytesIO(b""))
    monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
    reader._fetched_at = float("-inf")
    out = reader.poll()
    assert out["reason_code"] == "rate_limited" and out["stale"] is True
    assert out["windows"] and reader._backoff == 900.0


def test_unreachable_reason_code(isolated, monkeypatch):
    _creds(isolated / "cfg")
    monkeypatch.setattr(usage.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert usage.UsageReader().poll()["reason_code"] == "unreachable"


def test_disk_cache_reload_is_cached(isolated, monkeypatch):
    _creds(isolated / "cfg")
    _serve(monkeypatch)
    usage.UsageReader().poll()
    again = usage.UsageReader()
    assert again._cache["reason_code"] == "cached" and again._cache["stale"] is True
    assert again._cache["limits"]


# ── the token never leaks ───────────────────────────────────────────────────

def test_token_never_in_logs_cache_output_or_exception_text(isolated, monkeypatch, caplog, capsys):
    _creds(isolated / "cfg")
    caplog.set_level(logging.DEBUG)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 500, f"bad {TOKEN}", {}, io.BytesIO(b""))
    monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
    reader = usage.UsageReader()
    bad = reader.poll()
    _serve(monkeypatch)
    reader._fetched_at = float("-inf")
    good = reader.poll()
    blobs = [caplog.text, capsys.readouterr().out, capsys.readouterr().err,
             json.dumps(bad), json.dumps(good)]
    if usage.CACHE_FILE.exists():
        blobs.append(usage.CACHE_FILE.read_text())
    assert not any(TOKEN in b or "REFRESH-NEVER-USED" in b for b in blobs)
