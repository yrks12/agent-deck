"""GET /v1/usage must exist on the REAL app, not only on a hand-built router.

Measured 2026-09-30: the live deck answered 404 because `usage_api` was never
mounted in `server/app.py`, so both apps hid the plan meter.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod

TOKEN = "deck-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PLAN = {
    "available": True, "stale": False, "reason_code": None,
    "subscription": {"name": "Max 20x"},
    "limits": [
        {"key": "session", "label": "5-hour session", "percent": 12.0,
         "severity": "ok", "resets_at": "2026-09-30T20:00:00Z", "scope": None},
        {"key": "weekly_all", "label": "Weekly", "percent": 40.0,
         "severity": "ok", "resets_at": "2026-10-04T08:00:00Z", "scope": None},
    ],
    "fetched_at": 1.0, "next_refresh_at": 2.0,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(app_mod.collector.usage, "poll", lambda: PLAN)
    monkeypatch.setattr(app_mod.collector.opencode_usage, "poll",
                        lambda: {"available": False, "windows": []})
    return TestClient(app_mod.app)


def test_usage_needs_auth(client):
    assert client.get("/v1/usage").status_code == 401


def test_usage_serves_plan_windows(client):
    r = client.get("/v1/usage", headers=AUTH)
    assert r.status_code == 200
    windows = {w["key"]: w for w in r.json()["windows"]}
    assert windows["session"]["percent"] == 12.0
    assert windows["weekly_all"]["resets_at"]
