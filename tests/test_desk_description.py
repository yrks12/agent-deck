"""A desk says what it is for: `description`, one or two plain sentences.

MEASURED on the box, 2026-10-01: `GET /v1/agents` rows carried name, label,
account, preview -- and no sentence saying what the desk is for. The owner
asked why none of his agents has a description in the apps. The roster had a
`charter`, but that is the desk's whole brief, written to the desk in the
second person ("You own ..."), often a thousand characters long; it is not
something a header can show.

`description` is the owner-facing line. Who writes it:

* the hiring desk, on every `YOS_HIRE` -- it is a required field;
* the desk itself, with `set_my_look` (and in a `YOS_DESK` line);
* the chief of staff, for any desk, with `set_my_look name=...`;
* the owner, from the app, with `PATCH /v1/agents/{name}`.

Hermetic: tmp bus dir, hand-written roster, no daemon.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import deck_mcp, decisions, features, hire, office, onboard, roster
from server.harvest import Harvester
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHIEF = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
         "label": "COS", "charter": "Own it.", "reports_to": None}
JUNIOR = {"name": "scout", "cwd": "/tmp/p", "engine": "claude",
          "mission": "look", "label": "Scout", "charter": "Look.",
          "reports_to": "atlas"}
OTHER = {"name": "ledger", "cwd": "/tmp/p", "engine": "claude",
         "mission": "sell", "label": "Ledger", "charter": "Sell.",
         "reports_to": "atlas"}
SAID = "Runs the shop's Instagram: posts, replies and the nightly round."


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    (tmp_path / "messages.jsonl").write_text("")
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1,
                                "agents": [CHIEF, JUNIOR, OTHER]}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", path)
    return tmp_path


def make(bus, monkeypatch):
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=bus / "roster.json",
        prefs_path=bus / "prefs.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return surface, TestClient(app)


def desks(bus):
    return {d.name: d for d in roster.load_roster(bus / "roster.json")}


def tool(desk, which, **args):
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 7,
                                   "method": "tools/call",
                                   "params": {"name": which,
                                              "arguments": args}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def row(client, name):
    rows = client.get("/v1/agents", headers=AUTH).json()["agents"]
    return next(r for r in rows if r["name"] == name)


# ── the roster and the wire ─────────────────────────────────────────────────


def test_the_roster_keeps_a_description(bus):
    path = bus / "roster.json"
    roster.upsert(path, roster.Desk(**{**JUNIOR, "description": SAID}))
    assert desks(bus)["scout"].description == SAID


def test_every_row_and_the_detail_carry_description(bus, monkeypatch):
    """Always present, `""` when unset: a client reads a value, never has to
    know the key can be missing."""
    roster.upsert(bus / "roster.json",
                  roster.Desk(**{**JUNIOR, "description": SAID}))
    _, client = make(bus, monkeypatch)
    assert row(client, "scout")["description"] == SAID
    assert row(client, "atlas")["description"] == ""
    got = client.get("/v1/agents/scout", headers=AUTH).json()
    assert got["description"] == SAID


def test_the_owner_edits_it_from_the_app(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.patch("/v1/agents/scout", headers=AUTH,
                     json={"description": "  Finds leads.\n Reports daily. "})
    assert r.status_code == 200
    assert r.json()["description"] == "Finds leads. Reports daily."
    assert desks(bus)["scout"].description == "Finds leads. Reports daily."
    # The charter is the desk's brief, and the description is not it.
    assert desks(bus)["scout"].charter == "Look."
    assert row(client, "scout")["description"] == "Finds leads. Reports daily."


def test_the_owner_cannot_store_an_essay(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.patch("/v1/agents/scout", headers=AUTH,
                     json={"description": "x" * (roster.DESCRIPTION_MAX + 1)})
    assert r.status_code == 400 and r.json()["reason"] == "bad_description"
    assert desks(bus)["scout"].description == ""


def test_a_description_change_reaches_the_apps_live(bus, monkeypatch):
    """Written from another process (a desk's tool) -- pushed as the same-name
    `agent_renamed` a look change is, so the row is swapped in place."""
    surface, _ = make(bus, monkeypatch)
    surface.refresh()
    roster.upsert(bus / "roster.json",
                  roster.Desk(**{**JUNIOR, "description": SAID}))
    pushed = [e for e in surface.refresh() if e["type"] == "agent_renamed"]
    assert [e["agent"]["description"] for e in pushed] == [SAID]


def test_the_owner_can_state_one_when_he_hires(bus, monkeypatch, tmp_path):
    _, client = make(bus, monkeypatch)
    r = client.post("/v1/agents", headers=AUTH, json={
        "name": "inbox", "cwd": str(tmp_path), "engine": "claude",
        "label": "Email", "charter": "You own the inbox.",
        "description": "Triages the inbox and drafts replies."})
    assert r.status_code in (200, 201), r.text
    assert desks(bus)["inbox"].description == (
        "Triages the inbox and drafts replies.")


# ── the contract says so ────────────────────────────────────────────────────


def test_the_client_doc_describes_the_field_where_clients_look():
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs" /
           "client-api.md").read_text()
    fields = doc[doc.index("### Fields"):doc.index("### 3.0")]
    assert "| `description` |" in fields
    detail = doc[doc.index("## 4. `GET /v1/agents/{name}`"):
                 doc.index("## 5. `PATCH /v1/agents/{name}`")]
    assert '"description":' in detail and "**Description** → `description`" in detail
    patch = doc[doc.index("## 5. `PATCH /v1/agents/{name}`"):]
    assert "`bad_description`" in patch and "| `description` |" in patch
