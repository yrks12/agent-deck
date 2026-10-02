"""The detector: a hired desk that meets an unknown action ends up with the
question in front of the owner, never stalled at a TTY.

MEASURED, Claude Code 2.1.252, interactive session in a real pty
(`--settings` hook, `Bash: echo PROBEMARKER`, no rule covering it):

* `PreToolUse` -> `ask` draws the modal and the session sits on it forever::

      │Hook PreToolUse:Bash requires confirmation for this command:
      │PROBE-PRETOOL-ASK [settings]
       Do you want to proceed?
       ❯ 1. Yes
         2. No

  Nothing outside the process can answer that. The deck's own authed socket
  injection reported ``{'ok': True, 'bytes': 145, 'transport': 'cc-sock',
  'authed': True}`` and the text landed in the *composer underneath the modal*
  -- delivered, and useless. Three speculative control frames did nothing
  either. So `delivered: true` is a lie in this state.
* `--permission-mode dontAsk` does NOT help: the status bar read "don't ask on"
  and the identical modal was drawn. It only covers rule/mode-origin asks.
* A `PermissionRequest` hook DOES fire on the interactive path, immediately
  before the prompt resolves, and its vocabulary is only
  ``{"behavior": "allow"}`` / ``{"behavior": "deny", "message": ...}`` -- there
  is no "ask", so it structurally cannot fall through to a modal. Returning
  deny dismissed the prompt, the transcript recorded ``Denied by
  PermissionRequest hook``, the model read the message and the turn completed.
  Six consecutive denials in one session: still no dialog, still no stall.

So the good signal this file asserts is not "no error". It is: the call is
refused in a sentence the agent can read and repeat, AND the same question is
sitting on the deck as a pending ask the owner can answer through the API.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import approval, asking, autoreview

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-permission.js"

UNKNOWN = {
    "tool_name": "Bash",
    "tool_input": {"command": "npx some-brand-new-thing --init"},
    "session_id": "sid-hired",
    "cwd": "/Users/samcarter/Projects/deck",
    "agent": "Acme",
}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    """A daemon whose rules, asks and ledger all live in tmp_path."""
    rules = tmp_path / "autoreview.json"
    rules.write_text(json.dumps({"version": 1, "rules": []}))
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", rules)
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "events.jsonl")
    return tmp_path


@pytest.fixture
def client(deck):
    return TestClient(app_mod.app)


def permission(client, **over):
    payload = {**UNKNOWN, **over}
    res = client.post("/api/permission", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def ledger(deck):
    path = deck / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ── the endpoint: the only two words it is allowed to say ──────────────────


def test_an_unknown_action_is_refused_in_a_sentence_and_never_asked(client, deck):
    """The GOOD signal, both halves in one assertion block: the agent gets a
    denial it can read, and the owner gets a pending ask he can answer."""
    body = permission(client)

    assert body["behavior"] == "deny"
    pending = asking.pending(deck / "asks.json")
    assert len(pending) == 1, pending
    ask = pending[0]
    assert ask.subject == "npx some-brand-new-thing --init"
    assert ask.agent == "Acme"
    # The denial has to name the row, or the agent cannot tell its boss which
    # question is outstanding and the owner's answer arrives against nothing.
    assert ask.id in body["message"]
    assert body["ask_id"] == ask.id


def test_the_endpoint_has_no_ask_in_its_vocabulary(client):
    """`ask` is the word that draws the modal. It must not be reachable from
    here for any input -- an unknown tool, a non-dict input, junk."""
    for over in (
        {},
        {"tool_name": "SomeToolShippedNextYear"},
        {"tool_input": "not a dict"},
        {"tool_name": "", "tool_input": {}, "cwd": "", "session_id": ""},
    ):
        body = permission(client, **over)
        assert body["behavior"] in ("allow", "deny"), body
        assert "ask" != body["behavior"]


def test_a_rule_written_since_the_pretooluse_call_allows_it_here(client, deck):
    """The loop closes without the agent retrying blind: the owner answers on
    the deck, and the very next permission request is allowed."""
    (deck / "autoreview.json").write_text(json.dumps({"version": 1, "rules": [{
        "id": "r-npx", "kind": "always_allow", "tool": "Bash",
        "pattern": "npx some-brand-new-thing*", "cwd": "**"}]}))
    body = permission(client)
    assert body["behavior"] == "allow"
    assert body.get("rule_id") == "r-npx"


def test_the_secure_handoff_floor_still_denies_rather_than_allowing(client, deck):
    """A credential or an irreversible command may never be allowed here, and
    a floor that answered "ask" would be the stall all over again."""
    body = permission(client, tool_input={"command": "cat ~/.env"})
    assert body["behavior"] == "deny"
    assert asking.pending(deck / "asks.json"), "the owner must still be asked"


def test_the_owner_answering_always_turns_the_denial_into_an_allow(client, deck):
    """End to end, through the owner's own answer path -- the whole claim."""
    first = permission(client)
    assert first["behavior"] == "deny"

    asks = deck / "asks.json"
    rule = asking.rule_from(asking.find(asks, first["ask_id"]), "always")
    autoreview.save_rules(deck / "autoreview.json", [rule])

    assert permission(client)["behavior"] == "allow"


def test_the_ledger_says_whether_pretooluse_had_already_reached_the_deck(client, deck):
    """Why a call fell through is a question this project has had to answer by
    reasoning. `preask` makes it a reading: True means the deck decided "no
    rule", False means `cc-approve.js` never got here (timeout, deck down)."""
    permission(client)  # nothing recorded the ask first
    cold = [r for r in ledger(deck) if r.get("event") == "permission_request"]
    assert len(cold) == 1 and cold[0]["preask"] is False

    # Now play it in order: PreToolUse first, then the prompt-time hook.
    client.post("/api/approve", json=UNKNOWN)
    permission(client, tool_input={"command": "npx some-brand-new-thing --init"})
    warm = [r for r in ledger(deck) if r.get("event") == "permission_request"]
    assert warm[-1]["preask"] is True
    assert warm[-1]["behavior"] == "deny"


def test_one_question_per_call_not_one_per_retry(client, deck):
    """An agent that retries must not fill the board. Same flood guard the
    PreToolUse path already honours."""
    for _ in range(5):
        permission(client)
    assert len(asking.pending(deck / "asks.json")) == 1


# ── the hook: its floor is inverted from cc-approve.js, on purpose ─────────


def run_hook(payload, **env_extra):
    env = {**os.environ, **env_extra}
    return subprocess.run(["node", str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)


def decision_of(proc):
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    block = out["hookSpecificOutput"]
    assert block["hookEventName"] == "PermissionRequest"
    return block["decision"]


def test_the_hook_denies_when_the_deck_is_unreachable(deck):
    """`cc-approve.js` fails to "ask" because there the fall-through is the
    prompt the human would have got anyway. HERE the fall-through IS the
    unanswerable modal, so the only safe floor is deny -- which never widens
    permission and which the agent can read and route around."""
    decision = decision_of(run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
         "session_id": "s1", "cwd": "/x"},
        DECK_URL="http://127.0.0.1:1"))
    assert decision["behavior"] == "deny"
    assert decision["message"], "a denial with no sentence is a modal with no dialog"


def test_the_hook_never_emits_allow_on_a_failure(deck):
    """Swept across every failure the hook can meet."""
    cases = [
        ({"tool_name": "Bash"}, {"DECK_URL": "http://127.0.0.1:1"}),
        ({"tool_name": "Bash"}, {"DECK_URL": "not a url"}),
        ({}, {"DECK_URL": "http://127.0.0.1:1"}),
    ]
    for payload, env in cases:
        assert decision_of(run_hook(payload, **env))["behavior"] == "deny"


def test_the_hook_answers_inside_its_budget(deck):
    """It runs while a prompt is half-drawn. A hung daemon must not become the
    stall this whole file exists to remove."""
    import time
    start = time.monotonic()
    run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"},
              "session_id": "s", "cwd": "/x"},
             DECK_URL="http://10.255.255.1:7788")
    assert time.monotonic() - start < 4


# ── the install: without this the hook is decorative ───────────────────────


def test_every_hired_desk_is_started_with_the_prompt_time_hook():
    """The measured fault class this project keeps hitting is a correct piece
    of code registered nowhere. `PreToolUse` alone cannot prevent the modal --
    returning `ask` is what draws it."""
    doc = approval.settings_document("/usr/bin/node")
    hooks = doc["hooks"]
    assert "PermissionRequest" in hooks, sorted(hooks)
    command = hooks["PermissionRequest"][0]["hooks"][0]["command"]
    assert "cc-permission.js" in command
    # It used to be pinned to `approval.MATCHER`, the PreToolUse one. That
    # equality encoded an assumption nobody had measured, and the assumption
    # was wrong: PermissionRequest also fires for MCP tools and for
    # AskUserQuestion, so the narrow matcher left a hired desk on a modal for
    # exactly those. See tests/test_permission_matcher.py for the pty evidence.
    assert hooks["PermissionRequest"][0]["matcher"] == approval.PERMISSION_MATCHER
    assert approval.PERMISSION_MATCHER != approval.MATCHER


def test_the_install_refuses_when_the_prompt_time_hook_is_missing(tmp_path, monkeypatch):
    """A settings file naming a script that is not there registers a hook whose
    every invocation is a failed spawn -- and, here, a drawn prompt."""
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "s.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    result = approval.install(permission_hook=tmp_path / "nope.js")
    assert result.ok is False
    assert result.reason == "no_hook"
    assert not (tmp_path / "s.json").exists()
