"""Routines: cron and event triggers that wake a named agent.

Two pure pieces and a thin file-I/O shell, matching the rest of the deck's
modules. `next_fire` is a stdlib-only 5-field cron evaluator (minute hour
day-of-month month day-of-week) -- no `croniter`, no third-party dependency.
It returns the *strictly* next matching timestamp; returning `after` itself
would make a routine re-fire in a tight loop.

Persistence follows `server/manager.py:86-94`'s pattern: write to a `.tmp`
sibling, then `os.replace` -- so a reader never sees a half-written file.
Run history (last `MAX_RUNS`) lives inside `routines.json` alongside each
routine, not in a second file and not in `events.jsonl` (that belongs to the
orchestrator task).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .paths import BUS_DIR

DEFAULT_PATH = BUS_DIR / "routines.json"
MAX_RUNS = 20

# How far ahead next_fire will search before giving up on a spec that can
# never match (e.g. "0 0 30 2 *" -- February 30th does not exist).
_SEARCH_DAYS = 4 * 366 + 2


@dataclass(frozen=True)
class Routine:
    id: str
    agent: str
    prompt: str
    trigger: dict
    next_run_at: float | None = None
    enabled: bool = True


# ── cron math ────────────────────────────────────────────────────────────


def _parse_field(text: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"empty field segment in cron spec: {text!r}")
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"bad step in cron field: {part!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, end_s = base.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron field {part!r} out of range [{lo},{hi}]")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron field parsed to no values: {text!r}")
    return values


def next_fire(spec: str, tz: str, after: float) -> float:
    """The strictly-next POSIX timestamp this cron spec fires at, after `after`.

    Raises ValueError for a malformed spec or one that can never match within
    the search horizon (rather than looping forever).
    """
    fields = spec.split()
    if len(fields) != 5:
        raise ValueError(f"cron spec must have 5 fields, got {len(fields)}: {spec!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minutes = _parse_field(minute_f, 0, 59)
    hours = _parse_field(hour_f, 0, 23)
    doms = _parse_field(dom_f, 1, 31)
    months = _parse_field(month_f, 1, 12)
    dows = _parse_field(dow_f, 0, 6)  # 0 = Sunday

    dom_restricted = dom_f != "*"
    dow_restricted = dow_f != "*"

    zone = ZoneInfo(tz)
    start_dt = datetime.fromtimestamp(after, tz=zone).replace(second=0, microsecond=0)
    day0 = start_dt.replace(hour=0, minute=0)

    sorted_hours = sorted(hours)
    sorted_minutes = sorted(minutes)

    for i in range(_SEARCH_DAYS):
        day = day0 + timedelta(days=i)
        if day.month not in months:
            continue
        dom_ok = day.day in doms
        cron_dow = (day.weekday() + 1) % 7  # python Mon=0..Sun=6 -> cron Sun=0..Sat=6
        dow_ok = cron_dow in dows
        if dom_restricted and dow_restricted:
            day_ok = dom_ok or dow_ok
        elif dom_restricted:
            day_ok = dom_ok
        elif dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True
        if not day_ok:
            continue
        for h in sorted_hours:
            for m in sorted_minutes:
                candidate = day.replace(hour=h, minute=m)
                if candidate.timestamp() > after:
                    return candidate.timestamp()

    raise ValueError(f"cron spec never matches within {_SEARCH_DAYS} days: {spec!r}")


def due(routines: list[Routine], now: float) -> list[Routine]:
    return [
        r
        for r in routines
        if r.enabled and r.next_run_at is not None and r.next_run_at <= now
    ]


# ── persistence ──────────────────────────────────────────────────────────


def _read_raw(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "routines": []}
    if not isinstance(data, dict) or not isinstance(data.get("routines"), list):
        return {"version": 1, "routines": []}
    return data


def _write_raw(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)  # atomic: no reader sees a partial file


def _entry_to_routine(entry: dict) -> Routine:
    return Routine(
        id=entry["id"],
        agent=entry["agent"],
        prompt=entry["prompt"],
        trigger=entry["trigger"],
        next_run_at=entry.get("next_run_at"),
        enabled=entry.get("enabled", True),
    )


def load_routines(path: Path) -> list[Routine]:
    raw = _read_raw(path)
    return [_entry_to_routine(e) for e in raw["routines"]]


def save_routines(path: Path, routines: list[Routine]) -> None:
    existing_runs: dict[str, list] = {}
    if path.exists():
        for e in _read_raw(path)["routines"]:
            existing_runs[e.get("id")] = e.get("runs", [])
    entries = [
        {
            "id": r.id,
            "agent": r.agent,
            "prompt": r.prompt,
            "trigger": r.trigger,
            "next_run_at": r.next_run_at,
            "enabled": r.enabled,
            "runs": existing_runs.get(r.id, []),
        }
        for r in routines
    ]
    _write_raw(path, {"version": 1, "routines": entries})


def _find_entry(raw: dict, routine_id: str) -> dict | None:
    for e in raw["routines"]:
        if e.get("id") == routine_id:
            return e
    return None


def advance(path: Path, routine_id: str, *, now: float) -> None:
    """Persist the routine's next fire time. Called right after it runs.

    This is the restart-safety piece: `next_run_at` is written to disk
    immediately, so a LaunchAgent restart between fires neither re-fires a
    routine that already ran nor skips one that came due while it was down.
    """
    raw = _read_raw(path)
    entry = _find_entry(raw, routine_id)
    if entry is None:
        return
    trigger = entry.get("trigger", {})
    if trigger.get("kind") == "cron":
        entry["next_run_at"] = next_fire(trigger["spec"], trigger["tz"], now)
    else:
        entry["next_run_at"] = None
    _write_raw(path, raw)


def record_run(path: Path, routine_id: str, *, ts: float, ok: bool, detail: str) -> None:
    raw = _read_raw(path)
    entry = _find_entry(raw, routine_id)
    if entry is None:
        return
    history = entry.setdefault("runs", [])
    history.append({"ts": ts, "ok": ok, "detail": detail})
    if len(history) > MAX_RUNS:
        del history[: len(history) - MAX_RUNS]
    _write_raw(path, raw)


def runs(path: Path, routine_id: str) -> list[dict]:
    raw = _read_raw(path)
    entry = _find_entry(raw, routine_id)
    if entry is None:
        return []
    return entry.get("runs", [])
