"""One atomic write, used everywhere, with a temp name that is never shared.

**The bug this exists to kill.** Every atomic write in this package used to
derive its temp path from the target: `roster.json` -> `roster.json.tmp`. That
name is a constant, so two writers race on the *same* temp file:

    A: writes roster.json.tmp
    B: writes roster.json.tmp          -- clobbers A's bytes
    A: os.replace(tmp, roster.json)    -- the temp is now gone
    B: os.replace(tmp, roster.json)    -- FileNotFoundError

A's write is lost, B raises, and between the two replaces there is a window
where `roster.json` does not exist at all. To a reader that is not "a stale
roster", it is **no roster** -- every desk vanishes from the board.

`os.replace` was always atomic. Deriving the *source* from the destination was
the part that was not. `tempfile.mkstemp` in the same directory gives each
writer a private file, and the replace stays on one filesystem so it remains a
rename rather than a copy.

Found by a concurrency test on `POST /v1/agents` and then swept across the
package: `roster`, `asking`, `autoreview`, `handoff`, `manager`, `office`,
`vault` and `api` all carried it. `compaction` did not -- it already used
mkstemp, which is why that one is the shape everything else now follows.

This does **not** make a load-modify-write sequence safe. Two readers that each
load, edit and save still lose one edit; that needs a lock at the call site.
This guarantees only that the file on disk is always complete and always there.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["write_text", "write_bytes"]


def write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write `data` to `path` atomically. Never leaves a partial file.

    `mode` is applied to the temp file *before* the replace, so there is no
    instant where the destination exists with the wrong permissions -- which
    matters for `vault.json`, where a 0644 window is a leaked secret.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Leave the previous contents of `path` untouched. Losing yesterday's
        # roster to today's serialisation bug is the compounding failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str, *, mode: int | None = None,
               encoding: str = "utf-8") -> None:
    """`write_bytes` for text. Encoding happens before anything is created,
    so a value that cannot be encoded never touches the filesystem."""
    write_bytes(path, text.encode(encoding), mode=mode)
