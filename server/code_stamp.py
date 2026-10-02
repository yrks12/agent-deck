"""Which server CODE a desk's MCP process started with -- one stamp per server.

MEASURED 2026-10-02: PR #319 made desks obey the daemon's published browser
cap, but every desk's `computer_mcp` had started before the deploy and kept
the old code, so a desk was still refused at cap 3. `tool_stamp` only
tracks tool SCHEMAS; a code-only change never reloaded anyone.

Each MCP process stamps `CODE_SIG` (a hash of every `server/**/*.py` it could
import) when it starts. The deck, restarted by every deploy, compares it with
its own `CODE_SIG`; `server/tool_reload.py` reloads the stale live desks
between turns. Files: `<bus>/code/mcp/<server>--<desk>.json`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import tool_stamp

#: The deck's own MCP servers, each run per desk from this package.
SERVERS = ("computer", "deck", "mac")
#: Injected by tests; the deck's bus otherwise.
STAMP_ROOT: Path | None = None
SERVER_DIR = Path(__file__).resolve().parent


def fingerprint(root: Path = SERVER_DIR) -> str:
    """A hash of every `.py` under `root`, by relative path and content."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*.py")):
        try:
            body = path.read_bytes()
        except OSError:
            continue
        digest.update(str(path.relative_to(root)).encode() + b"\0" + body + b"\0")
    return digest.hexdigest()[:16]


#: This process's code, fixed at import: what it started with.
CODE_SIG = fingerprint()


def _key(server: str, desk: str) -> str:
    return f"{server}--{desk}"


def _root(root: Path | None) -> Path:
    return Path(root or STAMP_ROOT or tool_stamp.root_for("code"))


def write(server: str, desk: str, *, sig: str | None = None,
          root: Path | None = None) -> None:
    tool_stamp.write("code", _key(server, desk), sig or CODE_SIG, root=_root(root))


def stale(server: str, desk: str, *, root: Path | None = None) -> bool:
    """Did this desk's `server` process start from code other than ours?"""
    return tool_stamp.stale("code", _key(server, desk), CODE_SIG, root=_root(root))
