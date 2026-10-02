"""GET /v1/usage (C4): auth, shape, other providers, and the old board's feed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import collector, usage_api
from server.sources import usage

TOKEN = "deck-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "oauth_usage.json").read_text())


class StubReader:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.polls = 0

    def poll(self) -> dict:
        self.polls += 1
        return self.payload


@pytest.fixture
def real_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CACHE_FILE", tmp_path / "usage-cache.json")
    monkeypatch.setattr(usage, "_read_token", lambda: "t")
    monkeypatch.setattr(usage, "_read_login", lambda: {
        "accessToken": "t", "expiresAt": 9e15,
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"})
    reader = usage.UsageReader()
    monkeypatch.setattr(reader, "_request", lambda token: FIXTURE)
    return reader.poll()


def _client(monkeypatch, anthropic, opencode=None):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=Path("/nonexistent/r.json"), prefs_path=Path("/nonexistent/p.json")),
        background=False)
    app.include_router(usage_api.build_router(
        reader=anthropic, opencode=opencode or StubReader({"available": False, "windows": []})))
    return TestClient(app)


def test_requires_bearer(monkeypatch, real_payload):
    c = _client(monkeypatch, StubReader(real_payload))
    assert c.get("/v1/usage").status_code == 401
    assert c.get("/v1/usage", headers=AUTH).status_code == 200


def test_body_matches_contract_c4(monkeypatch, real_payload):
    body = _client(monkeypatch, StubReader(real_payload)).get("/v1/usage", headers=AUTH).json()
    assert body["available"] is True and body["stale"] is False and body["reason"] is None
    assert body["plan"] == {"name": "Max 20×", "subscription": "max",
                            "tier": "default_claude_max_20x"}
    assert [w["key"] for w in body["windows"]] == ["session", "weekly_all", "weekly_scoped:Fable"]
    assert set(body["windows"][0]) == {"key", "label", "percent", "severity", "resets_at", "scope"}
    assert body["breakdown"]["rows"][0] == {"key": "claude_code", "label": "Claude Code", "percent": 37}
    assert body["extra_usage"] == {"enabled": False, "spend_limit_reached": False}
    assert body["other"] == []
    assert body["fetched_at"] and body["next_refresh_at"]


def test_other_carries_opencode_windows(monkeypatch, real_payload):
    oc = {"available": True, "windows": [{"key": "opencode_rolling", "percent": 5.0}]}
    body = _client(monkeypatch, StubReader(real_payload), StubReader(oc)).get(
        "/v1/usage", headers=AUTH).json()
    assert body["other"] == [{"key": "opencode_rolling", "percent": 5.0}]


def test_stale_reason_is_the_slug_and_numbers_stay(monkeypatch, real_payload):
    stale = {**real_payload, "stale": True, "reason": "token expired", "reason_code": "token_expired"}
    body = _client(monkeypatch, StubReader(stale)).get("/v1/usage", headers=AUTH).json()
    assert body["stale"] is True and body["reason"] == "token_expired"
    assert len(body["windows"]) == 3


def test_unavailable_is_honest(monkeypatch):
    dead = {"available": False, "reason": "no keychain access", "reason_code": "no_login",
            "windows": [], "limits": []}
    body = _client(monkeypatch, StubReader(dead)).get("/v1/usage", headers=AUTH).json()
    assert body["available"] is False and body["reason"] == "no_login"
    assert body["plan"] is None and body["windows"] == [] and body["breakdown"] is None


def test_route_reuses_the_readers_no_second_poller(monkeypatch, real_payload):
    reader = StubReader(real_payload)
    c = _client(monkeypatch, reader)
    c.get("/v1/usage", headers=AUTH)
    c.get("/v1/usage", headers=AUTH)
    assert reader.polls == 2  # poll() is the reader's own 300 s cache, not ours


# ── the old board keeps working ─────────────────────────────────────────────

def test_old_board_plan_payload_is_unchanged(real_payload):
    merged = collector._merge_plan_usage(real_payload, {"available": False, "windows": []})
    assert [w["key"] for w in merged["windows"]] == ["session", "weekly"]
    assert merged["available"] is True and merged["stale"] is False
    assert merged["extra_usage_enabled"] is False
