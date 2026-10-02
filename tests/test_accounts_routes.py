"""S8c: the accounts routes on the API. The move itself is
tests/test_accounts_move.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import accounts, mover
from server import api as api_mod

SID = "c91c8d85-4bad-4529-a026-2f2ab6956b43"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)
    return path


# -- the routes ---------------------------------------------------------------


@pytest.fixture
def client(reg, monkeypatch):
    from server import accounts_api
    monkeypatch.setenv(api_mod.TOKEN_ENV, "t")
    app = FastAPI()
    api_mod.register(app, surface=api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=Path("/nonexistent/r.json"), prefs_path=Path("/nonexistent/p.json")),
        background=False)

    class _Meters:
        def poll_all(self):
            return [(accounts.main_account(), {"subscription": {"name": "Max"}}),
                    (accounts.get("work"), {"subscription": None})]

    outcome: dict = {}

    class _Mover:
        def move(self, name, to):
            if "raise" in outcome:
                raise outcome["raise"]
            return {"ok": True, "moved": True, "desk": name, "from": "main", "to": to,
                    "session_id": SID}

    app.include_router(accounts_api.build_router(meters=_Meters(), mover=_Mover()))
    c = TestClient(app)
    c.outcome = outcome
    return c


AUTH = {"Authorization": "Bearer t"}


def test_get_accounts_is_an_object_with_a_list(client):
    body = client.get("/v1/accounts", headers=AUTH).json()
    assert body == {"accounts": [
        {"id": "main", "label": "Main", "kind": "subscription", "plan": {"name": "Max"}},
        {"id": "work", "label": "Work", "kind": "subscription", "plan": None}]}
    assert client.get("/v1/accounts").status_code == 401


@pytest.mark.parametrize("raised, status, reason", [
    (None, 200, None),
    (mover.MoveError(409, "not_idle", "busy"), 409, "not_idle"),
    (mover.MoveError(404, "no_account", "?"), 404, "no_account"),
    (mover.MoveError(502, "move_failed", "x"), 502, "move_failed"),
])
def test_post_move_maps_each_refusal(client, raised, status, reason):
    if raised:
        client.outcome["raise"] = raised
    r = client.post("/v1/agents/atlas/account", json={"account": "work"}, headers=AUTH)
    assert r.status_code == status
    if reason:
        assert r.json()["reason"] == reason
    else:
        assert r.json()["to"] == "work"


def test_the_routes_are_mounted_on_the_real_app(monkeypatch):
    """The measured way this breaks (usage, 2026-09-30): a router nobody
    mounted answers 404 on the live deck, and both apps hide the feature.
    401, not 404: mounted, and refused before it reads anything."""
    from server import app as app_mod
    monkeypatch.setenv(api_mod.TOKEN_ENV, "t")
    real = TestClient(app_mod.app)
    assert real.get("/v1/accounts").status_code == 401
    assert real.post("/v1/agents/atlas/account", json={"account": "w"}).status_code == 401


def test_post_move_needs_an_account(client):
    r = client.post("/v1/agents/atlas/account", json={}, headers=AUTH)
    assert r.status_code == 400 and r.json()["reason"] == "bad_request"
