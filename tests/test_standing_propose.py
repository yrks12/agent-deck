"""A desk PROPOSES a standing approval; only the owner can make it live."""

import json

import pytest

from server import deck_mcp, standing
from tests.test_deck_mcp import tool

ROSTER = [{"name": "atlas", "cwd": "/tmp/p", "engine": "claude",
           "mission": "run", "reports_to": None},
          {"name": "scout", "cwd": "/tmp/p", "engine": "claude",
           "mission": "look", "reports_to": "atlas"}]
EMAIL = {"kind": "send_email", "limits": {"count_per_day": 80,
                                          "recipients": ["@acme.com"]},
         "note": "weekly updates to Acme"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": ROSTER}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    monkeypatch.setattr(standing, "DEFAULT_PATH", tmp_path / "standing.json")
    return tmp_path / "standing.json"


def test_the_chief_proposes_and_it_grants_nothing(store):
    out, error = tool("atlas", "propose_standing_approval", **EMAIL)
    assert not error and out["ok"] and out["status"] == "proposed"
    [policy] = standing.load(store)
    assert (policy.status, policy.created_by, policy.desk) == (
        "proposed", "atlas", "atlas")
    assert not policy.live()


def test_the_chief_may_propose_for_a_report_or_the_company(store):
    tool("atlas", "propose_standing_approval", for_desk="scout", **EMAIL)
    tool("atlas", "propose_standing_approval", for_desk="*", **EMAIL)
    assert [p.desk for p in standing.load(store)] == ["scout", "*"]


def test_a_report_proposes_only_for_itself(store):
    out, error = tool("scout", "propose_standing_approval", for_desk="atlas",
                      **EMAIL)
    assert error and out["reason"] == "not_your_desk"
    out, error = tool("scout", "propose_standing_approval", **EMAIL)
    assert not error and standing.load(store)[0].desk == "scout"


def test_a_proposal_cannot_smuggle_an_active_status(store):
    tool("atlas", "propose_standing_approval", status="active", **EMAIL)
    assert standing.load(store)[0].status == "proposed"


@pytest.mark.parametrize("bad,reason", [
    ({"kind": "run_command", "pattern": "rm -rf *",
      "limits": {"count_per_day": 3}}, "never_coverable"),
    ({"kind": "spend_money", "tool": "mcp__stripe__create_transfer",
      "limits": {"usd_per_day": 50}}, "never_coverable"),
    ({"kind": "send_email"}, "no_limit"),
])
def test_what_could_never_be_approved_is_refused_at_the_proposal(store, bad,
                                                                 reason):
    out, error = tool("atlas", "propose_standing_approval", **bad)
    assert error and out["reason"] == reason
    assert standing.load(store) == []


def test_the_tool_is_served_and_the_brief_names_it():
    from server import features
    names = {t["name"] for t in deck_mcp.TOOLS}
    assert "propose_standing_approval" in names
    assert "mcp__deck__propose_standing_approval" in features.section()
