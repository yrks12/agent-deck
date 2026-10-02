"""Live session discovery.

Primary source is the official `claude agents --json` API, which returns every
active session (interactive + background). It is a subprocess spawn, so we only
call it every REFRESH_EVERY ticks and read ~/.claude/sessions/*.json in between
-- those files carry the same fields plus updatedAt/statusUpdatedAt.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

from .. import accounts
from ..paths import SESSIONS_DIR

# `claude agents --json` costs ~1s of subprocess time, so it is not something to
# run at the collector's 1Hz tick. The per-pid session files update just as fast.
CLI_REFRESH_SECONDS = 15.0
CLI_TIMEOUT_SECONDS = 10.0


@dataclass
class RawSession:
    pid: int
    session_id: str
    cwd: str
    name: str
    status: str  # idle | busy | shell
    kind: str  # interactive | background
    started_at: int  # epoch ms
    updated_at: int = 0  # epoch ms, 0 when unknown
    status_updated_at: int = 0  # epoch ms, 0 when unknown
    version: str = ""
    source: str = "file"  # which source won, for debugging
    account: str = "main"  # whose config dir the session file is in


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _from_file(payload: dict, source: str) -> RawSession | None:
    try:
        pid = int(payload["pid"])
        return RawSession(
            pid=pid,
            session_id=str(payload.get("sessionId") or ""),
            cwd=str(payload.get("cwd") or ""),
            name=str(payload.get("name") or f"pid-{pid}"),
            status=str(payload.get("status") or "idle"),
            kind=str(payload.get("kind") or "interactive"),
            started_at=int(payload.get("startedAt") or 0),
            updated_at=int(payload.get("updatedAt") or 0),
            status_updated_at=int(payload.get("statusUpdatedAt") or 0),
            version=str(payload.get("version") or ""),
            source=source,
        )
    except (KeyError, TypeError, ValueError):
        return None


class SessionScanner:
    """Merges `claude agents --json` with the per-pid session files."""

    def __init__(self) -> None:
        self._cli_cache: dict[int, RawSession] = {}
        self._cli_fetched_at = float("-inf")
        self._cli_failed = False

    def _refresh_cli(self) -> None:
        now = time.monotonic()
        if now - self._cli_fetched_at < CLI_REFRESH_SECONDS:
            return
        self._cli_fetched_at = now
        try:
            proc = subprocess.run(
                ["claude", "agents", "--json"],
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                self._cli_failed = True
                return
            rows = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            # The session files alone are enough to keep the deck live.
            self._cli_failed = True
            return
        if not isinstance(rows, list):
            self._cli_failed = True
            return
        self._cli_failed = False
        cache: dict[int, RawSession] = {}
        for row in rows:
            if isinstance(row, dict):
                s = _from_file(row, "cli")
                if s is not None:
                    cache[s.pid] = s
        self._cli_cache = cache

    def scan(self) -> list[RawSession]:
        self._refresh_cli()

        merged: dict[int, RawSession] = dict(self._cli_cache)

        # Every account's sessions dir: a desk under a second account writes
        # its <pid>.json there, and `claude agents` (main's daemon) never
        # lists it. Missed here, the board says nobody is at the desk and the
        # next wake seats the same session twice.
        entries: list[tuple[str, object]] = []
        for ident, folder in [("main", SESSIONS_DIR), *accounts.other_dirs("sessions")]:
            try:
                entries += [(ident, p) for p in folder.glob("*.json")]
            except OSError:
                continue

        for ident, path in entries:
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            s = _from_file(payload, "file")
            if s is None:
                continue
            s.account = ident
            known = merged.get(s.pid)
            if known is None:
                merged[s.pid] = s
            else:
                # Session files carry the freshest status plus fields the CLI omits.
                known.status = s.status
                known.updated_at = s.updated_at
                known.status_updated_at = s.status_updated_at
                known.version = s.version or known.version
                known.name = s.name or known.name
                known.source = "cli+file"
                known.account = ident

        # A stale <pid>.json survives a crash; the pid check is what makes the
        # deck honest about which sessions actually exist.
        return sorted(
            (s for s in merged.values() if s.session_id and _pid_alive(s.pid)),
            key=lambda s: s.started_at,
        )
