#!/usr/bin/env python3
"""OpenCode socket proxy + dashboard injection helper.

OpenCode TUI sessions do not expose a message-injection socket like Claude Code
does. This module runs as a small proxy beside an `opencode serve` + `opencode
attach` pair:

  - it listens on a UNIX socket at /tmp/opencode-socks/<attach_pid>.sock
  - it accepts the same JSON frame Claude Code uses:
      {"type":"user","message":{"role":"user","content":"..."}}
  - it forwards the content by running:
      opencode run --attach <url> --session <session_id> --format json "..."

The dashboard side (inject()) discovers the same socket by session PID and
writes the frame to it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SOCK_DIR = Path("/tmp/opencode-socks")
CONNECT_TIMEOUT = 2.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _read_meta(pid: int) -> dict:
    path = SOCK_DIR / f"{pid}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _find_session_id(db_path: Path, cwd: str) -> str | None:
    """Look up the active session ID for the attached directory in OpenCode's DB.

    macOS often resolves /tmp to /private/tmp, so we match exactly or by suffix.
    """
    if not db_path.exists():
        return None

    candidates = {cwd}
    # /tmp/foo -> /private/tmp/foo on macOS.
    if cwd.startswith("/") and not cwd.startswith("/private"):
        candidates.add("/private" + cwd)
    # /private/tmp/foo -> /tmp/foo
    if cwd.startswith("/private/"):
        candidates.add(cwd[len("/private"):])

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only = ON")
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id FROM session
            WHERE directory IN ({','.join('?' for _ in candidates)})
              AND time_archived IS NULL
            ORDER BY time_updated DESC LIMIT 1
            """,
            tuple(candidates),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0]
    except sqlite3.Error:
        pass
    except OSError:
        pass
    return None


class InjectError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def socket_path(pid: int) -> Path | None:
    """Return the socket path for a given OpenCode attach PID if it exists."""
    path = SOCK_DIR / f"{pid}.sock"
    if path.exists():
        return path
    return None


def frame(text: str) -> bytes:
    """The same frame Claude Code uses."""
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def inject(pid: int, text: str) -> dict:
    """Write a user message into the socket for an OpenCode attach session."""
    text = (text or "").strip()
    if not text:
        raise InjectError("empty", "nothing to send")
    if len(text) > 2000:
        raise InjectError("too_long", f"{len(text)} > 2000")

    path = socket_path(pid)
    if path is None:
        raise InjectError("no_socket", f"no socket for pid {pid}")

    blob = frame(text)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CONNECT_TIMEOUT)
            client.connect(str(path))
            client.sendall(blob)
    except OSError as exc:
        raise InjectError("unreachable", str(exc))
    return {"ok": True, "bytes": len(blob)}


def _serve_once(sock_path: Path, meta_path: Path, url: str, cwd: str, db_path: Path) -> None:
    """Run the proxy listener until the parent attach process exits."""
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(8)
    except OSError as exc:
        print(f"opencode-sock: cannot bind {sock_path}: {exc}", file=sys.stderr)
        return

    print(f"opencode-sock: listening on {sock_path}", file=sys.stderr)

    session_id: str | None = None
    while True:
        # Poll for the attached session ID until we have it.
        if not session_id:
            session_id = _find_session_id(db_path, cwd)

        try:
            conn, _ = server.accept()
        except OSError:
            break

        try:
            with conn:
                conn.settimeout(2.0)
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    print("opencode-sock: empty connection", file=sys.stderr)
                    continue

                print(f"opencode-sock: received {len(data)} bytes", file=sys.stderr)
                try:
                    payload = json.loads(data.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print("opencode-sock: invalid JSON", file=sys.stderr)
                    continue

                if payload.get("type") != "user":
                    print(f"opencode-sock: ignored type {payload.get('type')}", file=sys.stderr)
                    continue

                content = payload.get("message", {}).get("content", "")
                if not isinstance(content, str) or not content.strip():
                    print("opencode-sock: empty content", file=sys.stderr)
                    continue

                # If we still don't have a session ID, try one more time.
                if not session_id:
                    session_id = _find_session_id(db_path, cwd)

                print(f"opencode-sock: session_id={session_id}, content={content[:50]}", file=sys.stderr)

                if session_id:
                    cmd = [
                        "opencode", "run", "--attach", url,
                        "--session", session_id,
                        "--format", "json",
                        content,
                    ]
                else:
                    # Fallback: send to the server's last/default session.
                    cmd = [
                        "opencode", "run", "--attach", url,
                        "--format", "json",
                        content,
                    ]

                # Run asynchronously; don't block the socket.
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                print(f"opencode-sock: spawned opencode run", file=sys.stderr)
        except OSError as exc:
            print(f"opencode-sock: connection error: {exc}", file=sys.stderr)


def proxy_main() -> None:
    parser = argparse.ArgumentParser(description="OpenCode socket proxy")
    parser.add_argument("--socket", required=True, help="path to the UNIX socket")
    parser.add_argument("--meta", required=True, help="path to the JSON metadata file")
    parser.add_argument("--cwd", required=True, help="working directory of the session")
    parser.add_argument("--db", default=str(Path.home() / ".local" / "share" / "opencode" / "opencode.db"), help="path to OpenCode SQLite DB")
    args = parser.parse_args()

    sock_path = Path(args.socket)
    meta_path = Path(args.meta)
    db_path = Path(args.db)
    cwd = os.path.abspath(args.cwd)

    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"opencode-sock: bad metadata: {exc}", file=sys.stderr)
        sys.exit(1)

    url = meta.get("url")
    if not url:
        print("opencode-sock: missing url in metadata", file=sys.stderr)
        sys.exit(1)

    _serve_once(sock_path, meta_path, url, cwd, db_path)


if __name__ == "__main__":
    proxy_main()
