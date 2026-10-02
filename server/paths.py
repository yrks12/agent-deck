"""Filesystem locations Shaliach reads. Single source of truth."""

from __future__ import annotations

import os
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))

SESSIONS_DIR = CLAUDE_HOME / "sessions"        # <pid>.json, one per live session
PROJECTS_DIR = CLAUDE_HOME / "projects"        # <slug>/<sessionId>.jsonl
# events.jsonl, written by our hooks. `DECK_BUS_DIR` first, exactly as the JS
# hooks do: a desk on a second Claude account runs -- with its deck, computer
# and mac MCPs -- under that account's CLAUDE_CONFIG_DIR, and `accounts.env_for`
# pins this beside it. The board reads only the deck's one bus.
BUS_DIR = Path(os.environ.get("DECK_BUS_DIR") or CLAUDE_HOME / "agent-bus")
BUS_FILE = BUS_DIR / "events.jsonl"

# OpenCode CLI data (a separate agent that runs alongside Claude Code).
OPENCODE_HOME = Path(os.environ.get("OPENCODE_CONFIG_DIR", Path.home() / ".local" / "share" / "opencode"))
OPENCODE_LOG = OPENCODE_HOME / "log" / "opencode.log"
OPENCODE_DB = OPENCODE_HOME / "opencode.db"
OPENCODE_AUTH_JSON = OPENCODE_HOME / "auth.json"

# Claude Code encodes a cwd into a project-dir slug by replacing each
# non-alphanumeric character with "-" (one dash per character, not per run):
#   /Users/x/Projects/Foo    -> -Users-x-Projects-Foo
#   /Users/x/.claude/skills  -> -Users-x--claude-skills
def slug_for(cwd: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in cwd)


def transcript_path(cwd: str, session_id: str) -> Path:
    return PROJECTS_DIR / slug_for(cwd) / f"{session_id}.jsonl"


def subagents_dir(cwd: str, session_id: str) -> Path:
    return PROJECTS_DIR / slug_for(cwd) / session_id / "subagents"
