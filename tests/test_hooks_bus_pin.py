"""S2: a desk running under a second Claude account still reports to the ONE bus.

Every hook found the bus as `$CLAUDE_CONFIG_DIR/agent-bus`. A desk under
account `work` runs with CLAUDE_CONFIG_DIR=<work dir>, so its hooks would write
events, read mail and look for the deck token in a bus nobody reads: the board
goes blind to that desk and its mail never arrives. `DECK_BUS_DIR`, set by
`accounts.env_for` (and MEASURED in probe P2 to reach the `--bg` worker's
environ), wins over CLAUDE_CONFIG_DIR. Unset, every hook behaves as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _hooks_naming_the_bus() -> list[Path]:
    return sorted(p for p in HOOKS.glob("*.js") if '"agent-bus"' in p.read_text())


def test_every_hook_that_names_the_bus_honours_deck_bus_dir_first():
    """The class sweep: every `"agent-bus"` path in every hook sits behind a
    `process.env.DECK_BUS_DIR ||`, so a hook added later cannot forget it
    without this failing by name."""
    found = _hooks_naming_the_bus()
    assert len(found) >= 6, [p.name for p in found]
    unpinned = []
    for hook in found:
        text = hook.read_text()
        for m in re.finditer(r'"agent-bus"', text):
            if "process.env.DECK_BUS_DIR ||" not in text[max(0, m.start() - 260):m.start()]:
                line = text.count("\n", 0, m.start()) + 1
                unpinned.append(f"{hook.name}:{line}")
    assert not unpinned, f"bus paths that ignore DECK_BUS_DIR: {unpinned}"


def _run(hook: str, payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, str(HOOKS / hook)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=15, env=env)


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DECK_BUS_DIR", "CLAUDE_CONFIG_DIR")}
    env.update(extra)
    return env


@needs_node
def test_cc_bus_writes_to_the_pinned_bus_not_the_accounts_dir(tmp_path):
    pinned, other = tmp_path / "bus", tmp_path / "work-account"
    pinned.mkdir()
    other.mkdir()
    done = _run("cc-bus.js", {"hook_event_name": "Stop", "session_id": "s-work"},
                _env(DECK_BUS_DIR=str(pinned), CLAUDE_CONFIG_DIR=str(other)))
    assert done.returncode == 0, done.stderr
    lines = (pinned / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["session_id"] == "s-work"
    assert not (other / "agent-bus").exists()


@needs_node
def test_cc_bus_without_the_pin_is_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    done = _run("cc-bus.js", {"hook_event_name": "Stop", "session_id": "s-main"},
                _env(CLAUDE_CONFIG_DIR=str(home)))
    assert done.returncode == 0, done.stderr
    assert "s-main" in (home / "agent-bus" / "events.jsonl").read_text()


@needs_node
def test_cc_office_reads_mail_from_the_pinned_bus(tmp_path):
    bus, other = tmp_path / "bus", tmp_path / "work-account"
    bus.mkdir()
    other.mkdir()
    now = time.time()
    (bus / "office.json").write_text(json.dumps({
        "generated_at": now,
        "sessions": {"sid-w": {"name": "atlas", "cwd": "/srv/w", "toplevel": "/srv/w",
                               "branch": "main", "state": "IDLE",
                               "address": "uds:/tmp/1.sock"}}}))
    (bus / "messages.jsonl").write_text(json.dumps(
        {"ts": now, "to": "sid-w", "id": "m-1", "from": "owner",
         "text": "the pinned bus delivered this"}) + "\n")
    done = _run("cc-office.js", {"session_id": "sid-w",
                                 "hook_event_name": "UserPromptSubmit"},
                _env(DECK_BUS_DIR=str(bus), CLAUDE_CONFIG_DIR=str(other)))
    assert done.returncode == 0, done.stderr
    assert "the pinned bus delivered this" in done.stdout
