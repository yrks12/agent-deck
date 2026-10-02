"""The Money Board reaches the apps (GET /v1/money) and the desks (log_money,
my_money)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod, app as app_mod, deck_mcp, features
from server.money import ledger, service
from tests.test_money_service import _refresh

TOKEN = "deck-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(service, "BOARD_PATH", tmp_path / "board.json")
    return TestClient(app_mod.app)


def test_money_route_is_mounted_and_needs_auth(client):
    assert client.get("/v1/money").status_code == 401


def test_money_route_says_warming_then_serves_the_cached_board(client, tmp_path):
    assert client.get("/v1/money", headers=AUTH).json()["state"] == "warming"
    (tmp_path / "board.json").write_text(json.dumps({"currency": "GBP", "companies": []}))
    got = client.get("/v1/money", headers=AUTH).json()
    assert got["state"] == "ready" and got["currency"] == "GBP"


def test_desks_log_money_and_read_their_own_company(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(service, "BOARD_PATH", tmp_path / "board.json")
    got = deck_mcp.call("globex-growth", "log_money", {
        "kind": "cost", "amount": 5, "currency": "usd", "what": "Kie credits",
        "category": "provider"})
    assert got["ok"] and ledger.read(tmp_path / "ledger.jsonl")[0]["desk"] == "globex-growth"
    with pytest.raises(deck_mcp.ToolError) as bad:
        deck_mcp.call("globex-growth", "log_money", {"kind": "cost", "amount": -5,
                                                   "currency": "usd", "what": "x"})
    assert bad.value.reason == "bad_amount"
    with pytest.raises(deck_mcp.ToolError):
        deck_mcp.call("globex-growth", "my_money", {})        # no board yet
    _refresh(tmp_path, [])
    monkeypatch.setattr(service, "BOARD_PATH", tmp_path / "board.json")
    mine = deck_mcp.call("globex-growth", "my_money", {})
    assert [c["name"] for c in mine["companies"]] == ["Globex"]
    assert [e["desk"] for e in mine["experiments"]] == ["globex-growth"]
    chief = deck_mcp.call("atlas", "my_money", {})
    assert {c["name"] for c in chief["companies"]} == {"HQ", "Globex", "Acme"}


def test_desks_are_told_about_the_money_board():
    text = features.section()
    assert "mcp__deck__log_money" in text and "mcp__deck__my_money" in text
