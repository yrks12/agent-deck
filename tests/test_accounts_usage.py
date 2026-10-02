"""S6b: one usage meter per Claude account, honest about an idle login, and
labelled unofficial.

* Each account's meter reads ITS OWN `.credentials.json` -- never main's token
  for the second account's numbers.
* MEASURED on the box: an account with no daemon running is never refreshed,
  so its access token lapses about 8h after its last use. That is `idle_token`
  while `refreshTokenExpiresAt` is still ahead -- the next desk that runs there
  refreshes it -- and `token_expired` only when the login itself is gone.
* `claude.usage_meter = false` sends nothing anywhere: `meter_off`.
* `GET /v1/usage` keeps its top-level C4 fields, adds `accounts[]` and
  `policy`, and says `unofficial: true` (the endpoint is undocumented).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import accounts, deckconfig, roster
from server import api as api_mod
from server import usage_api
from server.sources import usage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "oauth_usage.json").read_text())
AUTH = {"Authorization": "Bearer deck-token"}


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _creds(folder: Path, token: str, *, expires_in=3600.0, refresh_in=86400.0 * 20):
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "NEVER-USED",
        "expiresAt": (now + expires_in) * 1000,
        "refreshTokenExpiresAt": (now + refresh_in) * 1000,
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}}))


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CACHE_FILE", tmp_path / "usage-cache.json")
    monkeypatch.setattr(usage, "CONFIG_DIR", tmp_path / "main")
    monkeypatch.setattr(usage, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(usage, "BUS_DIR", tmp_path)
    monkeypatch.setattr(usage, "_keychain_payload", lambda: None)
    reg = tmp_path / "accounts.json"
    reg.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", reg)
    _creds(tmp_path / "main", "MAIN-TOKEN")
    _creds(tmp_path / "work", "WORK-TOKEN")
    seen: list[str] = []

    def fake(req, timeout=None):
        seen.append(req.get_header("Authorization"))
        return _Resp(FIXTURE)

    monkeypatch.setattr(usage.urllib.request, "urlopen", fake)
    return {"root": tmp_path, "seen": seen}


def test_the_second_accounts_meter_uses_its_own_token(two):
    got = usage.UsageReader(account=accounts.get("work")).poll()
    assert got["available"] is True
    assert two["seen"] == ["Bearer WORK-TOKEN"]


def test_an_idle_account_is_idle_token_not_expired(two):
    _creds(two["root"] / "work", "WORK-TOKEN", expires_in=-60)
    got = usage.UsageReader(account=accounts.get("work")).poll()
    assert got["reason_code"] == "idle_token"
    assert two["seen"] == [], "a lapsed token is never sent"
    assert got["refresh_expires_at"] > time.time()


def test_a_gone_login_is_token_expired(two):
    _creds(two["root"] / "work", "WORK-TOKEN", expires_in=-60, refresh_in=-60)
    got = usage.UsageReader(account=accounts.get("work")).poll()
    assert got["reason_code"] == "token_expired"


def test_an_api_account_has_no_plan_meter_and_sends_nothing(two, tmp_path, monkeypatch):
    """S10: a Console (API-key) account is billed per token. There is no
    5-hour or weekly window to read, and its credential is not an OAuth token
    to send to the plan endpoint."""
    reg = tmp_path / "accounts.json"
    reg.write_text(json.dumps([{"id": "api", "label": "API", "kind": "api",
                                "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    got = usage.UsageReader(account=accounts.get("api")).poll()
    assert got["reason_code"] == "api_account" and got["available"] is False
    assert two["seen"] == []


def test_the_meter_off_sends_nothing(two):
    got = usage.UsageReader(enabled=False).poll()
    assert got["reason_code"] == "meter_off" and got["available"] is False
    assert two["seen"] == []


def test_the_switch_is_read_from_deck_toml(monkeypatch):
    off = deckconfig.DeckConfig(claude=deckconfig.ClaudeSection(usage_meter=False))
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: off)
    assert usage.meter_enabled() is False
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: deckconfig.DeckConfig())
    assert usage.meter_enabled() is True


def test_the_api_has_one_meter_per_account_and_says_unofficial(two, monkeypatch, tmp_path):
    path = tmp_path / "roster.json"
    roster.save_roster(path, [
        roster.Desk(name="a", cwd="/w", engine="claude", mission="m"),
        roster.Desk(name="b", cwd="/w", engine="claude", mission="m", account="work")])
    monkeypatch.setattr(roster, "DEFAULT_PATH", path)
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: deckconfig.DeckConfig(
        accounts=deckconfig.AccountsSection(policy="failover", failover_allowed=True,
                                            failover_order=("main", "work"))))
    monkeypatch.setenv(api_mod.TOKEN_ENV, "deck-token")
    app = FastAPI()
    api_mod.register(app, surface=api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=Path("/nonexistent/r.json"), prefs_path=Path("/nonexistent/p.json")),
        background=False)

    class _Off:
        def poll(self):
            return {"available": False, "windows": []}

    app.include_router(usage_api.build_router(reader=usage.UsageReader(), opencode=_Off()))
    body = TestClient(app).get("/v1/usage", headers=AUTH).json()

    assert body["unofficial"] is True
    assert body["available"] is True and body["windows"], "C4 top level unchanged"
    by_id = {a["id"]: a for a in body["accounts"]}
    assert list(by_id) == ["main", "work"]
    assert by_id["main"]["desks"] == ["a"] and by_id["work"]["desks"] == ["b"]
    for row in body["accounts"]:
        assert set(row) >= {"id", "label", "kind", "plan", "available", "stale", "reason",
                            "windows", "extra_usage", "fetched_at", "refresh_expires_at",
                            "desks"}
        assert row["windows"]
    assert body["policy"] == {"mode": "failover", "threshold_pct": 90,
                              "failover_allowed": True, "default": "main",
                              "failover_order": ["main", "work"], "api_account": ""}
