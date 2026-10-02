"""OpenCode session discovery.

OpenCode (opencode.ai CLI) writes a shared log file and an SQLite database on the
local machine. We watch the log to decide whether a session is WORKING (recent log
activity) or IDLE, and we read the database for stable session metadata. The
scanner keeps a byte offset so each tick is incremental and cheap.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..paths import OPENCODE_DB, OPENCODE_LOG

# A session is WORKING if the shared log shows activity within this window.
RECENT_LOG_SECONDS = 10.0

# Parsing a key=value log line where values are unquoted or double-quoted.
_KV_RE = re.compile(r'(\w+(?:\.\w+)*)="((?:[^"\\]|\\.)*)"|(\w+(?:\.\w+)*)=(\S+)')


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _parse_kv(line: str) -> dict:
    """Turn `key=value key2="quoted value"` into a dict."""
    out = {}
    for m in _KV_RE.finditer(line):
        key = m.group(1) or m.group(3)
        value = m.group(2) if m.group(1) else m.group(4)
        out[key] = value
    return out


def _parse_timestamp(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class OpencodeSession:
    """Same public shape as RawSession plus a few OpenCode-specific fields."""

    pid: int
    session_id: str
    cwd: str
    name: str
    status: str  # idle | busy
    kind: str  # always "interactive" for now
    started_at: int  # epoch ms
    updated_at: int = 0  # epoch ms
    status_updated_at: int = 0  # epoch ms
    version: str = ""
    source: str = "opencode"
    model: str = ""
    agent: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    last_log_at: float = 0.0


@dataclass
class _ProcessInfo:
    pid: int
    cwd: str


@dataclass
class _Candidate:
    sid: str
    cwd: str
    name: str
    last_log: float
    model: str
    agent: str
    started_at: int
    updated_at: int
    version: str
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_write: int
    source: str


@dataclass
class _LogTail:
    """Incremental tail over the shared OpenCode log file."""

    path: Path
    _offset: int = 0
    _inode: int | None = None
    last_seen: dict[str, float] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    directories: dict[str, str] = field(default_factory=dict)

    def _reset(self) -> None:
        self._offset = 0
        self.last_seen.clear()
        self.models.clear()
        self.directories.clear()

    def poll(self) -> dict[str, float]:
        """Read new log lines and return a fresh copy of the last-seen map."""
        try:
            stat = self.path.stat()
        except OSError:
            return dict(self.last_seen)

        if self._inode is not None and (stat.st_ino != self._inode or stat.st_size < self._offset):
            self._reset()
        self._inode = stat.st_ino

        if stat.st_size <= self._offset:
            return dict(self.last_seen)

        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(stat.st_size - self._offset)
        except OSError:
            return dict(self.last_seen)

        cut = chunk.rfind(b"\n")
        if cut == -1:
            return dict(self.last_seen)
        self._offset += cut + 1

        for line in chunk[:cut].decode("utf-8", errors="replace").split("\n"):
            if not line.strip():
                continue
            self._apply(line)

        return dict(self.last_seen)

    def _apply(self, line: str) -> None:
        kv = _parse_kv(line)
        ts = _parse_timestamp(kv.get("timestamp", ""))
        if not ts:
            return
        sid = kv.get("session.id")
        if not sid:
            return
        self.last_seen[sid] = ts
        if kv.get("modelID"):
            self.models[sid] = kv["modelID"]
        if kv.get("cwd"):
            self.directories[sid] = kv["cwd"]


def _parse_model(model_json: str) -> str:
    if not model_json:
        return ""
    try:
        parsed = json.loads(model_json)
        if isinstance(parsed, dict):
            mid = parsed.get("id")
            if isinstance(mid, str):
                return mid
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _discover_opencode_processes() -> list[_ProcessInfo]:
    """Return currently-running `opencode` processes and their launch directories."""
    try:
        proc = subprocess.run(
            ["ps", "-x", "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    pids: list[int] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, comm = parts
        if comm.strip() == "opencode":
            try:
                pids.append(int(pid_str))
            except ValueError:
                continue

    processes: list[_ProcessInfo] = []
    for pid in pids:
        if not _pid_alive(pid):
            continue
        cwd = _proc_cwd(pid)
        if cwd:
            processes.append(_ProcessInfo(pid=pid, cwd=cwd))
    return processes


def _proc_cwd(pid: int) -> str:
    """Best-effort cwd of another process, portable across macOS and Linux."""
    # macOS / BSD style via lsof.
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if line.startswith("n"):
                    return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass

    # Linux /proc fallback.
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (OSError, ValueError):
        pass

    # Last resort: try to infer cwd from `ps` args (not always possible).
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            args = out.stdout.strip()
            # opencode often has the cwd as the first arg or is launched from it.
            if args:
                return os.path.dirname(args.split()[0]) or ""
    except (OSError, subprocess.SubprocessError):
        pass

    return ""


def _read_db_sessions(path: Path) -> list[dict]:
    """Read the session table with a short timeout; degrade on lock/error."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only = ON")
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, directory, title, version, model,
                   tokens_input, tokens_output,
                   tokens_cache_read, tokens_cache_write,
                   time_created, time_updated, agent
            FROM session
            WHERE time_archived IS NULL
            """
        )
        rows = cur.fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    except OSError:
        return []

    cols = [
        "id", "directory", "title", "version", "model",
        "tokens_input", "tokens_output",
        "tokens_cache_read", "tokens_cache_write",
        "time_created", "time_updated", "agent",
    ]
    return [dict(zip(cols, row)) for row in rows]


class OpencodeScanner:
    """Discovers live OpenCode sessions by merging DB, log, and process data."""

    def __init__(self, log_path: Path = OPENCODE_LOG, db_path: Path = OPENCODE_DB) -> None:
        self.log_path = log_path
        self.db_path = db_path
        self._tail = _LogTail(path=log_path)

    def scan(self) -> list[OpencodeSession]:
        now = time.time()
        log = self._tail.poll()
        processes = _discover_opencode_processes()

        # Group live processes by directory. A directory may host several
        # processes, so we must not collapse them into a single pid.
        procs_by_dir: dict[str, list[_ProcessInfo]] = {}
        for proc in processes:
            if not _pid_alive(proc.pid):
                continue
            procs_by_dir.setdefault(proc.cwd, []).append(proc)

        db_rows = _read_db_sessions(self.db_path)
        candidates: dict[str, list[_Candidate]] = {}
        seen_ids: set[str] = set()

        for row in db_rows:
            sid = row["id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

            cwd = row["directory"] or ""
            if cwd not in procs_by_dir:
                continue

            last_log = log.get(sid, 0.0)
            if not last_log and row["time_updated"]:
                last_log = row["time_updated"] / 1000

            model = _parse_model(row["model"] or "")
            if not model:
                model = self._tail.models.get(sid, "")

            candidates.setdefault(cwd, []).append(
                _Candidate(
                    sid=sid,
                    cwd=cwd,
                    name=row["title"] or f"opencode-{sid[-8:]}",
                    last_log=last_log,
                    model=model,
                    agent=str(row["agent"] or ""),
                    started_at=int(row["time_created"] or 0),
                    updated_at=int(row["time_updated"] or 0),
                    version=str(row["version"] or ""),
                    tokens_input=int(row["tokens_input"] or 0),
                    tokens_output=int(row["tokens_output"] or 0),
                    tokens_cache_read=int(row["tokens_cache_read"] or 0),
                    tokens_cache_write=int(row["tokens_cache_write"] or 0),
                    source="opencode",
                )
            )

        # If we can't open the DB, still surface sessions we can map from the log
        # to live processes. This is a graceful degradation path.
        for sid, ts in log.items():
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            cwd = self._tail.directories.get(sid)
            if not cwd or cwd not in procs_by_dir:
                continue

            candidates.setdefault(cwd, []).append(
                _Candidate(
                    sid=sid,
                    cwd=cwd,
                    name=f"opencode-{sid[-8:]}",
                    last_log=ts,
                    model=self._tail.models.get(sid, ""),
                    agent="",
                    started_at=0,
                    updated_at=int(ts * 1000),
                    version="",
                    tokens_input=0,
                    tokens_output=0,
                    tokens_cache_read=0,
                    tokens_cache_write=0,
                    source="opencode+log",
                )
            )

        sessions: list[OpencodeSession] = []
        for cwd, procs in procs_by_dir.items():
            # A directory may have stale DB rows from older sessions. Only show
            # as many sessions as there are live processes, preferring the most
            # recently active ones.
            rows = candidates.get(cwd, [])
            rows.sort(key=lambda c: c.last_log, reverse=True)
            for proc, row in zip(procs, rows):
                status = "busy" if (now - row.last_log) < RECENT_LOG_SECONDS else "idle"
                sessions.append(
                    OpencodeSession(
                        pid=proc.pid,
                        session_id=row.sid,
                        cwd=row.cwd,
                        name=row.name,
                        status=status,
                        kind="interactive",
                        started_at=row.started_at,
                        updated_at=row.updated_at,
                        status_updated_at=row.updated_at,
                        version=row.version,
                        source=row.source,
                        model=row.model,
                        agent=row.agent,
                        tokens_input=row.tokens_input,
                        tokens_output=row.tokens_output,
                        tokens_cache_read=row.tokens_cache_read,
                        tokens_cache_write=row.tokens_cache_write,
                        last_log_at=row.last_log,
                    )
                )

        return sorted(sessions, key=lambda s: s.started_at)
