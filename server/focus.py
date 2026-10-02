"""Bring a session's Terminal window to the front.

pid -> tty (via ps) -> Terminal window index (via AppleScript). Verified against
a live 8-session layout: the ttys map 1:1 onto Terminal windows.

macOS asks for Automation permission the first time this runs.
"""

from __future__ import annotations

import subprocess

FOCUS_SCRIPT = """
on run argv
  set targetTTY to item 1 of argv
  tell application "Terminal"
    repeat with w from 1 to count of windows
      repeat with t from 1 to count of tabs of window w
        if (tty of tab t of window w) is targetTTY then
          set selected of tab t of window w to true
          set index of window w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "not-found"
end run
"""


def tty_for(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    if not name or name == "??":
        return None
    return name if name.startswith("/dev/") else f"/dev/{name}"


def focus(pid: int) -> tuple[bool, str]:
    tty = tty_for(pid)
    if tty is None:
        return False, f"no tty for pid {pid} (background session?)"
    try:
        out = subprocess.run(
            ["osascript", "-", tty],
            input=FOCUS_SCRIPT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if out.returncode != 0:
        return False, (out.stderr or "osascript failed").strip()
    if out.stdout.strip() != "ok":
        return False, f"no Terminal window on {tty}"
    return True, tty
