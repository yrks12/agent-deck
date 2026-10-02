"""The deck's per-hire settings file, and the two holes left open in it.

The approval hook is installed and proven (`docs/prove-the-hook.md`). Two things
that slice named in its own report and did not close:

**1. `hooks/cc-compact.js` is registered nowhere.** The compaction fix has two
layers. Layer one -- the full brief in `--append-system-prompt` -- is live.
Layer two, the one that tells you when layer one stopped holding, is the hook
plus `server/compaction.py`, and neither had a caller. Worse, `POST /api/compact`
-- the endpoint the hook has always posted to -- did not exist, so registering
the hook alone would have moved the silence rather than ended it.

**2. `DECK_URL` was inherited, not stated.** A hired session's hook read
`DECK_URL` off the Terminal's environment and fell back to `127.0.0.1:7788`, so
a deck running on any other port hired desks that reported to a *different*
daemon -- or to nothing. The deck now names its own address in the file.

The event name is the trap here, and it is measured rather than read off the
hook's own comment: a hook registered on an event that never fires passes every
assertion you could write about the file's contents. Measured against Claude
Code 2.1.252 by registering both events and running `/compact` for real:

* `PreCompact` fires with `{trigger, custom_instructions}` -- and it fires even
  when the CLI then REFUSES to compact ("Not enough messages to compact"). A
  detection layer on `PreCompact` would therefore invent compactions that never
  happened.
* `PostCompact` fires only when a compaction actually occurred, with
  `{trigger, compact_summary}`. That is the event this registers.

Both dispatch sites are in the binary: `hook_event_name:"PostCompact"` and
`hook_event_name:"PreCompact"`. `test_postcompact_is_an_event_this_cli_emits`
pins that against the installed CLI so a future release cannot rename it
silently.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import approval

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def recording_server():
    """A stand-in daemon that records which paths it was called on.

    Local to this module on purpose: `conftest.approve_server` answers approvals
    and says nothing about *where* a request went, and "which address did the
    hook actually reach" is the whole question in half these tests.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    paths: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            paths.append(self.path)
            body = json.dumps({"decision": "ask", "rule_id": None,
                               "reason": "stub"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Silence: pytest output is not an access log."""

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.paths = paths
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    return tmp_path


def document(bus) -> dict:
    return json.loads(Path(approval.install().path).read_text())


def command_for(doc: dict, event: str) -> str:
    # The mirror (`hooks/cc-mirror.js`) rides on PreToolUse beside the
    # approver; it observes Bash and decides nothing, so it is not the one.
    entries = doc["hooks"][event]
    commands = [h["command"] for entry in entries for h in entry["hooks"]
                if "cc-mirror.js" not in h["command"]]
    assert len(commands) == 1, f"expected one {event} command, got {commands}"
    return commands[0]


# -- 1. the compaction hook is registered, on the event that really fires -----


def test_the_settings_file_registers_the_compaction_hook_on_postcompact(bus):
    command = command_for(document(bus), "PostCompact")
    assert "cc-compact.js" in command, \
        f"a hired desk's compaction is still invisible: {command}"


def test_the_compaction_hook_is_not_registered_on_precompact(bus):
    """PreCompact fires even when the CLI goes on to refuse the compaction --
    measured, with `/compact` on a session too short to compact. Registering
    there would file compaction records for compactions that never happened,
    which is worse than the silence it replaces."""
    assert "PreCompact" not in document(bus)["hooks"]


def test_postcompact_is_an_event_this_cli_emits(bus):
    """Anti-rot, read off the binary that will actually run the hook rather than
    off the hook's own comment. This is the assertion that would have caught a
    hook registered on an invented event -- every other test here would pass."""
    cli = Path.home() / ".local" / "share" / "claude" / "versions"
    versions = sorted(p for p in cli.glob("*") if p.is_file()) if cli.is_dir() else []
    if not versions:
        pytest.skip("no installed Claude Code binary to pin the event name against")
    found = subprocess.run(
        ["grep", "-c", "-a", 'hook_event_name:"PostCompact"', str(versions[-1])],
        capture_output=True, text=True, timeout=120)
    assert found.stdout.strip() not in ("", "0"), \
        f"{versions[-1].name} no longer dispatches PostCompact -- re-measure"


def test_the_compaction_hook_reaches_the_daemon(bus, recording_server):
    """The join, through the artefact: the command string read back out of the
    generated file, run the way a `"type": "command"` hook is run, must arrive
    at POST /api/compact."""
    command = command_for(document(bus), "PostCompact")
    payload = {"session_id": "hired-9", "cwd": "/p", "trigger": "auto",
               "hook_event_name": "PostCompact", "compact_summary": "..."}
    done = subprocess.run(command, shell=True, input=json.dumps(payload),
                          capture_output=True, text=True, timeout=20,
                          env={**os.environ, "DECK_URL": recording_server.url})
    assert done.returncode == 0, done.stderr
    assert done.stdout == "", "a compaction hook that prints breaks the session"
    assert recording_server.paths == ["/api/compact"], recording_server.paths


# -- the endpoint that has never existed -------------------------------------


def test_a_compaction_leaves_a_line_in_the_ledger(tmp_path, monkeypatch):
    """The good signal, end to end on the daemon: a `compact` event on the same
    bus every other event lands on, naming the session that forgot itself."""
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setattr(app_mod, "BUS_FILE", ledger)
    client = TestClient(app_mod.app)

    res = client.post("/api/compact", json={
        "session_id": "sid-compacted", "cwd": "/p", "trigger": "manual"})

    assert res.status_code == 200, res.text
    written = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    compacts = [w for w in written if w["event"] == "compact"]
    assert len(compacts) == 1, written
    assert compacts[0]["session_id"] == "sid-compacted"
    assert compacts[0]["trigger"] == "manual"


def test_a_junk_compaction_payload_is_recorded_not_raised(tmp_path, monkeypatch):
    """On the hot path of a session that is already mid-compaction. A traceback
    here is a 500 the hook swallows, and the record is lost silently -- the
    exact failure mode this whole layer exists to end."""
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setattr(app_mod, "BUS_FILE", ledger)
    client = TestClient(app_mod.app)

    res = client.post("/api/compact", json={"session_id": None, "trigger": 7})

    assert res.status_code == 200, res.text
    written = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert [w for w in written if w["event"] == "compact"]


# -- 2. the deck names its own address ---------------------------------------


def test_the_generated_file_pins_the_deck_that_hired_the_session(bus, monkeypatch):
    """Not a constant. A deck on 7791 must hire desks that report to 7791 --
    otherwise its agents' approvals and compactions land on somebody else's
    daemon, or on nothing at all."""
    monkeypatch.setenv("AGENT_DECK_PORT", "7791")
    assert approval.deck_url() == "http://127.0.0.1:7791"
    assert document(bus)["env"]["DECK_URL"] == "http://127.0.0.1:7791"


def test_the_address_is_read_off_the_running_daemons_own_argv(bus, monkeypatch):
    """`bin/cdash` honours AGENT_DECK_PORT, but the LaunchAgent that actually
    runs the deck sets no environment at all -- it puts `--port 7788` in
    ProgramArguments. So argv is the fallback that covers production."""
    monkeypatch.delenv("AGENT_DECK_PORT", raising=False)
    monkeypatch.delenv("DECK_URL", raising=False)
    monkeypatch.setattr(approval.sys, "argv",
                        ["uvicorn", "server.app:app", "--port", "7999"])
    assert approval.deck_url() == "http://127.0.0.1:7999"


def test_the_default_is_the_documented_port(bus, monkeypatch):
    monkeypatch.delenv("AGENT_DECK_PORT", raising=False)
    monkeypatch.delenv("DECK_URL", raising=False)
    monkeypatch.setattr(approval.sys, "argv", ["uvicorn", "server.app:app"])
    assert approval.deck_url() == "http://127.0.0.1:7788"


def test_the_pinned_address_is_the_one_the_hooks_actually_use(bus, monkeypatch,
                                                              recording_server):
    """The env block is only worth something if the hooks honour DECK_URL. Runs
    the approval command out of the generated file with exactly the environment
    Claude Code would apply from that file's own `env`, and requires the call to
    land on that address."""
    monkeypatch.setenv("DECK_URL", recording_server.url)
    doc = document(bus)
    assert doc["env"]["DECK_URL"] == recording_server.url

    subprocess.run(command_for(doc, "PreToolUse"), shell=True, timeout=20,
                   capture_output=True, text=True,
                   env={**os.environ, **doc["env"]},
                   input=json.dumps({"tool_name": "Bash", "cwd": "/p",
                                     "tool_input": {"command": "ls"},
                                     "session_id": "s"}))
    assert recording_server.paths == ["/api/approve"], recording_server.paths
