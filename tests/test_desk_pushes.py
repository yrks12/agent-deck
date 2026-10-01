"""Every desk reports to his phone -- and a muted one says nothing.

THE DEFECT. Measured by reading `server/phone.py` and `server/app.py` before
this slice: exactly two things buzz his phone. `page_ask`, when an agent is
blocked, and `page_done` -- which begins by throwing away every card that is
not the crowned session ("not the manager"). So seven desks can start a run,
hit three milestones each and finish, and his phone stays dark. The board has
all of it; he is not at the board. That is the whole gap between this product
and the one he showed us on his lock screen, where three named desks report
independently, several times an hour, including desks he never talks to.

WHAT THESE PIN, one behaviour at a time:

1. **All four transitions push.** Starting a job, saying something mid-run,
   going blocked, finishing. Not one of them, and not every tool call.
2. **Every desk, not the crowned one.** Two uncrowned desks both reach him.
3. **The desk's own words travel**, not a second summary of them -- the hire
   brief already teaches desks to put the answer in the first word, so
   re-writing their line here would throw away the only phone-shaped sentence
   in the system.
4. **The desk's name is on it**, bolded, because a line with no desk on it is
   unactionable when eight desks are running.
5. **A muted desk sends NOTHING** -- and, in the same tick, an unmuted one
   still does. The roster row has carried a `notifications` flag since the
   settings panel shipped; `server/api.py` saves it and nothing has ever read
   it. A toggle that does nothing is worse than no toggle.
6. **Volume is bounded by construction.** A chatty desk's lines coalesce into
   one message; a desk cannot send twice inside its own gap; and no two
   messages leave inside the global gap however many desks are running. The
   arithmetic that matters to him: the global gap alone caps the channel at
   `3600 / GLOBAL_GAP` messages an hour no matter how many desks exist.
7. **Nothing credential-shaped leaves.** `asking.redact` runs on the way out.
8. **A restart does not re-announce the world.** A desk first seen already
   WORKING is not a desk that just started.

Hermetic: no sockets, no bridge, no subprocess. `send` is a recorder and `now`
is injected, so the rate limiter is driven across an hour without sleeping.
"""

import pytest

from server import deskpage
from server import notify


class Recorder:
    """Stands in for `notify.send`. Records what would reach the phone."""

    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self.addresses: list = []
        self._ok = ok

    def __call__(self, text, reply_to=None):
        self.sent.append(text)
        self.addresses.append(reply_to)
        return notify.Sent(self._ok, 0 if self._ok else 3, "recorded")


def card(name, state, session_id=None):
    return {"name": name, "state": state,
            "session_id": session_id or f"sid-{name}", "pid": 4242}


def said(desk, text):
    """One record in the shape `harvest._say` writes down `office.send`."""
    return {"to": "owner", "from": desk, "text": text, "spoke": True}


def pager(recorder, prefs=None, **kw):
    return deskpage.Pager(send=recorder, prefs=lambda: (prefs or {}), **kw)


# -- 1. the four transitions -------------------------------------------------


def test_a_desk_starting_a_job_reaches_his_phone():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Hamatsesa", "IDLE")], [], now=0.0)          # seed, no beat
    p.tick([card("Hamatsesa", "WORKING")], [], now=1.0)

    assert len(rec.sent) == 1, rec.sent
    assert "*Hamatsesa*" in rec.sent[0]
    assert "started" in rec.sent[0].lower()


def test_a_milestone_carries_the_desks_own_words():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Hamatsesa", "WORKING")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")],
           [said("Hamatsesa", "UX PASS on the format-reject chrome in #15.")],
           now=1.0)

    assert len(rec.sent) == 1, rec.sent
    assert "UX PASS on the format-reject chrome in #15." in rec.sent[0]
    assert "*Hamatsesa*" in rec.sent[0]


def test_a_blocked_desk_reaches_his_phone():
    """It reaches him, but only after the app has had first refusal.

    BLOCKED is the one push that is a QUESTION rather than news, so it is held
    for the quiet window and dropped if he unblocks the desk at the deck
    inside it -- pinned in `tests/test_the_app_gets_first_refusal.py`. What
    this still owns is the other end: ignored, it really does arrive, with the
    desk's name on it.
    """
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "NEEDS_YOU")], [], now=1.0)
    p.tick([card("Product", "NEEDS_YOU")], [],
           now=1.0 + notify.quiet_seconds({}))

    assert len(rec.sent) == 1, rec.sent
    assert "*Product*" in rec.sent[0]
    assert "blocked" in rec.sent[0].lower()


def test_a_desk_finishing_reaches_his_phone():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "DONE")], [], now=1.0)

    assert len(rec.sent) == 1, rec.sent
    assert "*Product*" in rec.sent[0]
    assert "finished" in rec.sent[0].lower()


def test_a_finish_carries_the_last_thing_the_desk_said():
    """The prose and the DONE land on the same tick -- he gets ONE message
    carrying the desk's actual report, not 'Product finished' with the report
    thrown away."""
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "DONE")],
           [said("Product", "Bake ETA ~14:10 IDT for the #15 candidate.")],
           now=1.0)

    assert len(rec.sent) == 1, rec.sent
    assert "Bake ETA ~14:10 IDT for the #15 candidate." in rec.sent[0]
    assert "finished" in rec.sent[0].lower()


def test_a_tool_call_is_not_a_transition():
    """Only the four. A desk that stays WORKING across ticks with nothing to
    say has nothing to report, and the good signal is that the ONE message he
    was owed is still the only one."""
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Hamatsesa", "IDLE")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")], [], now=1.0)
    for i in range(2, 40):
        p.tick([card("Hamatsesa", "WORKING")], [], now=float(i))

    assert len(rec.sent) == 1, rec.sent


# -- 2. every desk, not the crowned one --------------------------------------


def test_two_uncrowned_desks_both_report():
    """The defect in one test. Neither of these is the session he types to."""
    rec = Recorder()
    p = pager(rec, global_gap=0.0)
    p.tick([card("Hamatsesa", "IDLE"), card("Product", "IDLE")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING"), card("Product", "WORKING")],
           [], now=1.0)

    joined = "\n".join(rec.sent)
    assert "*Hamatsesa*" in joined, rec.sent
    assert "*Product*" in joined, rec.sent


def test_every_desk_gets_its_own_name_not_a_shared_one():
    rec = Recorder()
    p = pager(rec, global_gap=0.0)
    p.tick([card("COS", "WORKING")], [], now=0.0)
    p.tick([card("COS", "WORKING")], [said("COS", "Bake in ~4 minutes.")],
           now=1.0)

    assert rec.sent[0].startswith("*COS*"), rec.sent


# -- 3. mute is real ---------------------------------------------------------


def test_a_muted_desk_sends_nothing_while_its_neighbour_still_does():
    """Both halves in one test on purpose: the silence only means something
    next to a message that DID go out on the same tick."""
    rec = Recorder()
    prefs = {"Hamatsesa": {"notifications": False},
             "Product": {"notifications": True}}
    p = pager(rec, prefs=prefs, global_gap=0.0)
    p.tick([card("Hamatsesa", "IDLE"), card("Product", "IDLE")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING"), card("Product", "WORKING")],
           [said("Hamatsesa", "starting the #15 walk"),
            said("Product", "opening the bake")], now=1.0)

    joined = "\n".join(rec.sent)
    assert "*Product*" in joined, rec.sent
    assert "Hamatsesa" not in joined, rec.sent
    assert "starting the #15 walk" not in joined, rec.sent


@pytest.mark.parametrize("state", ["WORKING", "NEEDS_YOU", "DONE"])
def test_a_muted_desk_is_silent_on_every_transition(state):
    """Sweep the class: mute must hold for start, blocked and finish alike,
    not just for the one an implementer happened to route through it."""
    rec = Recorder()
    prefs = {"Quiet": {"notifications": False}}
    p = pager(rec, prefs=prefs, global_gap=0.0)
    p.tick([card("Quiet", "WORKING")], [], now=0.0)
    p.tick([card("Quiet", state)], [said("Quiet", "anything at all")], now=1.0)

    assert rec.sent == [], rec.sent


def test_notifications_absent_means_on():
    """A desk with no pref row has never been muted. Defaulting to silence
    would ship a product that says nothing until he finds the toggle."""
    rec = Recorder()
    p = pager(rec, prefs={"Product": {"pinned": True}})
    p.tick([card("Product", "IDLE")], [], now=0.0)
    p.tick([card("Product", "WORKING")], [], now=1.0)

    assert len(rec.sent) == 1, rec.sent


# -- 4. volume ---------------------------------------------------------------


def test_three_lines_from_one_desk_coalesce_into_one_message():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Hamatsesa", "WORKING")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")],
           [said("Hamatsesa", "one"), said("Hamatsesa", "two"),
            said("Hamatsesa", "three")], now=1.0)

    assert len(rec.sent) == 1, rec.sent
    for word in ("one", "two", "three"):
        assert word in rec.sent[0], rec.sent[0]


def test_a_desk_cannot_send_twice_inside_its_own_gap():
    rec = Recorder()
    p = pager(rec, desk_gap=300.0, global_gap=0.0)
    p.tick([card("Hamatsesa", "WORKING")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")], [said("Hamatsesa", "first")],
           now=1.0)
    p.tick([card("Hamatsesa", "WORKING")], [said("Hamatsesa", "second")],
           now=100.0)

    assert len(rec.sent) == 1, rec.sent
    assert "first" in rec.sent[0]

    # Owed, not dropped: it goes out the moment the gap opens.
    p.tick([card("Hamatsesa", "WORKING")], [], now=400.0)
    assert len(rec.sent) == 2, rec.sent
    assert "second" in rec.sent[1]


def test_the_global_gap_holds_however_many_desks_are_running():
    """Eight desks all say something at once. He gets one message, then the
    channel is shut until the gap opens -- which is the guarantee that makes
    eight desks survivable at all."""
    rec = Recorder()
    p = pager(rec, global_gap=120.0)
    desks = [f"desk{i}" for i in range(8)]
    p.tick([card(d, "WORKING") for d in desks], [], now=0.0)
    p.tick([card(d, "WORKING") for d in desks],
           [said(d, f"{d} says something") for d in desks], now=1.0)

    assert len(rec.sent) == 1, rec.sent

    p.tick([card(d, "WORKING") for d in desks], [], now=60.0)
    assert len(rec.sent) == 1, rec.sent

    p.tick([card(d, "WORKING") for d in desks], [], now=125.0)
    assert len(rec.sent) == 2, rec.sent


def test_the_desk_that_waited_longest_goes_next():
    """Fairness, or one loud desk starves the other seven and he never hears
    from the quiet one that was actually blocked."""
    rec = Recorder()
    p = pager(rec, global_gap=120.0)
    p.tick([card("early", "WORKING"), card("late", "WORKING")], [], now=0.0)
    p.tick([card("early", "WORKING")], [said("early", "queued first")], now=1.0)
    p.tick([card("late", "WORKING")], [said("late", "queued second")], now=2.0)

    assert len(rec.sent) == 1 and "queued first" in rec.sent[0], rec.sent
    p.tick([card("late", "WORKING")], [], now=200.0)
    assert len(rec.sent) == 2 and "queued second" in rec.sent[1], rec.sent


def test_an_hour_of_eight_busy_desks_is_bounded():
    """The number he actually cares about. Eight desks each saying something
    every ten seconds for an hour must not produce 2,880 messages."""
    rec = Recorder()
    p = pager(rec, global_gap=120.0, desk_gap=300.0)
    desks = [f"desk{i}" for i in range(8)]
    p.tick([card(d, "WORKING") for d in desks], [], now=0.0)
    for t in range(1, 3600, 10):
        p.tick([card(d, "WORKING") for d in desks],
               [said(d, f"{d} line at {t}") for d in desks], now=float(t))

    assert len(rec.sent) <= 30, f"{len(rec.sent)} messages in an hour"
    assert len(rec.sent) >= 10, f"only {len(rec.sent)} in an hour -- too quiet"


def test_a_failed_send_does_not_burn_the_message():
    """The bridge being down must not eat what the desk said."""
    rec = Recorder(ok=False)
    p = pager(rec, global_gap=0.0)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "WORKING")], [said("Product", "the bake is up")],
           now=1.0)
    assert len(rec.sent) == 1

    rec._ok = True
    p.tick([card("Product", "WORKING")], [], now=400.0)
    assert len(rec.sent) == 2, rec.sent
    assert "the bake is up" in rec.sent[1]


# -- 5. shape and safety -----------------------------------------------------


def test_a_token_in_a_desks_line_never_reaches_the_phone():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "WORKING")],
           [said("Product",
                 "deployed with token=sk-ant-api03-QQhh12mnbb99zzXX")],
           now=1.0)

    assert len(rec.sent) == 1
    assert "sk-ant-api03-QQhh12mnbb99zzXX" not in rec.sent[0], rec.sent[0]
    assert "[redacted]" in rec.sent[0], rec.sent[0]


def test_the_message_is_phone_shaped():
    rec = Recorder()
    p = pager(rec)
    p.tick([card("Product", "WORKING")], [], now=0.0)
    p.tick([card("Product", "WORKING")],
           [said("Product", "x" * 4000)], now=1.0)

    body = rec.sent[0]
    assert len(body.splitlines()) <= 8, body
    assert body.startswith("*Product*"), body


def test_a_desk_first_seen_working_did_not_just_start():
    """The deck restarts. Eight desks are mid-run. He must not get eight
    'started' messages for work that began an hour ago."""
    rec = Recorder()
    p = pager(rec, global_gap=0.0)
    p.tick([card(f"d{i}", "WORKING") for i in range(8)], [], now=0.0)
    assert rec.sent == [], rec.sent


def test_a_session_with_no_desk_speaks_for_nobody():
    """A card with no name is a terminal, not a desk. Same rule
    `harvest._apply` already holds to."""
    rec = Recorder()
    p = pager(rec, global_gap=0.0)
    p.tick([{"state": "IDLE", "session_id": "x"}], [], now=0.0)
    p.tick([{"state": "WORKING", "session_id": "x"}], [], now=1.0)
    assert rec.sent == [], rec.sent


def test_each_push_carries_that_desks_return_address():
    """His reply has to go back to the desk that spoke, not to the last one --
    the bridge records the return address against the message id."""
    rec = Recorder()
    p = pager(rec, address=lambda desk: {"socket": f"/tmp/{desk}.sock",
                                         "session_id": f"sid-{desk}"})
    p.tick([card("Hamatsesa", "IDLE")], [], now=0.0)
    p.tick([card("Hamatsesa", "WORKING")], [], now=1.0)

    assert rec.addresses[0] == {"socket": "/tmp/Hamatsesa.sock",
                                "session_id": "sid-Hamatsesa"}


# -- 6. the daemon calls it --------------------------------------------------


def test_the_collector_tick_no_longer_pages_the_desks():
    """OWNER RULING 2026-09-30: a desk's news is app-only now -- *"we have
    clear communication in the app -- only in urgent cases should he send
    me"*. The pager in `server/deskpage.py` is kept and still correct, but the
    daemon no longer drives it. What does reach WhatsApp is pinned in
    `tests/test_whatsapp_is_urgent_only.py`.

    Read STATICALLY: `_page_the_owner` reads the real ledgers off `~/.claude`.
    """
    import inspect

    from server import app as app_mod

    tick = inspect.getsource(app_mod._page_the_owner)
    assert "_page_the_desks" not in tick
    assert "_desk_pager" not in tick
    assert not hasattr(app_mod, "_desk_pager")
