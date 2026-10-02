"""The premise this whole feature rests on: can an outside process put a user
message into a running Claude session?

The frame is measured -- it comes from the CLI's own startup log. Delivery is
not, until this passes. Spends a few tokens; opt in with `-m live`.

Two things bit hard enough to be worth writing down:

* A session spawned from inside another Claude session inherits
  CLAUDE_CODE_CHILD_SESSION and writes NO transcript, so asserting on transcript
  records silently passes on someone else's file. The child env is scrubbed.
* The TUI paints with cursor moves rather than spaces, so screen assertions must
  strip ANSI *and* whitespace before matching.
"""

import fcntl
import json
import os
import pty
import re
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

from server import manager
from server.paths import PROJECTS_DIR, slug_for

pytestmark = pytest.mark.live

MARKER = "PROBEZED-9042"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][B0]|\x1b\]0;[^\x07]*\x07")


def _flatten(chunks: list[str]) -> str:
    """Screen text with escapes and layout whitespace removed."""
    return ANSI.sub("", "".join(chunks)).replace(" ", "").replace("-", "").lower()


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                        "CLAUDE_CODE_MESSAGING_SOCKET")}
    env["TERM"] = "xterm-256color"
    # Even with the marker gone, a child still skips persistence unless forced.
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    return env


def test_injected_text_reaches_a_live_session(short_tmp):
    work = short_tmp / "work"
    work.mkdir()
    socks = short_tmp / "socks"
    socks.mkdir(mode=0o700)
    sock = socks / "1.sock"

    project = PROJECTS_DIR / slug_for(str(work))
    before = {p.name for p in project.glob("*.jsonl")} if project.is_dir() else set()

    primary, secondary = pty.openpty()
    fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    os.set_blocking(primary, False)
    proc = subprocess.Popen(
        ["claude", "--messaging-socket-path", str(sock)],
        cwd=str(work),
        stdin=secondary, stdout=secondary, stderr=secondary,
        close_fds=True, env=_child_env(),
    )
    os.close(secondary)
    screen: list[str] = []

    def drain(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                data = os.read(primary, 65536)
                if data:
                    screen.append(data.decode("utf-8", "replace"))
            except BlockingIOError:
                pass
            except OSError:
                break
            time.sleep(0.1)

    try:
        for _ in range(120):          # up to 60s to bind
            drain(0.5)
            if sock.exists():
                break
        assert sock.exists(), "claude never bound the messaging socket"
        drain(6)

        if "trust" in _flatten(screen):
            os.write(primary, b"\r")   # a fresh directory asks once
            drain(8)

        manager.inject(1, f"say {MARKER} and nothing else", sock_dir=socks)

        deadline = time.time() + 90
        while time.time() < deadline and MARKER.lower().replace("-", "") not in _flatten(screen):
            drain(2)

        assert MARKER.lower().replace("-", "") in _flatten(screen), (
            "injected text never reached the session's UI"
        )

        # Transcript persistence cannot be asserted here. A claude spawned from
        # inside another claude refuses to write one -- scrubbing
        # CLAUDE_CODE_CHILD_SESSION and setting
        # CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 both fail to re-enable it.
        # Real Terminal sessions (what the deck actually watches) do persist, so
        # this is reported rather than asserted; the delivered/pending marker is
        # covered by unit tests over synthetic transcripts instead.
        new = [p for p in project.glob("*.jsonl") if p.name not in before] if project.is_dir() else []
        if not new:
            print("\nNOTE: child session wrote no transcript; delivery proven via UI only")
        else:
            landed = any(
                json.loads(line).get("type") == "user"
                for path in new
                for line in path.open(errors="ignore")
                if MARKER in line
            )
            assert landed, "injected text never appeared as a user record"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(primary)
