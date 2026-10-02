"""The agent's commands, visible: every desk Bash call mirrored into its
computer's read-only `agent` terminal.

MEASURED on the box 2026-10-01: a desk is `claude --bg` running as the box
user (`ps`: `claude daemon run ... --spawned-by {"label":"claude --bg", ...}`),
so its Bash tool runs on the box in the desk's workspace, NOT in the desk's
container. Running it in the container instead would change what every desk
can reach (its repo, its tools, the deck's own CLIs), so the activity is
mirrored rather than moved: `hooks/cc-mirror.js` appends each command
(PreToolUse) and its output (PostToolUse) to `~/.deck/agent.log` inside the
container's bind-mounted home, and the container's tmux session `agent` is a
`tail -F` of that file, attached read-only.

The hook runs for real here, under node, against a temporary bus.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from server import approval

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-mirror.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture
def bus(tmp_path):
    bus = tmp_path / "agent-bus"
    (bus / "browser" / "computers" / "acme").mkdir(parents=True)
    (bus / "office.json").write_text(json.dumps(
        {"sessions": {"sid-acme": {"name": "acme"},
                      "sid-ghost": {"name": "ghost"},
                      "sid-evil": {"name": "../acme"}}}))
    return bus


def fire(bus, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "DECK_BUS_DIR": str(bus)}
    return subprocess.run([NODE, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)


def log(bus, desk="acme") -> str:
    path = bus / "browser" / "computers" / desk / ".deck" / "agent.log"
    return path.read_text() if path.exists() else ""


def pre(command, sid="sid-acme", **extra):
    return {"hook_event_name": "PreToolUse", "session_id": sid,
            "tool_name": "Bash", "cwd": "/home/owner/work/acme",
            "tool_use_id": "tu-1",
            "tool_input": {"command": command, **extra}}


def post(stdout="", stderr="", sid="sid-acme", **extra):
    return {"hook_event_name": "PostToolUse", "session_id": sid,
            "tool_name": "Bash", "cwd": "/home/owner/work/acme",
            "tool_use_id": "tu-1",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": stdout, "stderr": stderr, **extra}}


def test_the_command_shows_as_a_prompt_line_before_it_runs(bus):
    done = fire(bus, pre("git status --short"))
    assert done.returncode == 0 and done.stdout == ""
    text = log(bus)
    assert "$ git status --short" in text
    assert "acme" in text and "work/acme" in text


def test_the_output_follows_with_errors_in_red(bus):
    fire(bus, pre("make"))
    fire(bus, post(stdout="built 3 targets\n", stderr="warning: x\n"))
    text = log(bus)
    assert text.index("$ make") < text.index("built 3 targets")
    assert "\x1b[31mwarning: x" in text


def test_a_multi_line_command_keeps_its_lines_marked(bus):
    fire(bus, pre("cat <<EOF\nhello\nEOF"))
    text = log(bus)
    assert "$ cat <<EOF\n> hello\n> EOF" in text


def test_long_output_is_cut_with_a_visible_mark(bus):
    many = "".join(f"line {i}\n" for i in range(1000))
    fire(bus, post(stdout=many))
    text = log(bus)
    assert "line 0\n" in text
    assert "line 999" not in text
    assert "more lines not shown" in text


def test_dangerous_escapes_are_stripped_but_colours_kept(bus):
    """Output is the agent's; a title change or a clipboard write (OSC 52)
    must not reach the owner's terminal. Colours may."""
    fire(bus, post(stdout="\x1b]52;c;cHduZWQ=\x07\x1b]0;owned\x07"
                          "\x1b[32mgreen\x1b[0m\r\n"))
    text = log(bus)
    assert "\x1b]" not in text and "\x07" not in text
    assert "\x1b[32mgreen\x1b[0m" in text


def test_a_background_command_says_so(bus):
    fire(bus, post(backgroundTaskId="bg-1"))
    assert "running in the background" in log(bus)


def test_a_desk_without_a_computer_gets_nothing_written(bus):
    fire(bus, pre("ls", sid="sid-ghost"))
    assert not (bus / "browser" / "computers" / "ghost").exists()


def test_a_name_that_walks_out_of_the_tree_is_ignored(bus):
    fire(bus, pre("ls", sid="sid-evil"))
    assert not (bus / "browser" / "computers" / ".deck").exists()
    assert log(bus) == ""


def test_other_tools_are_not_mirrored(bus):
    payload = pre("ls")
    payload["tool_name"] = "Edit"
    fire(bus, payload)
    assert log(bus) == ""


def test_the_log_is_rotated_not_grown_forever(bus):
    deck = bus / "browser" / "computers" / "acme" / ".deck"
    deck.mkdir()
    (deck / "agent.log").write_text("x" * (2 * 1024 * 1024))
    fire(bus, pre("ls"))
    assert (deck / "agent.log").stat().st_size < 64 * 1024


def test_garbage_on_stdin_is_still_exit_zero(bus):
    env = {**os.environ, "DECK_BUS_DIR": str(bus)}
    done = subprocess.run([NODE, str(HOOK)], input="{nope", text=True,
                          capture_output=True, env=env, timeout=10)
    assert done.returncode == 0 and done.stdout == ""


# ── registered on every hired desk, for Bash only ──────────────────────────


def test_hired_desks_run_the_mirror_before_and_after_bash():
    doc = approval.settings_document("/usr/bin/node")
    for event in ("PreToolUse", "PostToolUse"):
        entries = [e for e in doc["hooks"][event]
                   if any("cc-mirror.js" in h["command"] for h in e["hooks"])]
        assert entries, f"cc-mirror.js is not registered on {event}"
        assert entries[0]["matcher"] == "Bash"
