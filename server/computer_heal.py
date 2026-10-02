"""A desk whose live session lost its computer tools gets them back.

MEASURED 2026-10-01, desk atlas: a deploy killed its computer MCP server;
Claude reconnected a fresh one but dropped every `mcp__computer__*` tool from
the session's list, and for an hour the desk said its browser was gone.

Each `deferred_tools_delta` attachment in the session transcript lists the
tools added, removed and re-added and the MCP servers pending or failed;
replaying them says whether the computer tools are there NOW (`lost`). The
cure is `claude respawn <job>`: same job and session, no copy. Never
mid-turn: a desk that is working, waiting on the owner, or has anything in
flight but background monitors is looked at again next sweep.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "mcp__computer__"
SERVER = "computer"
#: A server still "pending" this long after its tools went is not coming back.
PENDING_MAX = 180.0
#: One respawn per desk per this long: a respawn that does not cure it must not
#: become a restart loop.
COOLDOWN = 900.0
#: Job states that mean "between turns".
BETWEEN_TURNS = frozenset({"idle", "done"})

#: desk -> when it was last respawned.
_memory: dict = {}


# ── the detector ────────────────────────────────────────────────────────────


def _at(stamp) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")
                                      ).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def read_records(path: Path) -> list[dict]:
    """The tool-list deltas of one session transcript, oldest first. Reads
    only the lines that can be one: transcripts run to tens of MB."""
    out: list[dict] = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if "deferred_tools_delta" not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                att = row.get("attachment") if isinstance(row, dict) else None
                if not isinstance(att, dict) or att.get("type") != "deferred_tools_delta":
                    continue
                out.append({
                    "at": _at(row.get("timestamp")),
                    "added": list(att.get("addedNames") or []),
                    "removed": list(att.get("removedNames") or []),
                    "readded": list(att.get("readdedNames") or []),
                    "pending": list(att.get("pendingMcpServers") or []),
                    "failed": list(att.get("failedMcpServers") or []),
                })
    except OSError:
        return []
    return out


def lost(records: list[dict], *, now: float) -> str | None:
    """Why this session's computer tools are gone, or None. PURE.

    `server_failed`: the server is marked failed and its tools have not come
    back. `tools_missing`: tools it once had were removed and not re-added,
    and the server is not just reconnecting (pending for under `PENDING_MAX`).
    """
    have: set = set()
    seen = failed = False
    pending_since: float | None = None
    for rec in records:
        for name in rec["added"] + rec["readded"]:
            if str(name).startswith(PREFIX):
                have.add(name)
                seen = True
                failed = False
        for name in rec["removed"]:
            have.discard(name)
        if SERVER in rec["failed"]:
            failed = True
        pending_since = (pending_since or rec["at"]) if SERVER in rec["pending"] \
            else None
    if have:
        return None
    if failed:
        return "server_failed"
    if seen:
        if pending_since is not None and now - pending_since < PENDING_MAX:
            return None
        return "tools_missing"
    return None


# ── what the box has ────────────────────────────────────────────────────────


def _transcript(job: dict) -> Path | None:
    from . import accounts
    sid = str(job.get("sessionId") or "")
    if not sid:
        return None
    acct = accounts.get(str(job.get("account") or "")) or accounts.main_account()
    base = accounts.dirs(acct).projects
    guess = Path(base) / re.sub(r"[^A-Za-z0-9]", "-", str(job.get("cwd") or "")) \
        / f"{sid}.jsonl"
    if guess.exists():
        return guess
    found = sorted(Path(base).glob(f"*/{sid}.jsonl"))
    return found[0] if found else None


def _records(job: dict) -> list[dict]:
    path = _transcript(job)
    return read_records(path) if path else []


def _jobs() -> list[dict]:
    """The live job of every claude desk on the roster."""
    from . import connectors, office, roster, spawn
    out = []
    try:
        desks = roster.load_roster(roster.DEFAULT_PATH)
    except Exception:  # noqa: BLE001 - a bad roster is nobody's heal
        return out
    for desk in desks:
        if desk.engine != "claude":
            continue
        for sid in sorted(office.live_session_ids(desk.name) or ()):
            state = connectors._job_state(sid)
            if state:
                out.append({**state, "name": desk.name,
                            "account": spawn.locate_job(session_id=sid)})
    return out


def _desk_state(name: str) -> str:
    from . import connectors
    return connectors._desk_state(name)


def _respawn(short: str) -> bool:
    from . import spawn
    return spawn.respawn_job(short)


# ── the procedure ───────────────────────────────────────────────────────────


def _quiet(job: dict) -> bool:
    flight = job.get("inFlight") or {}
    busy = int(flight.get("tasks") or 0) - int(flight.get("drainableMonitors") or 0)
    return str(job.get("state") or "") in BETWEEN_TURNS and busy <= 0


def sweep(*, now: float | None = None, jobs=_jobs, records=_records,
          desk_state=_desk_state, respawn=_respawn,
          memory: dict | None = None) -> dict:
    """One pass. Returns {desk: "respawned" | "respawn_failed"} for each desk
    acted on; a desk that is fine, busy, or in its cooldown is not listed."""
    from .connectors import BUSY_STATES
    now = time.time() if now is None else now
    memory = _memory if memory is None else memory
    acted: dict = {}
    for job in jobs():
        name, short = str(job.get("name") or ""), str(job.get("daemonShort") or "")
        if not name or not short or name in acted:
            continue
        if not _quiet(job) or desk_state(name) in BUSY_STATES:
            continue
        if now - memory.get(name, -COOLDOWN) < COOLDOWN:
            continue
        why = lost(records(job), now=now)
        if why is None:
            continue
        memory[name] = now
        ok = respawn(short)
        print(f"[agent-deck] heal: {name} lost its computer tools ({why}); "
              f"respawn {'ok' if ok else 'FAILED'}", flush=True)
        acted[name] = "respawned" if ok else "respawn_failed"
    return acted
