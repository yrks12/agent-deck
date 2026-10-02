"""What the agent's browser looks like right now: one frame, not a stream.

`server/browser.py` puts a real Chrome on a virtual X display and
`server/browser_takeover.py` stops it at a login or a card field and asks the owner
to drive. Neither is any use if he cannot *see* the screen he is being asked to
take over. This module is that surface -- the panel the reference product calls
"<Agent>'s screen".

**Snapshots, deliberately, and not a video stream.** A still frame refreshed
every second or two is enough to see what a browser is doing: a page loaded, a
form half filled, a spinner that has not moved in ten minutes. Against that,
three properties a stream does not have:

* **It costs nothing when nobody is looking.** No viewer, no ffmpeg. A
  continuous encode per desk runs whether or not the panel is open, on the same
  box as the Chrome it is recording, and the first thing it starves is the
  thing being watched.
* **It cannot wedge the event loop.** A frame grab is a short subprocess that
  writes a file; the daemon serves bytes off disk and never holds a socket
  open per viewer. A streaming design puts a long-lived connection, a
  backpressure story and a reconnect story into the middle of the deck.
* **It survives a restart.** The newest frame is a file. A daemon that comes
  back finds it, ages it, and can say "this feed died twenty minutes ago" --
  which a dropped socket cannot say at all.

So: if you are about to replace this with a WebSocket, have a reason that
beats those three, and write it down here.

**A frame without an age is a lie.** A JPEG of a checkout page looks identical
whether it was captured a second ago or before the capture process died. "The
agent is sitting still" and "the feed is dead" are opposite facts and the panel
must be able to tell them apart, so `newest()` returns the age beside the path
and never the path alone. `STALE_AFTER` is where the deck draws that line.

**Two things this module refuses to do:**

1. **Grab a display it was not given.** x11grab captures whatever display it is
   pointed at, and the human's own desktop is `:0` on the box the daemon runs
   on. `snapshot_argv` states the display explicitly and never reads `DISPLAY`
   from the environment; the assertion in tests/test_screen.py is an identity
   ("`-i` is exactly this"), not an absence.
2. **Turn a bad desk name into a file path.** The name arrives from
   roster.json, which a human edits. `"../../etc"` is refused rather than
   sanitised -- trimming it to its last segment writes a chosen filename into a
   directory nobody chose, and serves one desk's screen as another's. Desk and
   display validation are `server.browser`'s, reused rather than restated so
   there is one answer to "what is a legal desk name".

No HTTP route lives here; `server/api.py` is where a route would go, and this
module is a library it can call rather than a surface of its own.

**The named gap.** Nothing here runs ffmpeg. `snapshot_argv` is pure and its
argv is asserted against, but *that this argv actually produces a readable
JPEG of a live Xvfb* is unproven -- there is no `tests/test_screen_live.py`
yet, and this Mac has no X server to write one against. That belongs beside
tests/test_browser_live.py, `-m live`, on the Linux box. Until it exists, the
capture path is a claim about a command line and not about reality.

Stdlib only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Private on purpose in `browser`, and imported on purpose here: a second copy
# of "what is a legal desk name" is a second answer, and the two drift.
from .browser import DEFAULT_ROOT, _check_desk, _check_display

# JPEG, not PNG: a browser screenshot is a photograph of text and gradients,
# and PNG's lossless encode of one costs several times the bytes for a picture
# nobody is going to pixel-peep.
FRAME_SUFFIX = ".jpg"

# Where frames live under the browser root. Same directory `browser.start()`
# already creates for captures.
FRAMES_DIRNAME = "screens"

# ffmpeg's `-q:v` for mjpeg: 2 is near-lossless, 31 is a mess. 4 keeps form
# labels and error text readable, which is the entire point of looking.
SNAPSHOT_QUALITY = 4

# A single frame of a 1280x800 desktop is tens of kilobytes. A megabyte is
# already far outside that, so anything bigger is a capture pointed at
# something it should not be, or an ffmpeg writing something that is not a
# still -- either way it is not going in the state directory.
MAX_FRAME_BYTES = 4 * 1024 * 1024

# Older than this and the panel must not present the frame as what the agent
# is looking at now. Not enforced here -- `newest()` reports the age and the
# caller decides how to say it -- because "stale" reads differently to a panel
# (grey it out) and to a handoff (say the feed died).
STALE_AFTER = 30.0


def snapshot_argv(display: str, out_path: str) -> list[str]:
    """One JPEG of `display`, then exit. PURE -- runs nothing.

    The safety property is the input: ffmpeg grabs whatever display it is
    handed, so the display is stated here and validated, never inherited from
    the environment. A daemon started from a desktop session has `DISPLAY=:0`
    exported, and a fallback to it would publish the human's own screen -- his
    mail, his terminals -- into a panel.

    `-frames:v 1` is the other half: this is a grab that terminates, not a
    per-desk encoder left running on the same box as the Chrome it is
    watching. `-y` because the destination is a temp file that may already
    exist, and `-loglevel error` because the caller discards stdout anyway and
    a banner in a log tells nobody anything.
    """
    display = _check_display(display)
    return [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-f", "x11grab",
        "-i", display,
        "-frames:v", "1",
        "-q:v", str(SNAPSHOT_QUALITY),
        "-f", "image2",
        str(out_path),
    ]


def frame_path(desk: str, root: Path) -> Path:
    """Where `desk`'s newest frame lives. PURE -- creates nothing.

    One file per desk, which is what bounds the disk: a watched desk costs one
    frame, not one frame per refresh. The name is validated, not sanitised,
    for the reason in the module docstring.
    """
    name = _check_desk(desk)
    return (Path(root) / FRAMES_DIRNAME / f"{name}{FRAME_SUFFIX}").resolve()


class ScreenCache:
    """The newest frame per desk, on disk, with an age.

    Deliberately not an in-memory dict. The frames outlive the process that
    captured them, so a restarted daemon can still answer "when was this
    screen last seen" instead of showing a blank panel and calling it idle.
    """

    def __init__(self, root: Path = DEFAULT_ROOT, *,
                 max_bytes: int = MAX_FRAME_BYTES) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_bytes)

    # ── writing ────────────────────────────────────────────────────────────

    def put(self, desk: str, data: bytes) -> Path:
        """Store `data` as `desk`'s current frame. Returns the path.

        Written to a temporary file and renamed into place, so a panel reading
        while a capture is writing gets the previous whole frame rather than
        half of the next one. `os.replace` is atomic within a directory.

        Empty and oversized frames are refused: a zero-byte file is a failed
        capture that would render as a broken image and read as "the browser
        is showing nothing", which is a different and much calmer claim than
        "the capture failed".
        """
        path = frame_path(desk, self.root)
        blob = bytes(data or b"")
        if not blob:
            raise ValueError(f"refusing to cache an empty frame for {desk!r}")
        if len(blob) > self.max_bytes:
            raise ValueError(
                f"frame for {desk!r} is {len(blob)} bytes, over the "
                f"{self.max_bytes}-byte cap"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory (rename across filesystems is not atomic) and a
        # suffix the sweep's glob does not match, so a crash mid-write leaves
        # litter rather than a file that looks like somebody's screen.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return path

    # ── reading ────────────────────────────────────────────────────────────

    def newest(self, desk: str) -> tuple[Path, float] | None:
        """`(path, age_in_seconds)` for `desk`, or None if nothing is cached.

        The age is the whole point. Returning a path alone would let a panel
        show a twenty-minute-old checkout page as though it were live, and the
        difference between "the agent is thinking" and "the feed died" is the
        difference between waiting and going to look.

        Age comes from the file's mtime rather than a timestamp this process
        remembers, so it stays true across a daemon restart. A clock that
        moved backwards clamps to 0.0 rather than reporting a negative age.
        """
        path = frame_path(desk, self.root)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        return path, max(0.0, time.time() - mtime)

    # ── sweeping ───────────────────────────────────────────────────────────

    def sweep(self, *, now: float, max_age: float) -> list[str]:
        """Delete frames older than `max_age`. Returns the desks dropped.

        The current frame must survive: a sweep that cleared the directory
        would satisfy "the stale one is gone" and leave every panel on the box
        blank. So the comparison is per file and strictly on age.

        Only files this cache wrote are touched -- the glob is the frame
        suffix, and the part-files `put` uses are hidden from it -- because
        this runs on a timer against a directory inside the deck's own state
        dir, and a sweeper that deletes by directory rather than by pattern is
        one refactor away from taking the browser profiles with it.

        Returns names rather than paths so a caller can say *whose* feed went
        away without parsing a filename back into a desk.
        """
        folder = self.root / FRAMES_DIRNAME
        dropped: list[str] = []
        try:
            entries = sorted(folder.glob(f"*{FRAME_SUFFIX}"))
        except OSError:  # pragma: no cover - unreadable state dir
            return dropped
        for entry in entries:
            try:
                age = float(now) - entry.stat().st_mtime
            except OSError:  # pragma: no cover - vanished under us
                continue
            if age <= float(max_age):
                continue
            try:
                entry.unlink()
            except OSError:  # pragma: no cover - best effort by design
                continue
            dropped.append(entry.name[:-len(FRAME_SUFFIX)])
        return dropped
