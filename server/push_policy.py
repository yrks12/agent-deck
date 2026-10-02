"""When the deck may actually buzz him. ONE policy for ntfy, the iPhone and the Mac.

`owner_alerts` decides WHAT is about him: cards that need him, and a reply he
is owed. This module decides WHEN any of that becomes a push. It exists
because of a measured complaint. The first live hour sent 81 pushes; he said
*"why do notifications go off so much? It's really bothering me."*

The rules:

1. **Settle.** A card waits `settle` seconds before it is pushed. Most
   approvals are cleared in a few seconds by the auto-reviewer, a standing
   rule or his own tap in the app, and those must never buzz.
2. **Wanted.** At push time each held item is checked live: a card still
   waiting, or a reply still unread. Anything settled meanwhile is dropped.
3. **Presence.** Nothing is pushed while he is looking: a read, a message
   from him, or an app in the foreground within `presence` seconds. Held
   items go out once he has left, if they are still wanted. That is how a
   reply he is owed reaches him: he wrote, he left, the desk answered.
4. **Coalesce.** Within `window` seconds of a push, new items wait, then go
   out together as one summary ("3 agents need you").
5. **Cap.** At most `cap` pushes per hour. Anything over the cap waits and
   goes out as one summary once a slot frees.
6. **Quiet hours.** Optional, off by default. Items wait until the window ends.
7. **A ring** (a desk calling him) skips 1-5: it is pushed at once, once.

Each card is pushed once. Offered ids are remembered, on disk when `path` is
given, so a restart neither replays a pending card nor resets the cap.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from . import atomic
from .owner_alerts import NEEDS_YOU

WINDOW = 120.0
CAP = 6
CAP_PERIOD = 3600.0
SETTLE = 30.0
PRESENCE = 120.0
#: Ids remembered so a card is offered once. Far more than a day of cards.
MEMORY = 2000
#: Pushes kept for `GET /v1/owner/alerts`.
KEEP = 200


def quiet_window(spec: str | None, clock=time.localtime
                 ) -> Callable[[float], bool] | None:
    """`"22:00-07:00"` -> `inside(epoch_seconds)`, in box-local time by
    default. None when empty or unreadable: off, never "always quiet"."""
    raw = str(spec or "").strip()
    if not raw:
        return None
    try:
        a, b = raw.split("-")
        start = int(a.split(":")[0]) * 60 + int(a.split(":")[1])
        end = int(b.split(":")[0]) * 60 + int(b.split(":")[1])
    except (ValueError, IndexError):
        return None
    if not (0 <= start < 1440 and 0 <= end < 1440) or start == end:
        return None

    def inside(at: float) -> bool:
        t = clock(at)
        minute = t.tm_hour * 60 + t.tm_min
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    return inside


def _cursor(at: float, ident: str) -> str:
    return f"{int(round(at * 1_000_000)):018d}-{ident}"


class PushPolicy:
    def __init__(self, *, window: float = WINDOW, cap: int = CAP,
                 cap_period: float = CAP_PERIOD, settle: float = SETTLE,
                 presence: float = PRESENCE,
                 quiet: Callable[[float], bool] | None = None,
                 path: Path | str | None = None) -> None:
        self.window, self.cap, self.cap_period = window, cap, cap_period
        self.settle, self.presence, self.quiet = settle, presence, quiet
        self.path = Path(path) if path else None
        self._held: list[tuple[float, dict]] = []
        self._offered: list[str] = []
        self._offered_set: set[str] = set()
        self._pushed_at: list[float] = []
        self._seen_at = float("-inf")
        self.pushes: list[dict] = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self._offered = [str(i) for i in state.get("offered") or []][-MEMORY:]
        self._offered_set = set(self._offered)
        self._pushed_at = [float(t) for t in state.get("pushed_at") or []]
        self.pushes = [p for p in state.get("pushes") or [] if isinstance(p, dict)]

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic.write_text(self.path, json.dumps({
                "offered": self._offered[-MEMORY:],
                "pushed_at": self._pushed_at[-(self.cap * 4):],
                "pushes": self.pushes[-KEEP:],
            }))
        except OSError:
            pass

    # -- inputs --------------------------------------------------------------

    @staticmethod
    def _key(alert: dict) -> str:
        """A card's identity is the card, whatever route announced it."""
        if alert.get("card_id"):
            return f"{alert.get('source')}:{alert['card_id']}"
        return str(alert.get("id"))

    def offer(self, alert: dict, *, at: float) -> bool:
        key = self._key(alert)
        if key in self._offered_set:
            return False
        self._offered.append(key)
        self._offered_set.add(key)
        if len(self._offered) > MEMORY:
            dropped = self._offered[: len(self._offered) - MEMORY]
            self._offered = self._offered[-MEMORY:]
            self._offered_set.difference_update(dropped)
        self._held.append((float(at), alert))
        self._save()
        return True

    def seen(self, *, at: float) -> None:
        """He is looking: a read, a message from him, an app in front."""
        self._seen_at = max(self._seen_at, float(at))

    def present(self, at: float) -> bool:
        return at - self._seen_at < self.presence

    def ring(self, alert: dict, *, at: float) -> dict | None:
        """A desk calling him (`server/ringing.py`): pushed NOW, once.

        A ring lasts 30 seconds, so settle, presence, coalesce and the hourly
        cap -- all of which hold an item for minutes -- would turn it into a
        missed call. Its own gate (`ringing.decide`) already caps it at a few a
        day and one per reason; here it is deduped like any card, on disk."""
        key = self._key(alert)
        if key in self._offered_set:
            return None
        self._offered.append(key)
        self._offered_set.add(key)
        push = {**alert, "id": f"push:{key}", "ts": at, "count": 1,
                "alert_ids": [str(alert.get("id"))],
                "cursor": _cursor(at, f"push-{key}")}
        self.pushes.append(push)
        self.pushes = self.pushes[-KEEP:]
        self._save()
        return push

    # -- the decision ----------------------------------------------------------

    def tick(self, at: float, wanted: Callable[[dict], bool]) -> list[dict]:
        if not self._held:
            return []
        self._held = [(t, a) for t, a in self._held if wanted(a)]
        ready = [(t, a) for t, a in self._held if at - t >= self.settle]
        if not ready:
            return []
        if self.present(at):
            return []
        if self.quiet is not None and self.quiet(at):
            return []
        recent = [t for t in self._pushed_at if at - t < self.cap_period]
        if len(recent) >= self.cap:
            return []
        if recent and at - max(recent) < self.window:
            return []
        taken = {id(a) for _t, a in ready}
        self._held = [(t, a) for t, a in self._held if id(a) not in taken]
        push = self._compose([a for _t, a in ready], at)
        self._pushed_at = recent + [at]
        self.pushes.append(push)
        self.pushes = self.pushes[-KEEP:]
        self._save()
        return [push]

    def _compose(self, items: list[dict], at: float) -> dict:
        first = items[0]
        ident = f"push-{int(at * 1000)}"
        base = {
            "id": f"push:{int(at * 1000)}",
            "source": "push",
            "ts": at,
            "cursor": _cursor(at, ident),
            "count": len(items),
            "alert_ids": [str(a.get("id")) for a in items],
            "urgent": any(a.get("urgent") is True for a in items),
        }
        if len(items) == 1:
            return {**first, **base, "source": first.get("source"),
                    "title": first.get("title") or first.get("agent") or "Agent Deck"}
        needs = [a for a in items if a.get("kind") == NEEDS_YOU]
        replies = [a for a in items if a.get("kind") != NEEDS_YOU]
        agents: list[str] = []
        for a in items:
            who = str(a.get("who") or a.get("agent") or "")
            if who and who not in agents:
                agents.append(who)
        threads = {a.get("thread_id") for a in items}
        parts = []
        if needs:
            n = len({a.get("agent") for a in needs})
            parts.append(f"{n} agent{'s' if n != 1 else ''} need{'' if n != 1 else 's'} you")
        if replies:
            parts.append(f"{len(replies)} repl{'ies' if len(replies) != 1 else 'y'} for you")
        if len(agents) > 1:
            names = ", ".join(agents[:-1]) + " and " + agents[-1]
        else:
            names = agents[0] if agents else ""
        return {**base,
                "kind": NEEDS_YOU if needs else first.get("kind"),
                "agent": first.get("agent") if len(threads) == 1 else "",
                "thread_id": first.get("thread_id") if len(threads) == 1 else "",
                "card_id": "", "message_id": "",
                "title": " · ".join(parts),
                "body": names}
