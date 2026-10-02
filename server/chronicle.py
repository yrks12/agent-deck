"""The chronicle: one dated line per desk per day of what was done.

`server/history.py` answers "find the message where..." -- the chronicle
answers "everything since day one" in one cheap read. Owner, 2026-10-01: Atlas
could not, and offered a document "tomorrow morning" instead.

Derived MECHANICALLY from the deck's own record -- no model call, so it costs
nothing and cannot invent: who was hired, what the owner asked, what the desk
reported back (its longest replies, which are its reports), which desks it
worked with, which lessons it saved. A closed day is SEALED into
`chronicle.json` the first time anything reads past it (and by `--backfill`
on deploy), so it outlives any trim of the logs it came from; today is always
computed live.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import atomic, history, office

PATH: Path = office.BUS_DIR / "chronicle.json"
SNIP = 160                 # one ask or one report, clipped
ASKS_SHOWN = 4
REPORTS_SHOWN = 4
LINE_MAX = 900             # one desk-day, stored
TEAM_LINE_MAX = 260        # one desk-day, when the chief reads the whole team
READ_MAX = 40_000          # one read; `next` pages forward from there


def _snip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= SNIP else text[:SNIP - 1] + "…"


def _weightiest(rows: list[history.Entry], n: int) -> list[str]:
    """The `n` longest texts -- the substance, not "hello?" -- in the order
    they were said. PURE."""
    longest = sorted(rows, key=lambda e: len(e.text), reverse=True)[:n]
    return [e.text for e in rows if e in longest]


def line_for(day: str, desk: str, entries: list[history.Entry]) -> str:
    """One desk's day, from its entries. PURE."""
    team = [e.text for e in entries if e.kind == "team"]
    asked = [e for e in entries if e.kind == "owner"
             and e.who.strip().lower() in office.TYPED_BY_HIM]
    asks = _weightiest(asked, ASKS_SHOWN)
    said = [e for e in entries if e.kind == "owner" and e.who == desk
            and not e.text.startswith(office.MARK)]
    reports = _weightiest(said, REPORTS_SHOWN)
    peers: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.kind == "desk":
            other = e.peer if e.desk == desk else e.desk
            if other and other != desk:
                peers[other] += 1
    lessons = [e.text.split(" -- ")[0] for e in entries if e.kind == "lesson"]
    parts = [f"{day} {desk}:"]
    if team:
        parts.append("; ".join(team) + ".")
    if asks:
        parts.append(f"Asked ({len(asked)}): " + "; ".join(
            _snip(a) for a in asks) + ".")
    if reports:
        parts.append(f"Reported ({len(said)}): " + "; ".join(
            _snip(r) for r in reports) + ".")
    if peers:
        parts.append("Worked with " + ", ".join(
            f"{p} ({n})" for p, n in sorted(peers.items(), key=lambda kv: -kv[1])) + ".")
    if lessons:
        parts.append("Lessons: " + "; ".join(lessons) + ".")
    if len(parts) == 1:
        return ""
    text = " ".join(parts)
    return text if len(text) <= LINE_MAX else text[:LINE_MAX - 1] + "…"


def build(entries: list[history.Entry]) -> dict[str, dict[str, str]]:
    """{desk: {day: line}} for every desk-day in `entries`. PURE."""
    days: dict[tuple[str, str], list[history.Entry]] = defaultdict(list)
    for e in entries:
        day = history.day_of(e.ts)
        days[(e.desk, day)].append(e)
        if e.kind == "desk" and e.peer and e.peer != e.desk:
            days[(e.peer, day)].append(e)
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for (desk, day), rows in days.items():
        line = line_for(day, desk, rows) if desk else ""
        if line:
            out[desk][day] = line
    return dict(out)


def _load() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
    days = raw.get("days") if isinstance(raw, dict) else None
    return days if isinstance(days, dict) else {}


def seal(now: float | None = None) -> dict[str, dict[str, str]]:
    """Every desk-day: the sealed ones from the file, any closed day not yet
    sealed added to it, and today live. Returns {desk: {day: line}}."""
    today = history.day_of(time.time() if now is None else now)
    sealed = _load()
    live = build(history.entries())
    grew = False
    for desk, days in live.items():
        for day, line in days.items():
            if day < today and day not in sealed.get(desk, {}):
                sealed.setdefault(desk, {})[day] = line
                grew = True
    if grew:
        try:
            atomic.write_text(PATH, json.dumps({"version": 1, "days": sealed}))
        except OSError:
            pass
    merged = {d: dict(v) for d, v in sealed.items()}
    for desk, days in live.items():
        for day, line in days.items():
            if day >= today:
                merged.setdefault(desk, {})[day] = line
    return merged


def read(asker: str, *, desk=None, since=None, now: float | None = None) -> dict:
    """The tool: the chronicle oldest first, from `since` (a day) on."""
    who = history.scope(asker, desk)
    lo = history.parse_when(since)
    start = history.day_of(lo) if lo is not None else ""
    every = seal(now)
    cap = LINE_MAX if who else TEAM_LINE_MAX
    rows = sorted((day, d, line) for d, days in every.items()
                  if who is None or d == who
                  for day, line in days.items() if day >= start)
    lines, used, last = [], 0, ""
    for day, _d, line in rows:
        line = line if len(line) <= cap else line[:cap - 1] + "…"
        if lines and used + len(line) > READ_MAX and day != last:
            break
        lines.append(line)
        used += len(line) + 1
        last = day
    out = {"ok": True, "desk": who or history.EVERYONE, "days": len(
        {r[0] for r in rows}), "lines": lines,
        "first": rows[0][0] if rows else "", "last": rows[-1][0] if rows else ""}
    if len(lines) < len(rows):
        out["next"] = {"since": rows[len(lines)][0]}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chronicle")
    parser.add_argument("--backfill", action="store_true",
                        help="seal every closed day now (idempotent)")
    parser.parse_args(argv)
    every = seal()
    print(f"chronicle: {sum(len(v) for v in every.values())} desk-days, "
          f"{len(every)} desks, sealed in {PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
