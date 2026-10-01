"""What may buzz his phone: ONE classification, shared by every channel.

His words: *"they should be able to notify me when they need me and when they
send messages intended to me."* So there are exactly two labels, and anything
without one stays in the app, unannounced:

``NEEDS_YOU``  something only he can clear: a decision card (`ask`), and in the
               feed a pending approval or a waiting handoff (their own ledgers).
``FOR_YOU``    a desk's message meant for him:
               * the turn-final answer of a desk that reports to HIM;
               * the answer of any desk he wrote to since it last spoke (a
                 report's turn-final prose is otherwise its boss's business,
                 and a turn the engineer's probe started is the engineer's);
               * a line the desk marked urgent.

Deliberately NOT: `say` without `urgent` (the tool's own description calls it
the progress line -- "On it -- checking Acme first."), desk<->desk traffic,
the deck's own notices (a restart line), and anything he or the deck wrote.

The iPhone's local notifications, its background refresh, the Mac app and the
ntfy sender all read this one decision through `GET /v1/owner/alerts`, so the
phone can never buzz for a thing the Mac stays quiet about.

Pure apart from the one piece of state the rule needs: which desks owe him an
answer. Feed it the office log in write order.
"""

from __future__ import annotations

from typing import Callable

from . import office

NEEDS_YOU = "needs_you"
FOR_YOU = "for_you"

#: Written by the deck or a schedule, never by a desk speaking for itself.
_NOT_A_DESK = frozenset({*office.OWNER_SENDERS, office.ENGINEER,
                         office.ENGINEER_TEST})

#: A notification is read on a lock screen; the thread holds the rest.
BODY_MAX = 180


class Classifier:
    """Labels office-log records, in order. One per log reader."""

    def __init__(self, boss_of: Callable[[str], str | None] = lambda _n: None
                 ) -> None:
        self._boss_of = boss_of
        #: Desks he has written to since they last answered him.
        self._owed: set[str] = set()
        #: Desks whose latest turn the ENGINEER started (a probe). MEASURED
        #: on the box, first day live: 33 of 50 alerts were a test desk
        #: answering the engineer. That answer is the engineer's, not his.
        self._probed: set[str] = set()

    def classify(self, record: dict) -> str | None:
        if not isinstance(record, dict) or record.get("ack"):
            return None
        to = str(record.get("to") or "").strip()
        sender = str(record.get("from") or "").strip()
        if not to or not sender:
            return None

        if to != office.OWNER_INBOX:
            # His words to a desk: that desk's next answer is for him. Only
            # what he TYPED -- a schedule or an engineer's probe is not him
            # asking.
            # The latest asker wins: a schedule or a peer after a probe puts
            # the desk's next answer back in the ordinary rule.
            if sender in (office.ENGINEER, office.ENGINEER_TEST):
                self._probed.add(to)
                self._owed.discard(to)
            else:
                self._probed.discard(to)
                if sender in office.TYPED_BY_HIM and to not in office.OWNER_SENDERS:
                    self._owed.add(to)
            return None

        if sender in _NOT_A_DESK:
            return None
        if record.get("restarted"):
            return None  # the deck talking about the desk, not the desk
        if record.get("kind") == "decision":
            self._owed.discard(sender)
            return NEEDS_YOU
        if record.get("said"):
            return FOR_YOU if record.get("urgent") is True else None
        if record.get("spoke"):
            owed = sender in self._owed
            self._owed.discard(sender)
            if owed:
                return FOR_YOU
            if sender in self._probed or self._boss_of(sender):
                return None
            return FOR_YOU
        return None


def one_line(text: str, limit: int = BODY_MAX) -> str:
    """Whitespace folded, cut with an ellipsis. PURE."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
