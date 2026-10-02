"""A desk's durable record, searchable -- what it has done since day one.

Owner, 2026-10-01, to Atlas: "tell me everything we did since I started
working with you". MEASURED on the box: Atlas's thread holds 1,222 messages
back to 2026-09-06; its restart at 20:11 replayed the newest 77
(`office.HISTORY_MAX`), so it said the record was gone and offered a document
"tomorrow morning". The deck had the whole record; the desk could not read it.

So the record is a tool. Read straight from what the deck keeps and no session
owns: `messages.jsonl` (his thread with every desk, and desk-to-desk), the bus
ledger (hires, retires, renames, account moves) and team memory (lessons). A
desk sees its own; the chief of staff sees the team. Renames are followed.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import learning, office, roster

#: The roster this reads (and the ledger and aliases beside it); a test points
#: it at its own.
ROSTER_PATH: Path | None = None
LIMIT_DEFAULT = 40
LIMIT_MAX = 200
TEXT_MAX = 700
KINDS = ("owner", "desk", "team", "lesson")
#: `desk` value the chief passes for the whole team.
EVERYONE = "*"
_TEAM_EVENTS = ("hire", "retire", "account_move", "desk")


class HistoryError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class Entry:
    ts: float
    kind: str      # one of KINDS
    desk: str      # the desk it belongs to, by the name it goes by NOW
    who: str       # who said or did it
    to: str
    text: str
    peer: str = ""  # the other desk, on a desk-to-desk message

    def row(self) -> dict:
        when = _dt.datetime.fromtimestamp(self.ts, _dt.timezone.utc)
        return {"when": when.strftime("%Y-%m-%d %H:%M"), "kind": self.kind,
                "desk": self.desk, "from": self.who, "to": self.to,
                "text": _clip(self.text)}


def day_of(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _roster_path() -> Path:
    return Path(ROSTER_PATH or roster.DEFAULT_PATH)


def _jsonl(path: Path) -> Iterator[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def _event_entry(rec: dict, canon) -> Entry | None:
    ev, ts = rec.get("event"), float(rec.get("ts") or 0)
    if ev == "hire":
        name = canon(str(rec.get("name") or ""))
        return Entry(ts, "team", name, str(rec.get("by") or "owner"), name,
                     f"hired {rec.get('name')} (reports to "
                     f"{rec.get('reports_to') or 'the owner'})")
    if ev == "retire":
        name = canon(str(rec.get("name") or ""))
        return Entry(ts, "team", name, str(rec.get("by") or ""), name,
                     f"retired {rec.get('name')}: {rec.get('reason') or ''}")
    if ev == "account_move":
        name = canon(str(rec.get("desk") or ""))
        return Entry(ts, "team", name, "deck", name,
                     f"moved from account {rec.get('from')} to {rec.get('to')}")
    if (ev == "desk" and rec.get("result") == "patched" and rec.get("name")
            and rec.get("actor") != rec.get("name")):
        name = canon(str(rec.get("name")))
        return Entry(ts, "team", name, str(rec.get("actor") or ""), name,
                     f"{rec.get('actor')} now goes by {rec.get('name')}")
    return None


def _lesson_ts(updated: str) -> float:
    try:
        return _dt.datetime.strptime(updated, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def entries() -> list[Entry]:
    """Every entry the deck keeps, oldest first, for the whole team."""
    # Late: `onboard` and `hire` import the brief, which imports the deck's
    # tools, which import this module.
    from . import hire, onboard
    path = _roster_path()
    aliases = onboard.load_aliases(onboard.aliases_path(path))

    def canon(name: str) -> str:
        return onboard.resolve(aliases, name) if name else name

    out: list[Entry] = []
    for rec in _jsonl(office.MESSAGES_FILE):
        if rec.get("ack") or not rec.get("text"):
            continue
        ts = float(rec.get("ts") or 0)
        to, who = str(rec.get("to") or ""), str(rec.get("from") or "")
        if office.is_owner(who):
            out.append(Entry(ts, "owner", canon(to), who, to, rec["text"]))
        elif to == office.OWNER_INBOX or office.is_owner(to):
            out.append(Entry(ts, "owner", canon(who), who, to, rec["text"]))
        else:
            out.append(Entry(ts, "desk", canon(who), who, to, rec["text"],
                             peer=canon(to)))
    for rec in _jsonl(hire.events_path(path)):
        if rec.get("event") in _TEAM_EVENTS:
            entry = _event_entry(rec, canon)
            if entry is not None and entry.desk:
                out.append(entry)
    for lesson in learning.lessons():
        for by in lesson.by or ("",):
            out.append(Entry(_lesson_ts(lesson.updated), "lesson", canon(by),
                             by, "", f"{lesson.title} -- {lesson.hook}"))
    out.sort(key=lambda e: e.ts)
    return out


def scope(asker: str, desk: str | None) -> str | None:
    """Whose record `asker` may read: a desk name, or None for the whole team
    (the chief of staff only). Raises `own_only` for anyone else's."""
    want = str(desk or "").strip()
    desks = roster.load_roster(_roster_path())
    if roster.chief(desks) == asker:
        return None if want in ("", EVERYONE) else want
    if want in ("", asker):
        return asker
    raise HistoryError("own_only", "a desk reads its own history; the chief "
                       "of staff reads the team's")


def mine(entry: Entry, desk: str | None) -> bool:
    return desk is None or desk in (entry.desk, entry.peer)


def parse_when(value) -> float | None:
    """'2026-09-06', an ISO time, or epoch seconds. None when absent."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return float(text)
    except ValueError:
        pass
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise HistoryError("bad_time", f"not a date: {value!r}") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.timestamp()


def search(asker: str, *, query=None, since=None, until=None, desk=None,
           kind=None, limit=None) -> dict:
    """The tool. With `since`: the OLDEST matches from then on, and `next`
    pages forward. Without: the NEWEST matches, and `next` pages back."""
    who = scope(asker, desk)
    if kind not in (None, "") and kind not in KINDS:
        raise HistoryError("bad_kind", f"kind is one of {', '.join(KINDS)}")
    lo, hi = parse_when(since), parse_when(until)
    words = str(query or "").lower().split()
    try:
        n = max(1, min(LIMIT_MAX, int(limit or LIMIT_DEFAULT)))
    except (TypeError, ValueError):
        n = LIMIT_DEFAULT
    hits = [e for e in entries()
            if mine(e, who) and (not kind or e.kind == kind)
            and (lo is None or e.ts >= lo) and (hi is None or e.ts < hi)
            and all(w in f"{e.who} {e.to} {e.text}".lower() for w in words)]
    page = hits[:n] if lo is not None else hits[-n:]
    more = len(hits) > len(page)
    out = {"ok": True, "desk": who or EVERYONE, "matched": len(hits),
           "entries": [e.row() for e in page], "more": more}
    if hits:
        out["first"] = day_of(hits[0].ts)
    if more:
        out["next"] = ({"since": page[-1].ts + 1e-6} if lo is not None
                       else {"until": page[0].ts})
    return out
