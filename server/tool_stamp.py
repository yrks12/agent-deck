"""Which tool set a desk's MCP process serves -- one stamp per server per desk.

MEASURED 2026-10-01, twice: a desk's MCP process keeps the code it started
with. Desks running before the Mac control tools shipped had no
mcp__mac__click, and Atlas, started before `call_owner` shipped, told the
owner "the calling tool isn't loaded in my session... restart my desk".

So each MCP process writes, when it starts, a signature of the tool set it
serves. The deck compares it with the signature of the code it is running now
(`stale`); a missing stamp is a process started before stamps existed, which
is stale too. `server/tool_reload.py` reloads those desks between turns.

Files: `<bus>/<server>/mcp/<desk>.json` -- {"desk", "tools", "names", "ts"}.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .paths import BUS_DIR


def signature(tools: list[dict]) -> str:
    """A hash of the whole tool list -- names, descriptions and schemas -- so
    a changed description reloads a desk as surely as a new tool. PURE."""
    blob = json.dumps(tools, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def root_for(server: str, root: Path | None = None) -> Path:
    return Path(root) if root else BUS_DIR / server / "mcp"


def path(server: str, desk: str, *, root: Path | None = None) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in desk)
    return root_for(server, root) / f"{safe or '_'}.json"


def write(server: str, desk: str, sig: str, *, names=(),
          root: Path | None = None) -> None:
    target = path(server, desk, root=root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"desk": desk, "tools": sig,
                                      "names": sorted(names), "ts": time.time()}))
    except OSError:
        pass   # a stamp never stops the desk's tools from serving


def read(server: str, desk: str, *, root: Path | None = None) -> dict | None:
    try:
        row = json.loads(path(server, desk, root=root).read_text())
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def stale(server: str, desk: str, sig: str, *, root: Path | None = None) -> bool:
    """Is this desk's `server` process missing the tools this code serves?"""
    row = read(server, desk, root=root)
    return row is None or row.get("tools") != sig
