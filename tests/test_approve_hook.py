import json, subprocess, sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-approve.js"


def run_hook(payload, *, env_extra):
    import os
    env = {**os.environ, **env_extra}
    p = subprocess.run(["node", str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=10)
    return p


def test_hook_emits_allow_when_the_daemon_allows(approve_server):
    """approve_server is a fixture serving POST /api/approve -> {"decision":"allow","rule_id":"a"}.
    Asserts the GOOD signal end to end: a real allow reaches Claude Code in the
    exact shape the hook protocol requires."""
    p = run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"},
                  "session_id": "s1", "cwd": "/x"},
                 env_extra={"DECK_URL": approve_server.url})
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_asks_when_the_daemon_is_unreachable():
    """Fail-safe direction. If the deck is down the hook must ASK, never allow.
    A hook that crashed would also produce no allow -- so this asserts the
    positive 'ask' string, not merely the absence of 'allow'."""
    p = run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
                  "session_id": "s1", "cwd": "/x"},
                 env_extra={"DECK_URL": "http://127.0.0.1:1"})
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_hook_never_takes_longer_than_its_budget():
    """A hook on the hot path of every tool call. A hung daemon must not hang
    the session."""
    import time
    start = time.monotonic()
    run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"},
              "session_id": "s", "cwd": "/x"},
             env_extra={"DECK_URL": "http://10.255.255.1:7788"})
    assert time.monotonic() - start < 4
