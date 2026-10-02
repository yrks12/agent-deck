"""The Mac terminal's starting directory is the desk computer's home.

MEASURED 2026-09-30: the Mac panel sent the desk's host workspace
(`/home/deckop/.claude/agent-bus/workspaces/<x>`) as `cwd`, which becomes
`docker exec --workdir` inside `deck-desk-<name>` -- a path that container does
not have, so every first command failed with an OCI chdir error. The app now
starts at `DeskShellModel.computerHome`; this pins it to `sandbox.DESK_HOME`
so the two cannot drift.
"""

import re
from pathlib import Path

from server import sandbox

SWIFT = Path(__file__).resolve().parents[1] / "macos/Sources/DeckKit/DeskShell.swift"


def test_the_mac_terminal_home_is_the_containers_home():
    found = re.search(r'static let computerHome\s*=\s*"([^"]+)"', SWIFT.read_text())
    assert found, "DeskShellModel.computerHome is gone from the Mac app"
    assert found.group(1) == sandbox.DESK_HOME


def test_the_terminal_route_runs_in_the_desks_own_container():
    argv = sandbox.exec_argv("atlas", ["pwd"], workdir=sandbox.DESK_HOME)
    assert "deck-desk-atlas" in argv
    assert argv[argv.index("--workdir") + 1] == sandbox.DESK_HOME
