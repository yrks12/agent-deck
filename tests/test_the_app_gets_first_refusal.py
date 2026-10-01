"""The app gets first refusal; the phone is the escalation, not the notice.

HIS WORDS: *"only if i have not confirmed for few min it should whatsapp me,
other ways on the app"*.

THE DEFECT. The decision to buzz his phone is taken at the instant the event
happens -- `phone.page_ask` sends the moment `_page_the_owner` sees a pending
ask, and `deskpage.Pager` sends the moment a desk transitions to NEEDS_YOU.
At that instant it is *impossible* to know whether he will answer, and he
usually will: the deck is open on the Mac in front of him and it is where the
Approve button lives. So his phone buzzes for things he is already looking at,
and a phone that buzzes for what he can already see is a phone he mutes.

The decision has to be taken AFTER a quiet period, from the item's LIVE state
-- is it *still* outstanding? -- and not from the fact that it once existed.

WHAT THESE PIN, and the split is the substance of the change:

  DEFERRED -- a thing he must ANSWER, and can answer on the app:
    * a pending ask (`phone.page_ask`)
    * a desk that has gone NEEDS_YOU (`deskpage` BLOCKED)
  Held for `notify.quiet_seconds()`. Withdrawn, never sent, if he settles it
  inside that window. Sent exactly once if he does not.

  IMMEDIATE -- a thing he is being TOLD, which no delay improves:
    * his own session finishing (`phone.page_done`)
    * a desk starting, speaking, or finishing (`deskpage` START/UPDATE/FINISH)
  Unchanged: these still leave on the tick they happen.

Every test here asserts a PRESENCE on both sides -- the held message really
does arrive when he ignores it, and the immediate ones really do go straight
out -- because "nothing was sent" on its own is also what a broken stub looks
like.

Hermetic. `send` is a recorder and `now` is injected: no bridge, no node, no
socket, no subprocess, and no message reaches a real phone at any point.
"""

import pytest

from server import asking, deskpage, notify, phone


@pytest.fixture(autouse=True)
def _no_bleed():
    """`phone._PAGED` mirrors the ledger in a module global."""
    phone._PAGED.clear()
    yield
    phone._PAGED.clear()


class Recorder:
    """Stands in for `notify.send`, and remembers what would have gone out."""

    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self._ok = ok

    def __call__(self, text, reply_to=None, **_):
        self.sent.append(text)
        return notify.Sent(self._ok, 0 if self._ok else 3, "recorded")


def card(name, state, session_id=None):
    return {"name": name, "state": state,
            "session_id": session_id or f"sid-{name}", "pid": 4242}


def pager(recorder, **kw):
    return deskpage.Pager(send=recorder, prefs=lambda: {}, **kw)


#: A fixed moment to hang every clock off, so no test reads the wall.
RAISED = 1_000_000.0


def an_ask(asks):
    """A question recorded at `RAISED`, so the window is entirely ours."""
    return asking.record(asks, agent="acme-growth", tool="Bash",
                         subject="gh pr create --fill",
                         cwd="/Users/y/Projects/acme", ts=RAISED)


def owner_tick(asks, ledger, send, now):
    """What `app._page_the_owner` does on one collector turn.

    Re-read from disk every time, exactly as production does -- that re-read is
    the whole mechanism. An ask he settled on the app is no longer `pending`,
    so the tick that would have escalated it never sees it.
    """
    for ask in asking.pending(asks):
        phone.page_ask(ask, ledger=ledger, send=send, now=now)


# ── the delay itself ────────────────────────────────────────────────────────


def test_the_quiet_window_is_minutes_and_he_can_change_it_without_a_deploy():
    assert notify.quiet_seconds({}) == notify.DEFAULT_QUIET_SECONDS
    # Thirty minutes: owner ruling 2026-09-30 made the phone urgent-only, and a
    # blocking ask becomes urgent after half an hour unanswered in the app.
    assert notify.DEFAULT_QUIET_SECONDS == 1800.0
    assert notify.quiet_seconds({notify.ENV_QUIET: "30"}) == 30.0
    # Nonsense must not silence him forever, nor buzz him instantly.
    assert (notify.quiet_seconds({notify.ENV_QUIET: "banana"})
            == notify.DEFAULT_QUIET_SECONDS)
    # Zero is a legitimate answer: "go back to buzzing me at once".
    assert notify.quiet_seconds({notify.ENV_QUIET: "0"}) == 0.0


# ── deferred: a question only he can answer ─────────────────────────────────


def test_a_question_he_answers_on_the_app_never_reaches_his_phone(tmp_path):
    """The point of the whole change. He taps Approve on the deck a minute in;
    his phone stays dark for that question, then and ever after."""
    asks, ledger = tmp_path / "asks.json", tmp_path / "paged.json"
    ask = an_ask(asks)
    send = Recorder()

    for second in range(0, 60):
        owner_tick(asks, ledger, send, now=RAISED + second)
    assert send.sent == [], (
        "his phone buzzed while he was sitting in front of the deck")

    asking.answer(asks, ask.id, "once")          # he tapped Approve on the app

    for second in range(60, 3600, 7):
        owner_tick(asks, ledger, send, now=RAISED + second)

    assert send.sent == [], (
        f"he answered it on the app and WhatsApp went out anyway: {send.sent}")


def test_a_question_he_ignores_does_reach_his_phone_once(tmp_path):
    """The other half, and the reason "nothing was sent" is not a pass on its
    own: left alone past the window the message really does go, it carries the
    id he has to quote back, and it goes exactly once."""
    asks, ledger = tmp_path / "asks.json", tmp_path / "paged.json"
    ask = an_ask(asks)
    send = Recorder()

    quiet = notify.quiet_seconds({})
    for second in range(0, int(quiet)):
        owner_tick(asks, ledger, send, now=RAISED + second)
    assert send.sent == [], f"it went early: {send.sent}"

    for second in range(int(quiet), int(quiet) + 3600, 3):
        owner_tick(asks, ledger, send, now=RAISED + second)

    assert len(send.sent) == 1, (
        f"one unanswered question, {len(send.sent)} messages")
    assert send.sent[0] == f"{phone.URGENT_MARK} {asking.compose(ask)}"
    assert f"(ask {ask.id})" in send.sent[0]


def test_answering_after_the_message_went_out_sends_nothing_further(tmp_path):
    asks, ledger = tmp_path / "asks.json", tmp_path / "paged.json"
    ask = an_ask(asks)
    send = Recorder()

    quiet = notify.quiet_seconds({})
    owner_tick(asks, ledger, send, now=RAISED + quiet + 1)
    assert len(send.sent) == 1, "the escalation never went out at all"

    asking.answer(asks, ask.id, "always")
    for second in range(0, 600, 5):
        owner_tick(asks, ledger, send, now=RAISED + quiet + 60 + second)

    assert len(send.sent) == 1, (
        f"a second message followed his answer: {send.sent}")


# ── deferred: a desk that needs him ─────────────────────────────────────────


def test_a_desk_he_unblocks_on_the_app_never_reaches_his_phone():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "NEEDS_YOU")], [], now=1.0)
    assert rec.sent == [], f"it buzzed the instant the desk blocked: {rec.sent}"

    # He answers it on the deck; the desk goes back to work well inside the
    # window, and the tick that would have escalated it sees WORKING.
    for second in range(40, 3600, 11):
        p.tick([card("Product", "WORKING")], [], now=float(second))

    assert not any("blocked" in m.lower() for m in rec.sent), (
        f"he unblocked it on the app and WhatsApp went out anyway: {rec.sent}")


def test_a_desk_still_blocked_past_the_window_does_reach_his_phone():
    rec = Recorder()
    p = pager(rec)
    quiet = notify.quiet_seconds({})
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "NEEDS_YOU")], [], now=1.0)

    for second in range(2, int(quiet)):
        p.tick([card("Product", "NEEDS_YOU")], [], now=float(second))
    assert rec.sent == [], f"it went early: {rec.sent}"

    for second in range(int(quiet), int(quiet) + 1800, 3):
        p.tick([card("Product", "NEEDS_YOU")], [], now=float(second))

    blocked = [m for m in rec.sent if "blocked" in m.lower()]
    assert len(blocked) == 1, f"one blocked desk, {len(blocked)} messages"
    assert "*Product*" in blocked[0]


# ── immediate: things he is being TOLD ──────────────────────────────────────


def test_his_own_session_finishing_still_reaches_him_at_once(tmp_path):
    """A report is not a question. Delaying it helps nobody."""
    ledger = tmp_path / "paged.json"
    send = Recorder()

    result = phone.page_done(card("manager", "DONE", "sid-manager"),
                             crown={"session_id": "sid-manager"},
                             ledger=ledger, send=send)

    assert result.ok, result.detail
    assert len(send.sent) == 1, send.sent
    assert "finished" in send.sent[0]


@pytest.mark.parametrize("seed, state, word", [("IDLE", "WORKING", "started"),
                                               ("WORKING", "DONE", "finished")])
def test_a_desk_starting_or_finishing_still_reaches_him_at_once(seed, state,
                                                                word):
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", seed)], [], now=0.0)
    p.tick([card("Product", state)], [], now=1.0)

    assert len(rec.sent) == 1, f"a {word} notice was delayed: {rec.sent}"
    assert word in rec.sent[0].lower()


def test_a_desks_own_words_still_reach_him_at_once():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Hamatsesa", "WORKING")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")],
           [{"to": "owner", "from": "Hamatsesa", "spoke": True,
             "text": "UX PASS on the format-reject chrome in #15."}],
           now=1.0)

    assert len(rec.sent) == 1, f"a milestone was delayed: {rec.sent}"
    assert "UX PASS on the format-reject chrome in #15." in rec.sent[0]
