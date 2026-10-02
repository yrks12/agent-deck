"""Detector 2: an MCP server asking a hired desk for input is answered by the
deck, never left as a dialog.

`Elicitation` is a confirmed, purpose-built member of the class
`hooks/cc-permission.js` already covers -- Claude Code's own description of it
is "Fired when an MCP server requests user input. Hooks can auto-respond
(accept/decline) instead of showing the dialog." The deck registered nothing
for it, so every elicitation drew a dialog on a desk with nobody at it.

MEASURED off the 2.1.252 binary's own dispatcher, not guessed, because getting
the key names wrong here fails SILENTLY -- the hook runs, prints something the
CLI ignores, and the dialog appears anyway.

The input the hook is handed::

    {...common, hook_event_name: "Elicitation", mcp_server_name, message,
     mode, url, elicitation_id, requested_schema}

The output the CLI parses::

    case "Elicitation":
      if (e.hookSpecificOutput.action) {
        M.elicitationResponse = {action: e.hookSpecificOutput.action,
                                 content: e.hookSpecificOutput.content};
        if (e.hookSpecificOutput.action === "decline")
          M.blockingError = {blockingError: e.reason || "Elicitation denied by hook", ...}
      }

So `action` and `content` sit DIRECTLY on `hookSpecificOutput`, and the
top-level `reason` is the sentence the agent is left holding. `elicitationResponse`
is the CLI's internal name and is not a key a hook writes.

And the fail-closed floor, from the same dispatcher::

    if (blockingError) return {action: "decline"};
    if (response) return {action: response.action, content: response.content};
    return;                       // <- undefined: the dialog IS shown

A hook that prints nothing, crashes, or prints a shape the CLI does not
recognise therefore falls through to the dialog. That is the stall. So the
signal this file asserts is the presence of a decline in every failure mode,
never the absence of an error.

One more measured detail that changes the registration: the Elicitation hook's
matcher is applied to `matchQuery: serverName` -- the MCP SERVER name, not a
tool name. There is no useful narrow matcher, so it is `*`.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import approval, asking

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-elicit.js"

ELICIT = {
    "hook_event_name": "Elicitation",
    "mcp_server_name": "docker-mcp",
    "message": "Which container should I restart?",
    "mode": "form",
    "elicitation_id": "el-1",
    "requested_schema": {"type": "object",
                         "properties": {"container": {"type": "string"}}},
    "session_id": "sid-hired",
    "cwd": "/Users/samcarter/Projects/acme",
    "agent": "Acme",
}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    rules = tmp_path / "autoreview.json"
    rules.write_text(json.dumps({"version": 1, "rules": []}))
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", rules)
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "events.jsonl")
    return tmp_path


@pytest.fixture
def client(deck):
    return TestClient(app_mod.app)


def elicit(client, **over):
    res = client.post("/api/elicitation", json={**ELICIT, **over})
    assert res.status_code == 200, res.text
    return res.json()


def ledger(deck):
    path = deck / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ── the endpoint ───────────────────────────────────────────────────────────


def test_an_elicitation_is_declined_and_the_owner_is_told_what_was_asked(client, deck):
    """The GOOD signal, both halves: the dialog is answered so nothing hangs,
    and the same question is on the deck for the owner to act on."""
    body = elicit(client)
    assert body["action"] == "decline"

    pending = asking.pending(deck / "asks.json")
    assert len(pending) == 1, pending
    ask = pending[0]
    assert "Which container should I restart?" in ask.subject
    assert "docker-mcp" in asking.compose(ask)
    assert ask.id in body["reason"], (
        "the agent must be able to name the outstanding row to its boss")


def test_the_endpoint_can_only_ever_say_decline(client):
    """`accept` would hand an MCP server a value nobody chose, and returning
    nothing at all is the dialog. Swept across malformed input."""
    for over in (
        {},
        {"message": None},
        {"mcp_server_name": ""},
        {"requested_schema": "not a schema"},
        {"message": 17, "mcp_server_name": None, "cwd": None},
    ):
        body = elicit(client, **over)
        assert body["action"] == "decline", body
        assert body.get("reason"), "a decline with no sentence explains nothing"


def test_a_crash_inside_the_endpoint_still_declines(client, monkeypatch):
    """Same inverted floor as `/api/permission`: a bug in here must not leave a
    dialog standing."""
    def boom(*_a, **_k):
        raise RuntimeError("recording blew up")

    monkeypatch.setattr(app_mod.ask_recorder, "on_verdict", boom)
    assert elicit(client)["action"] == "decline"


def test_the_elicitation_is_on_the_ledger(client, deck):
    elicit(client)
    rows = [r for r in ledger(deck) if r.get("event") == "elicitation"]
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "decline"
    assert rows[0]["mcp_server"] == "docker-mcp"
    assert rows[0]["session_id"] == "sid-hired"


def test_one_question_per_elicitation_not_one_per_retry(client, deck):
    for _ in range(5):
        elicit(client)
    assert len(asking.pending(deck / "asks.json")) == 1


# ── the hook ───────────────────────────────────────────────────────────────


def run_hook(payload, **env_extra):
    env = {**os.environ, **env_extra}
    return subprocess.run(["node", str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)


def output_of(proc):
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    block = out["hookSpecificOutput"]
    assert block["hookEventName"] == "Elicitation"
    return out


def test_the_hook_declines_when_the_deck_is_unreachable():
    """The fall-through here IS the dialog, so silence is the one thing the
    hook may never emit."""
    out = output_of(run_hook(ELICIT, DECK_URL="http://127.0.0.1:1"))
    assert out["hookSpecificOutput"]["action"] == "decline"
    assert out["reason"], (
        "`reason` is what the CLI turns into the agent's blockingError; "
        "without it the agent is told nothing")


def test_the_hook_never_accepts_on_a_failure():
    for payload, env in (
        (ELICIT, {"DECK_URL": "http://127.0.0.1:1"}),
        (ELICIT, {"DECK_URL": "not a url"}),
        ({}, {"DECK_URL": "http://127.0.0.1:1"}),
        ({"mcp_server_name": None}, {"DECK_URL": "http://127.0.0.1:1"}),
    ):
        out = output_of(run_hook(payload, **env))
        assert out["hookSpecificOutput"]["action"] == "decline"


def test_the_hook_puts_action_where_the_cli_reads_it():
    """Measured against the 2.1.252 dispatcher: `action` is read off
    `hookSpecificOutput` itself. Nesting it under `elicitationResponse` -- the
    CLI's internal name -- produces a hook that runs, prints, is ignored, and
    lets the dialog appear anyway."""
    out = output_of(run_hook(ELICIT, DECK_URL="http://127.0.0.1:1"))
    block = out["hookSpecificOutput"]
    assert "action" in block
    assert "elicitationResponse" not in block
    assert "decision" not in block, "that is PermissionRequest's vocabulary"


def test_the_hook_answers_inside_its_budget():
    start = time.monotonic()
    run_hook(ELICIT, DECK_URL="http://10.255.255.1:7788")
    assert time.monotonic() - start < 4


# ── the registration: without it the hook is decorative ────────────────────


def test_every_hired_desk_is_started_with_the_elicitation_hook():
    doc = approval.settings_document("/usr/bin/node")
    assert "Elicitation" in doc["hooks"], sorted(doc["hooks"])
    entry = doc["hooks"]["Elicitation"][0]
    assert "cc-elicit.js" in entry["hooks"][0]["command"]
    # Measured: this matcher is applied to the MCP server name, not a tool
    # name, so there is no narrow form of it that means anything.
    assert entry["matcher"] == "*"


def test_the_install_refuses_when_the_elicitation_hook_is_missing(tmp_path, monkeypatch):
    """A settings file naming a script that is not there registers a hook whose
    every invocation is a failed spawn -- and, here, a drawn dialog."""
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "s.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    result = approval.install(elicit_hook=tmp_path / "nope.js")
    assert result.ok is False
    assert result.reason == "no_hook"
