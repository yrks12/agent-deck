"""`mcp__deck__call_owner(reason, urgency)`: a desk rings him.

The tool is the desk's only way to start a ring, so it is where "never for a
status update" is said, and where a refusal comes back with what to do
instead. The desk's name comes from the spawn argv, as for every deck tool.
"""

import json
import time

import pytest

from server import deck_mcp, office, ringing
from test_deck_mcp import CHIEF, JUNIOR, PROBE, records, tool


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [CHIEF, JUNIOR, PROBE]}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    return tmp_path


def test_it_is_served_and_says_never_for_status_updates():
    [spec] = [t for t in deck_mcp.TOOLS if t["name"] == "call_owner"]
    assert spec["inputSchema"]["required"] == ["reason"]
    assert spec["inputSchema"]["properties"]["urgency"]["enum"] == ["normal", "urgent"]
    assert "never for a status update" in spec["description"].lower()


def test_off_by_default_the_reason_still_reaches_his_thread(bus):
    out, err = tool("atlas", "call_owner", reason="prod db is down",
                    urgency="urgent")
    assert err and out["reason"] == "calls_off" and out["posted_to_thread"]
    assert records(bus)[0]["text"] == "prod db is down"


def test_turned_on_the_chief_rings_him(bus, monkeypatch):
    ringing.update({"when": "urgent"})
    monkeypatch.setattr(ringing, "_quiet", lambda conf, at: False)
    out, err = tool("atlas", "call_owner", reason="prod db is down",
                    urgency="urgent")
    assert not err and out["state"] == "ringing"
    [live] = ringing.live(time.time())
    assert live["id"] == out["ring_id"] and live["agent"] == "atlas"


def test_a_junior_is_turned_away_to_its_boss(bus):
    ringing.update({"when": "anytime"})
    out, err = tool("scout", "call_owner", reason="tests are red")
    assert err and out["reason"] == "not_allowed"


def test_an_empty_reason_is_refused(bus):
    out, err = tool("atlas", "call_owner", reason="  ")
    assert err and out["reason"] == "empty_reason"
