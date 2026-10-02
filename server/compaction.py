"""Surviving a compaction: a desk must not forget whose it is.

**The failure.** A long-running session fills its window and compacts. The
early turns are replaced by a summary, and the opening brief goes with them --
including the one line that says *your boss is Acme, not the owner*. The desk
keeps working and starts reporting to the owner directly. Nothing errors,
nothing logs, and from the inside the desk is behaving perfectly reasonably.
That is the whole problem: it degrades silently.

**Where the fault is.** Not in compaction -- compaction is doing its job. The
fault is delivering identity as a *message*, because a message is exactly the
thing compaction is allowed to summarise away. The system prompt is re-sent on
every request and cannot be.

So the fix is two layers, in this order:

1. **Prevention** (`server/spawn.py`): the full `hire.brief()` goes to
   `--append-system-prompt`, not just the one-line mission. This is the real
   fix. `tests/test_compaction.py` pins that the boss is named there.
2. **Detection and repair** (this module + `hooks/cc-compact.js`): every
   compaction is recorded on the bus so it is visible on the board, and the
   brief is re-injected afterwards. Belt to the system prompt's braces -- if
   layer 1 ever stops holding, layer 2 says so out loud instead of leaving a
   quietly confused desk.

**Measured (2026-09-01, Claude Code 2.1.252):** `PreCompact` and `PostCompact`
are real hook events in the CLI. **Assumed, not measured:** that an
`--append-system-prompt` value survives a compaction unchanged. It is re-sent
with every request, so architecturally it must -- but nobody has watched a real
session compact and checked. That is precisely why layer 2 exists and why
`record()` is not optional.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .hire import brief
from .paths import BUS_DIR
from .roster import Desk

__all__ = ["DEFAULT_BUS", "record", "repair_message"]

#: The same ledger `hooks/cc-bus.js` appends to. One log, not two.
DEFAULT_BUS = BUS_DIR / "events.jsonl"


def record(path: Path, *, session_id: str, agent: str | None,
           trigger: str, ts: float | None = None) -> dict:
    """Append one `compact` line to the bus and return it.

    Same shape as every other bus event (`ts` float, `event`, `session_id`),
    so `server/sources/bus.py` reads it without a new parser and the board can
    show *why* a desk suddenly got vaguer.
    """
    line = {
        "ts": time.time() if ts is None else float(ts),
        "event": "compact",
        "session_id": session_id,
        "agent": agent or "",
        "trigger": trigger,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return line


def repair_message(desk: Desk | None) -> str | None:
    """What to re-inject after a compaction, or None when there is nothing to say.

    `None` for a session with no desk is deliberate: an ad-hoc terminal window
    has no charter to restore, and injecting a generic lecture into somebody's
    scratch session is worse than doing nothing at all.
    """
    if desk is None:
        return None
    return (
        "Your conversation was just compacted, so the opening brief may have "
        "been summarised away. Re-read it and carry on from where you were:\n\n"
        + brief(desk)
    )


def atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace, the convention used everywhere else in this package."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".compact-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
