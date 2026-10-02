"""A page that fails is retried on a backoff, never on every tick.

Found live on the Linux box, in the product, not in a test. Its ask ledger held
five unanswered questions and there is no WhatsApp bridge on that machine, so
`_page_the_owner` re-sent every one of them on every 1 Hz tick and failed every
time. **Measured: 285 send attempts in 60 seconds**, and the journal filling at
five lines a second.

The existing design is right and must survive this fix: `phone._page` marks a
key as sent ONLY on a clean exit, because marking it anyway would turn one dead
bridge into one permanently swallowed question -- and a swallowed question looks
exactly like one he chose to ignore. What was missing is the other half. "Not
yet delivered" was being retried as fast as the loop turns.

Today that only floods a log, because the bridge is absent. The moment the
bridge works, the same code path is his phone buzzing with the same question
once a second. That is the single most reliable way to make him stop trusting
the thing, so the retry policy is a product behaviour, not housekeeping.

Three good signals, all asserted as presences, because each one alone has a
degenerate version that passes the others:

  * attempts stay BOUNDED while the bridge is down -- or it floods;
  * the question is STILL retried once the backoff elapses -- or "bounded"
    was achieved by giving up, which is the swallowing the design forbids;
  * a NEW question is paged straight away -- or the backoff has been applied
    globally and an urgent question waits behind a stale one.

The sweep is the CLASS of failing sends: every non-zero exit `notify` can
report, not the one the box happened to produce.
"""

import itertools

import pytest

from server import asking, notify, phone


@pytest.fixture(autouse=True)
def _clean_memo():
    """`phone` memoises the paged ledger in a module global, so a test that
    did not clear it would be graded on the previous test's state."""
    phone._PAGED.clear()
    yield
    phone._PAGED.clear()


def _ask(ident: str = "3uz83") -> asking.Ask:
    # Raised long before any `now` below: these tests are about retries, so
    # the question must already be past the 30-minute urgent threshold.
    return asking.Ask(id=ident, ts=-10000.0, agent="install-verify", tool="Bash",
                      subject="/tmp/x", cwd="/tmp")


class _DeadBridge:
    """A sender that always refuses, counting how often it was asked.

    Exit code 3 is `wa-send.js` saying its daemon is down -- the exact code the
    box produces. `codes` lets a test sweep the other refusals.
    """

    def __init__(self, code: int = 3):
        self.calls = 0
        self.code = code

    def __call__(self, text, reply_to=None):
        self.calls += 1
        return notify.Sent(False, self.code, f"bridge refused ({self.code})")


def _tick(ask, bridge, ledger, now):
    """One collector tick's worth of paging, with time held still."""
    return phone.page_ask(ask, ledger=ledger, send=bridge, now=now)


def test_a_dead_bridge_is_not_retried_on_every_tick(tmp_path):
    """THE test. Sixty ticks -- one minute of the real loop -- must not be
    sixty attempts. The box did 285 in 60 seconds across five asks."""
    ledger = tmp_path / "paged.json"
    bridge = _DeadBridge()
    ask = _ask()

    for second in range(60):
        _tick(ask, bridge, ledger, now=1000.0 + second)

    assert bridge.calls <= 3, (
        f"the bridge was called {bridge.calls} times in 60 ticks; with a live "
        "bridge that is his phone buzzing once a second")
    assert bridge.calls >= 1, "it never even tried once"


def test_the_question_is_still_delivered_when_the_bridge_comes_back(tmp_path):
    """The half that stops 'bounded' from meaning 'given up'.

    A fix that simply marked the ask as handled would pass the test above and
    silently lose every question raised while the bridge was down -- which is
    the exact failure the once-only ledger was written to prevent.
    """
    ledger = tmp_path / "paged.json"
    bridge = _DeadBridge()
    ask = _ask()

    for second in range(60):
        _tick(ask, bridge, ledger, now=1000.0 + second)
    attempts_while_down = bridge.calls

    # An hour later, with the bridge answering.
    alive = []

    def good(text, reply_to=None):
        alive.append(text)
        return notify.Sent(True, 0, "sent")

    result = phone.page_ask(ask, ledger=ledger, send=good, now=1000.0 + 3600)

    assert result.ok, result.detail
    assert len(alive) == 1, (
        "the question was never delivered after the bridge recovered; "
        f"{attempts_while_down} attempts were made while it was down")
    assert "install-verify" in alive[0]


def test_a_delivered_question_is_never_sent_twice(tmp_path):
    """The original guarantee has to survive the change."""
    ledger = tmp_path / "paged.json"
    sent = []

    def good(text, reply_to=None):
        sent.append(text)
        return notify.Sent(True, 0, "sent")

    ask = _ask()
    for second in range(30):
        phone.page_ask(ask, ledger=ledger, send=good, now=2000.0 + second)

    assert len(sent) == 1, f"one question, {len(sent)} messages"


def test_a_new_question_does_not_wait_behind_a_stale_one(tmp_path):
    """The backoff is PER QUESTION. Applied globally, an urgent question would
    sit unsent behind an old one nobody can deliver -- which is worse than the
    flood it was meant to fix."""
    ledger = tmp_path / "paged.json"
    bridge = _DeadBridge()

    old = _ask("3uz83")
    for second in range(60):
        _tick(old, bridge, ledger, now=3000.0 + second)
    before = bridge.calls

    fresh = _ask("9r96h")
    _tick(fresh, bridge, ledger, now=3000.0 + 60)

    assert bridge.calls == before + 1, (
        "a brand-new question was not attempted immediately; it inherited the "
        "stale question's backoff")


@pytest.mark.parametrize("code", [1, 2, 3, 4, 5])
def test_every_refusal_backs_off_not_just_the_one_the_box_produced(tmp_path,
                                                                   code):
    """Sweep the class. `wa-send.js` has several non-zero exits and the box
    happened to show one of them; a fix keyed to that one would flood on the
    next."""
    ledger = tmp_path / f"paged-{code}.json"
    bridge = _DeadBridge(code=code)
    ask = _ask()

    for second in range(60):
        _tick(ask, bridge, ledger, now=4000.0 + second)

    assert bridge.calls <= 3, f"exit {code} floods: {bridge.calls} calls"


def test_the_backoff_grows_rather_than_repeating_at_a_fixed_rate(tmp_path):
    """A fixed 10-second retry is still 8,640 messages a day. The gap between
    attempts must widen, so a bridge that is down for a week costs a handful of
    attempts rather than a flood he has to mute."""
    ledger = tmp_path / "paged.json"
    bridge = _DeadBridge()
    ask = _ask()

    gaps, last_at, seen = [], None, 0
    for second in range(0, 7200):  # two hours of ticks
        _tick(ask, bridge, ledger, now=5000.0 + second)
        if bridge.calls > seen:
            seen = bridge.calls
            if last_at is not None:
                gaps.append(second - last_at)
            last_at = second

    assert len(gaps) >= 2, (
        f"only {len(gaps) + 1} attempts in two hours -- too few to show a "
        "growing gap")
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0], (
        f"the gap between retries never grew: {gaps}")
