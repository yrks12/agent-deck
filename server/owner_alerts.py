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

import re
from typing import Callable

from . import office

NEEDS_YOU = "needs_you"
FOR_YOU = "for_you"

#: Every kind of card a desk can raise that waits on his decision or his
#: hands, and how long it settles before it buzzes. A decision (`ask`), a
#: takeover / sign-in / payment / guard card (`handoff`, every kind), a tool
#: approval, a desk calling him (`ring`, pushed at once by its own path), and
#: a desk's PROPOSED standing approval (`standing`, `server/standing.py`).
#: Approvals wait out the auto-reviewer, which clears most in seconds; the
#: others only he clears. A new card type must be named here
#: (`tests/test_needs_you_always_reaches_him.py`).
NEEDS_YOU_SOURCES = {"decision": 10.0, "handoff": 10.0, "approval": 30.0,
                     "ring": 0.0, "standing": 10.0}

#: Written by the deck or a schedule, never by a desk speaking for itself.
_NOT_A_DESK = frozenset({*office.OWNER_SENDERS, office.ENGINEER,
                         office.ENGINEER_TEST})

#: A notification is read on a lock screen; the thread holds the rest.
BODY_MAX = 180


_TEST_WORDS = re.compile(r"\b(test|probe)\b", re.I)


def is_test_desk(*, name: str, label: str, test: bool) -> bool:
    """A desk that exists to be probed: the roster flag, or a name or label
    that says so. MEASURED: `wake-probe` carries `test: false` and the label
    "wake-probe (engineer test desk)", so the flag alone let it through."""
    return bool(test) or bool(_TEST_WORDS.search(str(label or ""))) \
        or bool(_TEST_WORDS.search(str(name or "").replace("-", " ")))


class Classifier:
    """Labels office-log records, in order. One per log reader."""

    def __init__(self, boss_of: Callable[[str], str | None] = lambda _n: None,
                 is_test: Callable[[str], bool] = lambda _n: False) -> None:
        self._boss_of = boss_of
        self._is_test = is_test
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

        if sender in _NOT_A_DESK or self._is_test(sender):
            self._owed.discard(sender)
            return None
        if record.get("restarted"):
            return None  # the deck talking about the desk, not the desk
        if record.get("kind") == "decision":
            self._owed.discard(sender)
            return NEEDS_YOU
        if record.get("file"):
            # A file a desk sent him (`deck_mcp.send_file`): he asked to see
            # what the desks make, so it is for him.
            return FOR_YOU
        if record.get("said"):
            return FOR_YOU if record.get("urgent") is True else None
        if record.get("spoke"):
            # Only a reply to HIM: the first answer after he wrote. Every
            # other turn-final line is in the app, unannounced. MEASURED: 45
            # of 81 pushes in an hour were the chief's plain replies.
            owed = sender in self._owed
            self._owed.discard(sender)
            return FOR_YOU if owed else None
        return None


def one_line(text: str, limit: int = BODY_MAX) -> str:
    """Whitespace folded, cut with an ellipsis. PURE."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
