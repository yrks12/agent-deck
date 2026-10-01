"""The same promise, through the two real hooks a desk runs.

`tests/test_always_means_always.py` drives the daemon's functions. This drives
what Claude Code actually executes: `node hooks/cc-approve.js` (PreToolUse) and
`node hooks/cc-permission.js` (PermissionRequest), over real HTTP, into the
real `app._approve` / `app._permission`. The hooks send only a session id, so
this is where a rule keyed on anything but the desk's NAME would fall over --
the wake below gives the desk a session id the board has never seen.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from server import api as api_mod
from server import app as app_mod
from server import asking, office
from server.sources import comms as comms_mod

ROOT = Path(__file__).resolve().parent.parent
APPROVE = ROOT / "hooks" / "cc-approve.js"
PERMISSION = ROOT / "hooks" / "cc-permission.js"

PALM_SID = "aaaaaaaa-0000-0000-0000-000000000001"
WOKEN_SID = "aaaaaaaa-0000-0000-0000-000000000002"
ATLAS_SID = "bbbbbbbb-0000-0000-0000-000000000003"


@pytest.fixture
def deck(tmp_path, monkeypatch):
    palm_cwd, atlas_cwd = str(tmp_path / "acme"), str(tmp_path / "atlas")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": atlas_cwd, "engine": "claude",
         "mission": "m", "reports_to": None},
        {"name": "acme", "cwd": palm_cwd, "engine": "claude",
         "mission": "m", "reports_to": "atlas"},
    ]}))
    snapshot = {"generated_at": 1.0, "sessions": [
        {"session_id": PALM_SID, "pid": 1, "name": "acme", "cwd": palm_cwd,
         "project": "acme", "state": "WORKING", "state_since": 1.0,
         "attention": None},
        {"session_id": ATLAS_SID, "pid": 2, "name": "atlas", "cwd": atlas_cwd,
         "project": "atlas", "state": "WORKING", "state_since": 1.0,
         "attention": None},
    ]}
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    token = tmp_path / "deck-token.txt"
    token.write_text("t")

    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    (tmp_path / "messages.jsonl").write_text("")
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", tmp_path / "autoreview.json")
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(app_mod, "ROSTER_PATH", roster)
    monkeypatch.setattr(app_mod, "SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(app_mod, "_state", snapshot)

    surface = api_mod.Surface(
        snapshot=lambda: snapshot, comms=comms_mod.CommsIndex(),
        roster_path=roster, prefs_path=tmp_path / "prefs.json",
        asks_path=tmp_path / "asks.json",
        rules_path=tmp_path / "autoreview.json")
    surface.refresh()

    routes = {"/api/approve": app_mod._approve,
              "/api/permission": app_mod._permission}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps(routes[self.path](json.loads(raw))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env = {**os.environ,
           "DECK_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
           "DECK_TOKEN_FILE": str(token)}
    try:
        yield {"surface": surface, "env": env, "acme": palm_cwd,
               "asks": tmp_path / "asks.json", "sessions": sessions_dir}
    finally:
        httpd.shutdown()


def hook(script: Path, env: dict, *, command: str, sid: str, cwd: str) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}, "session_id": sid,
               "cwd": cwd}
    done = subprocess.run(["node", str(script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]


def pre(deck, command, sid=PALM_SID) -> str | None:
    out = hook(APPROVE, deck["env"], command=command, sid=sid, cwd=deck["acme"])
    return out.get("permissionDecision")


def prompt(deck, command, sid=PALM_SID) -> str:
    out = hook(PERMISSION, deck["env"], command=command, sid=sid,
               cwd=deck["acme"])
    return out["decision"]["behavior"]


FLOOR = ("git checkout -b acmeco-domain -q; cat CLAUDE.md | head -60; "
         "grep -rn -e canonical -e success_url . | head -60")  # ask mxyw6


def test_always_through_the_real_hooks_survives_a_wake(deck):
    assert pre(deck, FLOOR) == "ask"               # the floor: `*checkout*`
    assert prompt(deck, FLOOR) == "deny"           # ...and the prompt door
    [ask] = asking.pending(deck["asks"])
    assert ask.agent == "acme", "the question is filed under the desk's name"

    deck["surface"].answer_ask(ask.id, "always")

    nxt = "git checkout main -q; cat CLAUDE.md | head -5; grep -n x ."
    assert pre(deck, nxt) == "allow"
    assert prompt(deck, nxt) == "allow"

    # Wake: new session id, only the session's own file names it so far.
    (deck["sessions"] / "7.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": WOKEN_SID, "name": "acme"}))
    assert pre(deck, nxt, sid=WOKEN_SID) == "allow"
    assert prompt(deck, nxt, sid=WOKEN_SID) == "allow"

    # Another desk, the same line: still his to answer.
    assert pre(deck, nxt, sid=ATLAS_SID) == "ask"
    # A verb he never saw on the card: still his to answer.
    assert pre(deck, "git checkout main; rm -rf .") == "ask"
