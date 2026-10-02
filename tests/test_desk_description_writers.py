"""Who writes a desk's `description`: the hirer, the desk, the chief.

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


# ── a desk, and the chief, set it with set_my_look ──────────────────────────


def test_a_desk_sets_its_own_description(bus):
    out, err = tool("scout", "set_my_look", description=SAID)
    assert not err and out["ok"] and out["description"] == SAID
    assert desks(bus)["scout"].description == SAID


def test_the_chief_sets_any_desks_description(bus):
    out, err = tool("atlas", "set_my_look", name="ledger", description=SAID)
    assert not err, out
    assert desks(bus)["ledger"].description == SAID


def test_a_desk_cannot_describe_another(bus):
    out, err = tool("scout", "set_my_look", name="ledger", description=SAID)
    assert err and out["reason"] == "not_your_report"
    assert desks(bus)["ledger"].description == ""


def test_the_tool_refuses_an_essay(bus):
    out, err = tool("scout", "set_my_look",
                    description="x" * (roster.DESCRIPTION_MAX + 1))
    assert err and out["reason"] == "bad_look"
    assert desks(bus)["scout"].description == ""


def test_the_tool_schema_offers_description():
    spec = next(t for t in deck_mcp.TOOLS if t["name"] == "set_my_look")
    assert "description" in spec["inputSchema"]["properties"]


def test_the_feature_registry_tells_desks_they_can():
    (feature,) = [f for f in features.FEATURES if f.key == "set_my_look"]
    assert "description" in feature.line


# ── on hire, the hiring desk must write one ─────────────────────────────────


def _said(text):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": text}]}}) + "\n"


def _harvest(tmp_path, line, actor="atlas"):
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_said(line))
    harvester = Harvester(tmp_path / "roster.json", tmp_path / "offsets.json")
    return harvester.poll([{"session_id": "s1", "name": actor, "cwd": "/tmp",
                            "state": "WORKING",
                            "transcript": str(transcript)}])


def _hire(tmp_path, **extra):
    return "YOS_HIRE " + json.dumps({
        "name": "growth-scout", "label": "Growth",
        "charter": "You own paid acquisition. You never spend.",
        "cwd": str(tmp_path), **extra})


def test_a_hire_carries_its_description_onto_the_roster(tmp_path, bus):
    _harvest(tmp_path, _hire(tmp_path, description=SAID))
    assert desks(bus)["growth-scout"].description == SAID


def test_a_hire_without_a_description_is_refused_and_the_hirer_told(
        tmp_path, bus):
    records = _harvest(tmp_path, _hire(tmp_path))
    assert "growth-scout" not in desks(bus)
    (refused,) = [r for r in records if r["result"] == "refused"]
    assert "description" in refused["reason"]
    told = [json.loads(line) for line in
            (bus / "messages.jsonl").read_text().splitlines() if line.strip()]
    assert any(m["to"] == "atlas" and "description" in m["text"]
               for m in told), told


def test_the_hiring_brief_teaches_the_field():
    (line,) = [ln for ln in hire.HIRING.splitlines()
               if ln.startswith(onboard.HIRE_PREFIX)]
    _, hires = onboard.parse_lines(line)
    assert hires and hires[0].description


def test_a_desk_line_can_carry_its_description(bus):
    (patch,), _ = onboard.parse_lines(
        "YOS_DESK " + json.dumps({"description": SAID}))
    onboard.apply_patch(bus / "roster.json", "scout", patch)
    assert desks(bus)["scout"].description == SAID
