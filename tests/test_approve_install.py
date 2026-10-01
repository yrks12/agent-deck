"""The approval hook has to be *registered*, not merely correct.

`hooks/cc-approve.js`, `server/autoreview.py`, `POST /api/approve`, the ask
ledger and the approval cards were all built, all tested, and all reachable by
nothing: the hook appears in no settings scope on this machine, so no real
session ever called any of it. `docs/autoreview-settings.md` said so out loud --
"this is documentation, not an installed change" -- and it stayed that way.

The install has one shape and one anti-shape:

* every session the deck spawns runs under the hook, via `--settings` on the
  argv `server/spawn.py` builds;
* the owner's own terminals do not. Nothing here writes `~/.claude/settings.json`
  and nothing here is machine-wide. A hired desk is governed because the deck
  hired it, not because the machine was reconfigured.

Every assertion below is for the presence of the GOOD signal -- the flag is on
the argv, the file names the hook on `PreToolUse`, the registered command really
does reach `/api/approve` -- because "no error" is exactly what this defect
looked like for its whole life.

Hermetic: a tmp bus dir, a stub daemon on port 0, no spawn, no window.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from server import approval, spawn
from server.roster import Desk

REPO = Path(__file__).resolve().parent.parent
ACME = Desk(name="acme-growth", cwd="/p", engine="claude", mission="ads",
            model="claude-opus-5")


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """The deck's own directory, relocated. The real one holds the live
    `events.jsonl` every session on this machine writes to."""
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    return tmp_path


def registered_command(document: dict) -> str:
    """The one shell command Claude Code would run for a `PreToolUse` tool call,
    read back out of the document the deck generated."""
    entries = document["hooks"]["PreToolUse"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert len(commands) == 1, f"expected exactly one hook command, got {commands}"
    return commands[0]


# ── the argv ───────────────────────────────────────────────────────────────


def test_a_hired_claude_desk_carries_the_decks_settings_file():
    """The whole defect in one line. `--append-system-prompt` was already on
    this argv; `--settings` is what puts the desk under the approver."""
    argv = spawn.build_argv(ACME, background=False, settings="/deck/s.json")
    assert "--settings" in argv, f"a hired desk is not under the approver: {argv}"
    assert argv[argv.index("--settings") + 1] == "/deck/s.json"


def test_the_seed_is_still_the_last_positional_after_the_flag():
    """`--settings` takes a value. Inserted in the wrong place it would eat the
    seed, and the desk would be asked nothing -- the exact class of fault that
    made `opencode <seed>` open a directory."""
    argv = spawn.build_argv(ACME, background=False, seed="what is this desk for?",
                            settings="/deck/s.json")
    assert argv[-1] == "what is this desk for?"
    assert argv[argv.index("--settings") + 1] == "/deck/s.json"


@pytest.mark.parametrize("engine", ["opencode", "codex"])
def test_only_claude_gets_the_flag(engine):
    """`--settings` is Claude Code's. Handing it to the other two would be a
    broken command line, not an unapproved session."""
    desk = Desk(name="n", cwd="/p", engine=engine, mission="m")
    assert "--settings" not in spawn.build_argv(desk, background=False,
                                                settings="/deck/s.json")


def test_the_terminal_window_carries_the_flag_quoted():
    """A Terminal desk goes through AppleScript, so the flag has to survive two
    layers of quoting to reach the CLI."""
    script = spawn.build_applescript(ACME, settings="/Users/x/My Deck/s.json")
    assert "--settings" in script
    assert "My Deck" in script
    assert script.count('"') % 2 == 0


# ── the file the flag points at ────────────────────────────────────────────


def test_the_settings_file_registers_the_approval_hook_on_pretooluse(bus):
    result = approval.install()
    assert result.ok, f"{result.reason}: {result.detail}"

    document = json.loads(Path(result.path).read_text())
    command = registered_command(document)
    assert "cc-approve.js" in command, \
        f"the deck's settings file registers something else: {command}"
    assert "PreToolUse" in document["hooks"]


def test_the_registered_command_names_a_hook_that_exists_in_this_checkout(bus):
    """The daemon vouches for the hook it ships with. A path baked in from some
    other checkout is how a settings file rots into a no-op."""
    command = registered_command(json.loads(
        Path(approval.install().path).read_text()))
    hook = [part for part in command.split('"') if part.endswith("cc-approve.js")]
    assert hook, f"no quoted hook path in {command}"
    assert Path(hook[0]).is_file()
    assert Path(hook[0]) == REPO / "hooks" / "cc-approve.js"


def test_the_node_binary_is_resolved_and_not_a_literal(bus, tmp_path, monkeypatch):
    """This machine runs node from `~/.nvm/versions/node/v22.17.0/bin/node` and
    the four hooks already in `~/.claude/settings.json` all hard-code it -- so
    the next `nvm install` breaks them. The deck resolves instead, and an
    override wins, so a moved node is one env var and not an edit."""
    fake = tmp_path / "node"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("DECK_NODE", str(fake))

    assert approval.resolve_node() == str(fake)
    assert str(fake) in registered_command(json.loads(
        Path(approval.install().path).read_text()))

    # Prose may name the version this machine happens to run; a string constant
    # the resolver could hand out must not. Docstrings are excluded, so the
    # module can explain the pin it is removing without tripping its own test.
    import ast
    tree = ast.parse(Path(approval.__file__).read_text())
    prose = {id(n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in prose]
    pinned = [s for s in literals if re.search(r"/v\d+\.\d+\.\d+/", s)]
    assert pinned == [], \
        f"a pinned node version in the resolver is the bug this test exists for: {pinned}"


def test_the_resolver_finds_the_node_that_is_actually_on_this_machine(bus):
    """Not a mock. Whatever it picks must be a runnable node here."""
    node = approval.resolve_node()
    assert node, "no node resolved; a hired session would get no approver at all"
    done = subprocess.run([node, "-e", "process.stdout.write('ok')"],
                          capture_output=True, text=True, timeout=20)
    assert done.stdout == "ok", done.stderr


def test_the_deck_never_writes_the_owners_own_settings(bus):
    """The design, pinned. A machine-wide install would put every one of the
    owner's working sessions behind the deck's rule engine and make every tool
    call in them wait on the daemon. The deck governs what it hired."""
    written = Path(approval.install().path).resolve()
    assert written.parent == Path(bus).resolve()
    for owned_by_the_owner in ("settings.json", "settings.local.json"):
        assert written != (Path.home() / ".claude" / owned_by_the_owner)


# ── the registered command, run the way Claude Code runs it ────────────────


def test_a_hired_sessions_tool_call_reaches_the_approve_endpoint(bus, approve_server):
    """End to end through the artefact: take the command string out of the
    generated settings file, run it through a shell exactly as a `"type":
    "command"` hook is run, feed it a PreToolUse payload, and require that the
    deck's own answer comes back. This is the join the whole feature was missing
    -- correctness of the hook proved nothing while nothing invoked it."""
    command = registered_command(json.loads(
        Path(approval.install().path).read_text()))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"},
               "session_id": "hired-1", "cwd": "/p"}

    done = subprocess.run(command, shell=True, input=json.dumps(payload),
                          capture_output=True, text=True, timeout=20,
                          env={**os.environ, "DECK_URL": approve_server.url})

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"


def test_the_registered_command_still_asks_when_the_deck_is_down(bus):
    """Unchanged floor, re-pinned on the path it now really runs on. Every
    failure must print `ask`: the owner gets the prompt he would have got
    anyway. Allowing on an outage would silently auto-approve the machine."""
    command = registered_command(json.loads(
        Path(approval.install().path).read_text()))
    started = time.monotonic()
    done = subprocess.run(command, shell=True, timeout=20, capture_output=True,
                          text=True, env={**os.environ,
                                          "DECK_URL": "http://127.0.0.1:1"},
                          input=json.dumps({"tool_name": "Bash",
                                            "tool_input": {"command": "rm -rf /"},
                                            "session_id": "s", "cwd": "/p"}))
    assert done.returncode == 0
    decision = json.loads(done.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert time.monotonic() - started < 4, "the 2s guard no longer holds"


# ── the wiring: the door that actually opens a window ──────────────────────


#: Both channels a hire can be started through, and what each one's own binary
#: prints on stdout. `spawn_background` refuses `no_agent_id` unless it can read
#: an id, so one stand-in for both would be testing the double.
#:
#: PARAMETRIZED, not pinned to the Mac. Being governed by the approval hook is
#: not a property of Terminal.app -- and the box is the install where the
#: headless channel is the one EVERY hire takes, so a pin here would have left
#: the only machine that actually hires with no test that its hires are
#: governed at all. A headless session has no window in which a permission
#: prompt could ever appear, which makes an ungoverned one harder to notice
#: there, not easier.
CHANNELS = [
    (spawn.Channel("terminal", "gui_session", "a logged-in Mac"), "tab 3"),
    (spawn.Channel("background", "no_osascript", "no Terminal.app"),
     "backgrounded · 0b697cee · acme-growth\n"),
]
CHANNEL_IDS = [channel.name for channel, _ in CHANNELS]


def _recording_run(commands: list[str], stdout: str):
    """A `subprocess.run` double that records the WHOLE command line.

    Joined rather than indexed, so one assertion reads both channels: on the
    terminal channel argv is `["osascript", "-e", <script>]` and the desk's
    command line is inside that script; on the headless channel argv IS the
    desk's command line.
    """
    class _Done:
        returncode, stderr = 0, ""

    _Done.stdout = stdout

    def fake_run(argv, **kwargs):
        commands.append(" ".join(str(arg) for arg in argv))
        return _Done()

    return fake_run


@pytest.mark.parametrize("channel,stdout", CHANNELS, ids=CHANNEL_IDS)
def test_spawn_installs_the_file_and_hands_it_to_the_session(
    channel, stdout, bus, monkeypatch, osascript_is_on_path
):
    """`server/approval.py` correct in isolation is worth exactly what
    `hooks/cc-approve.js` was worth. This pins the join to the path
    `POST /v1/agents/interview` runs: the file exists on disk when the session
    starts, and the argv the session is started with points at it."""
    commands: list[str] = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        _recording_run(commands, stdout))
    spawn.start(ACME, roster_path=bus / "roster.json", seed="say hello",
                channel=channel)

    assert commands, "nothing was spawned"
    assert "--settings" in commands[0]
    assert str(approval.SETTINGS_PATH) in commands[0]
    assert approval.SETTINGS_PATH.is_file(), \
        "the session started pointing at a settings file that does not exist"


@pytest.mark.parametrize("channel,stdout", CHANNELS, ids=CHANNEL_IDS)
def test_a_spawn_the_approver_could_not_be_installed_for_still_opens(
    channel, stdout, bus, monkeypatch, osascript_is_on_path
):
    """Fail open, loudly. No resolvable node means no approver, and a desk that
    refuses to start is a worse outcome than a desk that prompts normally --
    but it must leave a line in the ledger rather than look identical to a
    governed one."""
    monkeypatch.setattr(approval, "resolve_node", lambda: "")
    commands: list[str] = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        _recording_run(commands, stdout))
    spawn.start(ACME, roster_path=bus / "roster.json", channel=channel)

    assert commands and "--settings" not in commands[0]
    ledger = [json.loads(line) for line in
              approval.EVENTS_PATH.read_text().splitlines() if line.strip()]
    refusals = [e for e in ledger
                if e["event"] == "approve_install" and not e["ok"]]
    assert refusals, "an ungoverned hire left no trace"
    assert refusals[-1]["reason"] == "no_node"
