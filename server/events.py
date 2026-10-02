"""Event wake-ups (Feature C): a real event wakes the desk that owns it.

Before this, every "did anyone pay / reply / sign up / break the deploy?" was a
routine that woke a desk on a timer and spent a model turn looking. Here the
deck hears the event itself -- `POST /v1/events`, the Stripe webhook, or a
poller that runs in the deck process and spends no model tokens -- and hands
it to the ONE desk the routing table names, as that desk's message.

The core is pure apart from the files under `root` (BUS_DIR/events):

    routes.json   the routing table: (source, account glob, kind glob) -> desk
    log.jsonl     recent events and what became of each (bounded)
    seen.json     refs already taken, so a retried webhook is not news twice

Delivery is injected (`deliver(desk, text) -> state`), and so is the clock.
A burst for one desk is COALESCED: the first event goes at once, the rest that
arrive inside `window` seconds are held and go as one message when it closes,
so twenty payments in a minute cost the desk two turns, not twenty.
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import atomic

#: Bounds. The log is what `GET /v1/events` draws; the seen set is what makes
#: a webhook retry a no-op. Each is trimmed back to its bound once it doubles.
LOG_MAX = 1000
SEEN_MAX = 5000
ROUTES_MAX = 200
SUMMARY_MAX = 1000
FIELD_MAX = 200

#: Seconds a desk's window stays open after a delivery.
WINDOW_SECONDS = 60.0

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class EventError(ValueError):
    """An event or a route the deck cannot use. `str()` is the reason."""


def _text(raw: object, field: str, limit: int, *, required: bool) -> str:
    value = "" if raw is None else str(raw).strip()
    if required and not value:
        raise EventError(f"{field} is required")
    if len(value) > limit:
        raise EventError(f"{field} is longer than {limit} characters")
    return value


@dataclass(frozen=True)
class Event:
    source: str      # stripe, gmail, instagram, signup, deploy, ...
    account: str     # which site / inbox / repo it belongs to
    kind: str        # payment, reply, dm, signup, failed, ...
    summary: str     # one line a desk can act on
    ref: str         # the source's own id: dedupe key
    ts: float

    @classmethod
    def from_dict(cls, raw: object, *, now: float) -> "Event":
        if not isinstance(raw, dict):
            raise EventError("an event is a JSON object")
        source = _text(raw.get("source"), "source", 64, required=True).lower()
        if not _NAME.match(source):
            raise EventError("source must be lowercase letters, digits, - _ .")
        ts = raw.get("ts")
        return cls(
            source=source,
            account=_text(raw.get("account"), "account", FIELD_MAX, required=False),
            kind=_text(raw.get("kind"), "kind", 64, required=False).lower(),
            summary=_text(raw.get("summary"), "summary", SUMMARY_MAX, required=True),
            ref=_text(raw.get("ref"), "ref", FIELD_MAX, required=True),
            ts=float(ts) if isinstance(ts, (int, float)) and ts > 0 else now,
        )


@dataclass(frozen=True)
class Route:
    source: str
    desk: str
    account: str = "*"
    kind: str = "*"

    @classmethod
    def from_dict(cls, raw: object) -> "Route":
        if not isinstance(raw, dict):
            raise EventError("a route is a JSON object")
        return cls(
            source=_text(raw.get("source"), "route source", 64, required=True).lower(),
            desk=_text(raw.get("desk"), "route desk", 64, required=True),
            account=_text(raw.get("account"), "route account", FIELD_MAX,
                          required=False) or "*",
            kind=_text(raw.get("kind"), "route kind", 64, required=False).lower() or "*",
        )

    def to_dict(self) -> dict:
        return {"source": self.source, "account": self.account,
                "kind": self.kind, "desk": self.desk}

    def matches(self, event: Event) -> bool:
        return (fnmatch.fnmatchcase(event.source.lower(), self.source)
                and fnmatch.fnmatchcase(event.account.lower(), self.account.lower())
                and fnmatch.fnmatchcase(event.kind.lower(), self.kind))


def route_for(routes: list[Route], event: Event) -> str | None:
    """The desk the first matching route names, or None. PURE."""
    return next((r.desk for r in routes if r.matches(event)), None)


def message_for(batch: list[Event]) -> str:
    """What the desk reads. PURE. Says it is an event, from where, and that
    the desk need not poll for it."""
    head = ("1 event" if len(batch) == 1 else f"{len(batch)} events")
    lines = [f"{head} arrived by itself (Event wake-ups):"]
    for ev in batch:
        where = "/".join(p for p in (ev.source, ev.account, ev.kind) if p)
        lines.append(f"- [{where}] {' '.join(ev.summary.split())} (ref {ev.ref})")
    lines.append("You do not need to poll for these: the deck tells you when "
                 "the next one arrives.")
    return "\n".join(lines)


class Store:
    """The three files under `root`. Thread-safe; every write is atomic."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    @property
    def routes_path(self) -> Path:
        return self.root / "routes.json"

    def routes(self) -> list[Route]:
        try:
            raw = json.loads(self.routes_path.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for item in (raw.get("routes") if isinstance(raw, dict) else None) or []:
            try:
                out.append(Route.from_dict(item))
            except EventError:
                continue  # one bad hand edit must not drop every route
        return out

    def save_routes(self, raw: object) -> list[Route]:
        if not isinstance(raw, list):
            raise EventError("routes is a JSON list")
        if len(raw) > ROUTES_MAX:
            raise EventError(f"at most {ROUTES_MAX} routes")
        routes = [Route.from_dict(r) for r in raw]
        self.root.mkdir(parents=True, exist_ok=True)
        atomic.write_text(self.routes_path, json.dumps(
            {"version": 1, "routes": [r.to_dict() for r in routes]}, indent=1))
        return routes

    def log(self, record: dict) -> None:
        path = self.root / "log.jsonl"
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
            lines = path.read_text().splitlines()
            if len(lines) > 2 * LOG_MAX:
                atomic.write_text(path, "\n".join(lines[-LOG_MAX:]) + "\n")

    def recent(self, limit: int = 100) -> list[dict]:
        """Newest first, one row per ref: its latest state over its event."""
        try:
            lines = (self.root / "log.jsonl").read_text().splitlines()
        except OSError:
            return []
        rows: dict[str, dict] = {}
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            key = f"{rec.get('source')}:{rec.get('ref')}"
            rows[key] = {**rows.get(key, {}), **rec}
        ordered = sorted(rows.values(), key=lambda r: r.get("logged_at", 0), reverse=True)
        return ordered[:limit]

    def seen_refs(self) -> list[str]:
        try:
            raw = json.loads((self.root / "seen.json").read_text())
        except (OSError, ValueError):
            return []
        return [str(r) for r in raw] if isinstance(raw, list) else []

    def claim(self, key: str) -> bool:
        """Mark `key` seen. False when it already was."""
        with self._lock:
            seen = self.seen_refs()
            if key in seen:
                return False
            seen.append(key)
            if len(seen) > 2 * SEEN_MAX:
                seen = seen[-SEEN_MAX:]
            self.root.mkdir(parents=True, exist_ok=True)
            atomic.write_text(self.root / "seen.json", json.dumps(seen))
            return True


class Hub:
    """Route, dedupe, coalesce and deliver. One per deck."""

    def __init__(self, store: Store, *, deliver: Callable[[str, str], str],
                 clock: Callable[[], float] = time.time,
                 window: float = WINDOW_SECONDS,
                 resolve: Callable[[str], str] = lambda desk: desk) -> None:
        self.store = store
        self.deliver = deliver
        self.clock = clock
        self.window = window
        self.resolve = resolve
        self._lock = threading.Lock()
        self._open_until: dict[str, float] = {}
        self._held: dict[str, list[Event]] = {}

    def _record(self, ev: Event, desk: str | None, state: str) -> None:
        self.store.log({**asdict(ev), "desk": desk, "state": state,
                        "logged_at": self.clock()})

    def ingest(self, ev: Event) -> dict:
        if not self.store.claim(f"{ev.source}:{ev.ref}"):
            return {"ok": True, "state": "duplicate", "desk": None, "ref": ev.ref}
        routed = route_for(self.store.routes(), ev)
        if routed is None:
            self._record(ev, None, "unrouted")
            return {"ok": True, "state": "unrouted", "desk": None, "ref": ev.ref}
        desk = self.resolve(routed)
        now = self.clock()
        with self._lock:
            if self._open_until.get(desk, 0.0) > now:
                self._held.setdefault(desk, []).append(ev)
                held = True
            else:
                self._open_until[desk] = now + self.window
                held = False
        if held:
            self._record(ev, desk, "held")
            return {"ok": True, "state": "held", "desk": desk, "ref": ev.ref}
        state = self._send(desk, [ev])
        return {"ok": True, "state": state, "desk": desk, "ref": ev.ref}

    def _send(self, desk: str, batch: list[Event]) -> str:
        try:
            state = self.deliver(desk, message_for(batch))
        except Exception as exc:  # noqa: BLE001 - a delivery never kills the hub
            state = f"failed: {exc!r}"
        for ev in batch:
            self._record(ev, desk, state)
        return state

    def flush(self) -> list[dict]:
        """Deliver every held batch whose window has closed."""
        now = self.clock()
        due: list[tuple[str, list[Event]]] = []
        with self._lock:
            for desk, batch in list(self._held.items()):
                if batch and self._open_until.get(desk, 0.0) <= now:
                    due.append((desk, batch))
                    del self._held[desk]
                    self._open_until[desk] = now + self.window
        return [{"desk": desk, "count": len(batch), "state": self._send(desk, batch)}
                for desk, batch in due]
