"""One window, one meaning: a pending thing that nobody answered goes away.

Both queues in this system are *questions to a human*. `asking.py` holds "may I
run this", `handoff.py` holds "I cannot do this, you must". Neither had any
notion of time, so an unanswered record sat `pending` / `waiting` forever, and a
board built on them fills with rows the owner can no longer act on — at which point
he stops reading the board, which is the only failure that actually matters.

**Expiry is a removal, never a decision.** This is the one thing to get right.
When an ask expires, the agent that raised it is *still sitting on its own
permission prompt*, unanswered — Claude Code drew that prompt in its own
terminal and nothing here can dismiss it (spec M1). So timing the question out
changes exactly one thing: it leaves the owner's board. It writes no rule, it grants
nothing, and `autoreview.evaluate` still answers `ask` for the same call the
next time it comes round. The opposite reading — "unanswered for long enough,
therefore fine" — would hand the machine to whichever agent asked the most
frightening question at 2am and then waited. The same holds for a handoff: a
£99 payment nobody confirmed did not happen, and `expired` is deliberately not
one of `handoff.OUTCOMES`.

The sweep lives here rather than twice because two windows that drift apart is
how one of them quietly stops covering something — the same argument
`asking.is_handoff_subject` makes about the handoff patterns.
"""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

# Four hours.
#
# The window has to survive a normal gap in the owner's attention and no more. A
# question raised at the start of a meeting must still be there when he looks
# at his phone afterwards, so anything under an hour is useless — it would
# expire things he was always going to answer. But a question that has gone
# unanswered across half a working day is dead in practice: the agent that
# raised it has usually been closed, moved on, or asked something else since,
# so answering it now would apply a decision to a situation that no longer
# exists. Four hours is the longest window that is still shorter than "I forgot
# about this", which is the state the board exists to prevent.
#
# It is a constant and not a literal because both queues must move together;
# every function below takes `ttl` so a caller can narrow it, never widen it by
# accident.
DEFAULT_TTL: float = 4 * 60 * 60


def is_stale(ts: float, *, now: float, ttl: float = DEFAULT_TTL) -> bool:
    """Has `ts` fallen out of the window? PURE.

    A record from the future (a clock that jumped, a hand-edited file) is not
    stale: `now - ts` goes negative and stays under `ttl`. Sweeping it would be
    guessing, and the safe guess is to leave the question up.
    """
    return (float(now) - float(ts)) > float(ttl)


def sweep(
    rows: list[T],
    *,
    now: float,
    ttl: float = DEFAULT_TTL,
    ts_of: Callable[[T], float],
    is_open: Callable[[T], bool],
    mark: Callable[[T], T],
) -> tuple[list[T], list[T]]:
    """Mark every open, timed-out row expired. PURE — no I/O, no clock.

    Returns `(rows_after, changed)`. `changed` is what a caller reports; an
    empty list means there is nothing to tell anyone about, which is why
    sweeping twice must not report the same record twice.

    `is_open` is the guard that keeps this honest: a record that has already
    been answered or resolved is not open, so it is never re-marked. Losing an
    `always` rule's provenance, or turning a `done` handoff back into an
    unfinished one, would be a worse bug than never expiring anything.
    """
    after = list(rows)
    changed: list[T] = []
    for index, row in enumerate(after):
        if not is_open(row):
            continue
        if not is_stale(ts_of(row), now=now, ttl=ttl):
            continue
        after[index] = mark(row)
        changed.append(after[index])
    return after, changed
