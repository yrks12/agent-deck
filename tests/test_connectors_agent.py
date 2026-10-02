"""Desks can use the store: `store_search` and `store_install` deck tools.

Owner ruling 2026-08-27 (full autonomy): a TRUSTED item a desk asks for is
installed at once. An UNVERIFIED one is never installed on a desk's word: it
becomes a tap-to-approve decision card in the owner's thread, and only his tap
(`api.answer_decision` -> `connectors.on_decision`) installs it.
"""

from __future__ import annotations

import json

import pytest

from server import api as api_mod
from server import deck_mcp, decisions, office
from tests.test_connectors import MS_LEARN, VERCEL_APP, Rig, connectors


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    (tmp_path / "messages.jsonl").write_text("")
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setattr(connectors, "default_store", lambda: r.store)
    return r


def _call(desk: str, name: str, args: dict) -> tuple[dict, bool]:
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_the_deck_offers_store_tools_that_take_no_desk_of_their_own():
    listed = deck_mcp.handle("atlas", {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in listed}
    assert {"store_search", "store_install"} <= names
    install = next(t for t in listed if t["name"] == "store_install")
    # No secret ever travels through a desk's tool call (it would sit in the
    # transcript); keys are entered by the owner in the app.
    assert "secret" not in json.dumps(install["inputSchema"]).lower()


def test_a_desk_searches_the_trusted_catalog(rig):
    out, err = _call("atlas", "store_search", {"query": "microsoft learn"})
    assert not err
    assert [i["id"] for i in out["items"]] == [MS_LEARN]
    assert out["items"][0]["trust"] == "official"


def test_a_trusted_item_a_desk_asks_for_is_installed_on_it_at_once(rig):
    out, err = _call("globex", "store_install", {"id": MS_LEARN})
    assert not err and out["ok"] is True
    assert rig.item(MS_LEARN)["installed_on"] == ["globex"]
    assert [d for d, _ in rig.reloads] == ["globex"]
    assert not decisions.load(decisions.DEFAULT_PATH)


def test_an_unverified_item_becomes_an_approval_card_and_installs_nothing(rig):
    out, err = _call("globex", "store_install", {"id": VERCEL_APP})
    assert not err and out["ok"] is True
    assert out["pending_approval"] is True
    assert rig.item(VERCEL_APP)["installed_on"] == []
    assert rig.reloads == []
    (card,) = decisions.load(decisions.DEFAULT_PATH).values()
    assert card["desk"] == "globex" and card["state"] == "open"
    assert card["id"] == out["decision_id"]
    assert "unverified" in (card["prompt"] + card["help"]).lower()
    assert card["allow_custom"] is False
    labels = [o["label"] for o in card["options"]]
    assert labels[0].startswith("Install")


def test_his_tap_on_install_installs_it_and_a_no_does_not(rig):
    out, _ = _call("globex", "store_install", {"id": VERCEL_APP})
    card = decisions.find(out["decision_id"])
    yes = card["options"][0]["value"]
    settled = decisions.answer(card["id"], yes)
    assert connectors.on_decision(settled)["ok"] is True
    assert rig.item(VERCEL_APP)["installed_on"] == ["globex"]
    # A replayed answer does not install twice or raise.
    assert connectors.on_decision(settled).get("already") is True

    out, _ = _call("atlas", "store_install", {"id": VERCEL_APP})
    card = decisions.find(out["decision_id"])
    no = card["options"][1]["value"]
    connectors.on_decision(decisions.answer(card["id"], no))
    assert "atlas" not in rig.item(VERCEL_APP)["installed_on"]


def test_an_ordinary_decision_is_not_the_stores_business(rig):
    card = {"id": "dec_x", "desk": "atlas", "answer": "Approve copy as-is"}
    assert connectors.on_decision(card) is None


def test_the_deck_hands_every_answered_card_to_the_store(tmp_path, monkeypatch):
    """The wiring in `api.answer_decision`, through the real route."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.sources import comms as comms_mod
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    (tmp_path / "roster.json").write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "m"}]}))
    seen = []
    assert connectors is not None
    monkeypatch.setattr(connectors, "on_decision", lambda card: seen.append(card))
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=tmp_path / "roster.json",
        prefs_path=tmp_path / "prefs.json", decisions_path=tmp_path / "d.json",
        deliver=lambda name, text: False)
    monkeypatch.setenv(api_mod.TOKEN_ENV, "tok-not-real")
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    card = decisions.create("atlas", "Pick?", ["A", "B"], path=tmp_path / "d.json")
    r = TestClient(app).post(f"/v1/decisions/{card['id']}",
                             headers={"Authorization": "Bearer tok-not-real"},
                             json={"value": "A"})
    assert r.status_code == 200
    assert [c["id"] for c in seen] == [card["id"]]
