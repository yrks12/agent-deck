"""Every desk reports to his phone, at a volume he will not mute.

WHY THIS EXISTS. `server/phone.py` pages him for exactly two things: a pending
ask, and the CROWNED session going DONE -- `page_done` opens by discarding
every card that is not his own session ("not the manager"). That was the right
call when the deck ran one session he talked to. It is the wrong product now:
he showed us a lock screen where three named desks report independently
several times an hour, including desks he never types to. The board has all of
this; he is not at the board.

WHAT PUSHES, and only this: a desk STARTS a job, SAYS something mid-run, goes
BLOCKED, or FINISHES. Not a tool call, not a state flicker.

WHERE THE FOUR COME FROM. Three are state transitions on the collector's own
cards, which is deliberate: reading the state map sweeps every desk and every
door at once, where instrumenting call sites sweeps the ones that exist today
and silently misses the next one. `server/app.py::_page_the_owner` already
makes this argument for asks; this is the same argument for desks.

The fourth -- the milestone -- is the desk's own turn-final prose, taken off
the queue `harvest._say` already writes to. That matters: the hire brief
teaches desks to put the answer in the first word, so their line is already
the phone-shaped sentence. Re-summarising it here would throw away the only
good sentence in the system and add a second place for a credential to escape.

AGENT-TO-AGENT TRAFFIC DOES NOT PUSH, and that is a decision, not an omission.
His screenshot has "Re-sent Initech the walkable URL" -- but read who said
it: that is the PRODUCT desk's own report of what it did, not the deck
mirroring a raw SendMessage. Desks talk constantly; a machine that forwarded
every edge would be a transcript, not a briefing. So the traffic stays in the
app, where `server/office.py` already indexes it, and reaches his phone only
when a desk chooses to tell him about it -- which is precisely the line in the
screenshot.

VOLUME IS THE DESIGN RISK, so it is handled by construction and not by hoping
desks are tasteful. Three mechanisms, in this order:

  1. COALESCE. Everything one desk has to say while it waits its turn becomes
     ONE message, newest line first, up to `COALESCE_MAX` lines. A desk that
     speaks ten times costs one message, not ten.
  2. A PER-DESK GAP. No desk sends twice inside `DESK_GAP`. Nothing is
     dropped -- it waits and rides the next message out.
  3. A GLOBAL GAP. No two messages leave inside `GLOBAL_GAP`, however many
     desks are running, and the desk that has waited LONGEST goes next so one
     loud desk cannot starve the other seven.

The arithmetic he should hold us to: the global gap alone caps the channel at
`3600 / GLOBAL_GAP` = 30 messages an hour with eight desks or with eighty. The
per-desk gap caps any single desk at 12 an hour. His own screenshot ran about
26 an hour across three desks, so this lands on the cadence he already likes
and cannot go above it. A test drives eight desks speaking every ten seconds
for a simulated hour and asserts the ceiling holds.

ONE OF THE FOUR IS A QUESTION, and it is the only one that waits. START,
UPDATE and FINISH are news: he is being TOLD something, there is nothing to
answer, and a delay makes the product slower at the job he asked it to do.
BLOCKED is the desk asking HIM for something, and the deck -- open on the Mac
in front of him -- is where that gets answered. So BLOCKED is held for
`notify.quiet_seconds()` and, crucially, WITHDRAWN if the desk has left
NEEDS_YOU by the time the window closes. Held-then-sent-anyway would just be
a late buzz about a question he already dealt with; see `_withdraw_answered`.

MUTE IS REAL HERE. The roster row has carried a `notifications` flag since the
settings panel shipped and `server/api.py` has been saving it into
`agent_prefs.json` with nothing on the machine reading it. A muted desk is
dropped at OFFER time, before it is ever buffered -- so unmuting releases the
next thing that desk says, not an hour of backlog it never agreed to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import asking, notify

#: A run is under way / the desk spoke / it needs him / it is done.
START, UPDATE, BLOCKED, FINISH = "start", "update", "blocked", "finish"

#: Loudest kind in a coalesced group wins the headline. A desk that spoke and
#: then finished on the same tick has FINISHED; that is the word he acts on.
_RANK = {UPDATE: 0, START: 1, BLOCKED: 2, FINISH: 3}

_HEADLINE = {
    START: "started",
    UPDATE: "update",
    BLOCKED: "is blocked — needs you",
    FINISH: "finished",
}

#: Said when a transition arrives with no prose attached to it.
_BARE = {
    START: "picked up a job.",
    UPDATE: "",
    BLOCKED: "waiting on you before it can go on.",
    FINISH: "finished and is waiting on you.",
}

#: See the module docstring for the arithmetic these two produce.
DESK_GAP = 300.0
GLOBAL_GAP = 120.0

#: Lines carried in one coalesced message. Three plus the headline is four
#: lines, which is inside the eight his lock screen shows before it truncates.
COALESCE_MAX = 3

#: One line of a desk's prose, on a phone. Past this it stops being readable
#: and starts being a wall he swipes away.
LINE_MAX = 160


#: The receipt path's allowance. Five in a burst is wider than any human
#: exchange -- five replies inside a minute is faster than he can type -- and
#: the refill means it can never latch shut.
RECEIPT_BURST = 5
RECEIPT_REFILL = 60.0


class Ceiling:
    """A burst allowance that refills. For a path with no natural gap.

    `Pager` bounds the desks because a desk's news can wait. Receipts and
    refusals cannot: he has just typed something and is waiting to learn where
    it went, so delaying one by two minutes would read as the product ignoring
    him. What they need is not a gap but a CAP -- unlimited at human pace,
    bounded when something starts looping.

    It REFILLS, and that is the half worth stating: a limiter that latches shut
    means he answers a desk once and is never told again whether anything
    landed, which is a worse product than an occasional burst.
    """

    def __init__(self, burst: int = RECEIPT_BURST,
                 refill: float = RECEIPT_REFILL) -> None:
        self._burst = int(burst)
        self._refill = float(refill)
        self._spent: list[float] = []

    def allow(self, now=None) -> bool:
        """True when one more message may go out, and spends the allowance."""
        moment = time.time() if now is None else float(now)
        self._spent = [t for t in self._spent if moment - t < self._refill]
        if len(self._spent) >= self._burst:
            return False
        self._spent.append(moment)
        return True


@dataclass(frozen=True)
class Beat:
    """One thing that happened at one desk, worth one line on his phone."""
    desk: str
    kind: str
    text: str


@dataclass
class _Pending:
    """What a desk has waiting, and since when."""
    kind: str = UPDATE
    lines: list[str] = field(default_factory=list)
    since: float = 0.0
    #: When this desk went NEEDS_YOU, for the quiet window. None means it is
    #: not currently holding a "needs you" -- everything else here is news he
    #: is being told rather than a question he owes an answer to.
    blocked_at: float | None = None

    def add(self, beat: Beat, now: float) -> None:
        if not self.lines:
            self.since = now
        if beat.kind == BLOCKED and self.blocked_at is None:
            self.blocked_at = now
        if _RANK[beat.kind] >= _RANK[self.kind]:
            self.kind = beat.kind
        if beat.text and beat.text not in self.lines:
            self.lines.append(beat.text)
        # Newest wins when the buffer is full. An old milestone is stale by
        # the time the gap opens; the thing that just happened is not.
        if len(self.lines) > COALESCE_MAX:
            del self.lines[0]


def is_muted(prefs: dict, desk: str) -> bool:
    """PURE. Absent means ON -- a desk with no pref row was never muted, and
    defaulting to silence would ship a product that says nothing at all until
    he goes looking for the toggle."""
    row = (prefs or {}).get(desk)
    if not isinstance(row, dict):
        return False
    return not bool(row.get("notifications", True))


def beats(before: dict, sessions: list, said: list) -> tuple[list, dict]:
    """Every reportable thing since the last tick, and the new state map. PURE.

    `before` maps desk -> last observed state. A desk NOT in it is being seen
    for the first time: it is recorded and emits nothing. That is the restart
    case -- eight desks mid-run must not arrive as eight fresh starts for work
    that began an hour ago.

    A card with no `name` sits at no desk and speaks for nobody, which is the
    same rule `harvest._apply` already holds to.
    """
    now_states = dict(before or {})
    out: list[Beat] = []

    prose: dict[str, list[str]] = {}
    for record in said or []:
        who = str((record or {}).get("from") or "")
        body = str((record or {}).get("text") or "").strip()
        if who and body:
            prose.setdefault(who, []).append(body)

    spoke_and_moved: set[str] = set()
    for card in sessions or []:
        desk = str((card or {}).get("name") or "")
        state = str((card or {}).get("state") or "")
        if not desk or not state:
            continue
        was = now_states.get(desk)
        now_states[desk] = state
        if was is None or was == state:
            continue
        kind = None
        if state == "WORKING":
            kind = START
        elif state == "NEEDS_YOU":
            kind = BLOCKED
        elif state == "DONE" and was == "WORKING":
            kind = FINISH
        if kind is None:
            continue
        # The transition and the prose are the same event seen twice: a desk
        # finishes by writing its report, so the collector's DONE and
        # `harvest._say` land on one tick. Carry the words and drop the empty
        # sentence, or he gets "Product finished" with the report thrown away.
        lines = prose.get(desk) or []
        if lines:
            spoke_and_moved.add(desk)
            for line in lines:
                out.append(Beat(desk, kind, line))
        else:
            out.append(Beat(desk, kind, _BARE[kind]))

    for desk, lines in prose.items():
        if desk in spoke_and_moved:
            continue
        for line in lines:
            out.append(Beat(desk, UPDATE, line))

    return out, now_states


def compose(desk: str, kind: str, lines: list) -> str:
    """The message that lands on his lock screen. PURE.

    Bold desk name and the verb on line one, because with eight desks running
    a line with no name on it is unactionable. Redacted -- everything here is
    an agent's free prose and an agent can print anything.
    """
    head = f"*{asking.redact(desk)}* — {_HEADLINE.get(kind, 'update')}"
    body = []
    for raw in list(lines)[-COALESCE_MAX:][::-1]:   # newest first
        text = asking.redact(str(raw)).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > LINE_MAX:
            text = text[: LINE_MAX - 1].rstrip() + "…"
        body.append(text)
    return "\n".join([head] + body)


class Pager:
    """Buffers what desks say and lets it out at a rate he will tolerate.

    Nothing here knows a phone number or a token: `send` is `notify.send`,
    which owns the subprocess, and the bridge owns the credentials.

    `now` is injected rather than read so an hour of eight busy desks can be
    driven in a test without sleeping through it.
    """

    def __init__(self, *, send=None, prefs=None, address=None,
                 desk_gap: float = DESK_GAP,
                 global_gap: float = GLOBAL_GAP,
                 quiet: float | None = None) -> None:
        # Resolved here, not as a default argument: `send=notify.send` in a
        # signature binds the function object at import time and can never
        # afterwards be replaced. Same reasoning as `phone._page`.
        self._send = notify.send if send is None else send
        self._prefs = prefs or (lambda: {})
        self._address = address or (lambda desk: None)
        self._desk_gap = float(desk_gap)
        self._global_gap = float(global_gap)
        # None means "read `notify.quiet_seconds()` at flush time", so the env
        # override lands on the next tick rather than the next restart.
        self._quiet = None if quiet is None else float(quiet)
        self._states: dict = {}
        self._pending: dict = {}
        self._last_desk: dict = {}
        self._last_any: float | None = None

    def offer(self, new_beats: list, now: float) -> None:
        """Buffer, coalescing per desk. Muted desks are dropped HERE.

        Dropped at the door rather than at send time so that unmuting a desk
        releases the next thing it says, not an hour of backlog he never
        agreed to receive.
        """
        prefs = self._prefs() or {}
        for beat in new_beats or []:
            if is_muted(prefs, beat.desk):
                continue
            slot = self._pending.get(beat.desk)
            if slot is None:
                slot = _Pending()
                self._pending[beat.desk] = slot
            slot.add(beat, now)

    def flush(self, now: float) -> list:
        """Send everything the gaps currently permit, and not one more.

        A loop rather than a single send, because the gaps -- not the tick --
        must be what decides the rate. With a real `GLOBAL_GAP` this sends
        exactly one message and stops on the next pass, since the send it just
        made moves `_last_any` to `now`; with the gap dialled to zero it drains,
        which is what a caller asking for no throttle asked for.

        A failed send is not a delivery: the buffer is left exactly as it was,
        nothing advances, and the loop stops so a dead bridge is attempted once
        per tick rather than once per pending desk. `notify.send` already
        refuses to claim sent when the bridge is down, and swallowing that here
        would undo it at the last step.
        """
        results: list = []
        self._withdraw_answered(now)
        while self._pending:
            if (self._last_any is not None
                    and now - self._last_any < self._global_gap):
                break
            ready = [(slot.since, desk) for desk, slot in self._pending.items()
                     if slot.lines and self._desk_due(desk, slot.kind, now)]
            if not ready:
                break
            # Longest-waiting first, so one loud desk cannot starve the other
            # seven and the quiet desk that went blocked is still heard.
            ready.sort()
            desk = ready[0][1]
            slot = self._pending[desk]

            result = self._send(compose(desk, slot.kind, slot.lines),
                                reply_to=self._address(desk))
            results.append(result)
            if not getattr(result, "ok", False):
                break
            self._pending.pop(desk, None)
            self._last_desk[desk] = now
            self._last_any = now
        return results

    def _withdraw_answered(self, now: float) -> None:
        """Drop every held "needs you" the OWNER has already dealt with.

        THE APP GETS FIRST REFUSAL. His words: *"only if i have not confirmed
        for few min it should whatsapp me, other ways on the app."* A blocked
        desk is on the board the instant it blocks -- unchanged, and that is
        the primary surface -- but nothing leaves for his phone until the desk
        has been waiting `notify.quiet_seconds()`.

        The withdrawal is what makes that a real behaviour rather than a delay.
        It reads `self._states`, which `beats()` has just refreshed from the
        live cards, so a desk that is no longer NEEDS_YOU is a desk he
        unblocked at the deck -- and the message that was queued for it is
        dropped, never sent, rather than arriving to tell him about a question
        he answered two minutes ago.

        The whole slot goes, not just the blocked line. A desk that blocked and
        was released inside the window has, on balance, nothing to say: the
        alternative is telling him "Product started" about a desk that only
        started because he was standing over it.

        A desk whose state is UNKNOWN here is never withdrawn. That is the
        conservative half: an absent card cannot prove he answered anything,
        and dropping on it would swallow a real "needs you".
        """
        for desk, slot in list(self._pending.items()):
            if slot.blocked_at is None:
                continue
            state = str(self._states.get(desk) or "")
            if state and state != "NEEDS_YOU":
                self._pending.pop(desk, None)

    def _desk_due(self, desk: str, kind: str, now: float) -> bool:
        """FINISH ignores the per-desk gap -- it is the thing he acts on, and
        making him wait five minutes for "this is done" would be the rate
        limiter working against the only thing it is protecting. It still
        obeys the global gap, so the ceiling is unchanged.

        BLOCKED is the one kind that waits, and it waits on the QUIET WINDOW
        rather than on the per-desk gap: it is the only thing a desk pushes
        that is a question to him rather than news for him, and the deck is
        where the answer button is. Once the window has passed it too skips
        the per-desk gap -- by then it has already waited longer than that gap
        would have made it.
        """
        slot = self._pending.get(desk)
        if kind == BLOCKED:
            raised = getattr(slot, "blocked_at", None)
            if raised is None:
                return True
            window = (notify.quiet_seconds() if self._quiet is None
                      else self._quiet)
            return now - raised >= window
        if kind == FINISH:
            return True
        last = self._last_desk.get(desk)
        return last is None or now - last >= self._desk_gap

    def tick(self, sessions: list, said: list, *, now=None) -> list:
        """One collector tick: read the transitions, buffer, let one out."""
        moment = time.time() if now is None else float(now)
        new_beats, self._states = beats(self._states, sessions, said)
        self.offer(new_beats, moment)
        return self.flush(moment)
