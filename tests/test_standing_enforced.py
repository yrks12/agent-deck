"""Standing approvals at the real decision point: `app._approve` and
`app._permission`, the two doors every gated tool call goes through."""

import json

import pytest

from server import app as app_mod
from server import standing, standing_usage

SID = "cccccccc-0000-0000-0000-000000000001"
SEND = {"tool_name": "mcp__claude_ai_Gmail__send_message",
        "tool_input": {"to": "bob@acme.com", "body": "hi"},
        "session_id": SID, "cwd": "/w/atlas"}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(app_mod, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(app_mod, "STANDING_PATH", tmp_path / "standing.json")
    monkeypatch.setattr(app_mod, "STANDING_USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(app_mod, "STANDING_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(app_mod, "_state", {"sessions": [
        {"session_id": SID, "name": "atlas"}]})
    return tmp_path


def make(deck, **fields):
    return standing.create({"desk": "atlas", "kind": "send_email",
                            "limits": {"count_per_day": 1}, **fields},
                           by="owner", path=deck / "standing.json")


def test_no_policy_is_exactly_today(deck):
    assert app_mod._approve(dict(SEND))["decision"] == "abstain"
    assert app_mod._permission(dict(SEND))["behavior"] == "deny"


def test_the_pre_tool_door_allows_within_policy_then_asks_over_it(deck):
    made = make(deck)
    first = app_mod._approve(dict(SEND))
    assert (first["decision"], first["rule_id"]) == (
        "allow", f"standing:{made.id}")
    second = app_mod._approve(dict(SEND))
    assert second["decision"] == "ask"
    # Over the limit is a card on his board, exactly as an ask is today.
    assert json.loads((deck / "asks.json").read_text())
    [line] = standing_usage.audit(deck / "audit.jsonl")
    assert line["policy_id"] == made.id and line["desk"] == "atlas"


def test_the_prompt_door_allows_within_policy_and_denies_with_a_card_over(deck):
    make(deck)
    assert app_mod._permission(dict(SEND))["behavior"] == "allow"
    over = app_mod._permission(dict(SEND))
    assert over["behavior"] == "deny" and over["ask_id"]


def test_a_floor_action_is_carded_even_under_a_matching_policy(deck):
    make(deck, kind="call_api", tool="*")
    trash = {**SEND, "tool_name": "mcp__claude_ai_Gmail__trash_message",
             "tool_input": {"messageId": "1"}}
    assert app_mod._approve(dict(trash))["decision"] != "allow"
    assert app_mod._permission(dict(trash))["behavior"] == "deny"
    assert not (deck / "audit.jsonl").exists()


def test_a_broken_policy_store_never_breaks_the_hot_path(deck, monkeypatch):
    make(deck)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(standing_usage, "apply", boom)
    assert app_mod._approve(dict(SEND))["decision"] == "abstain"
