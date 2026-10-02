"""When the deck may actually buzz him: ONE delivery policy for every channel.

THE COMPLAINT, verbatim: *"why do notifications go off so much? It's really
bothering me."* MEASURED on the box: 81 ntfy pushes in one hour -- 45 of them
the chief's plain replies, 14 a test desk.

The rule this file pins down (`server/push_policy.py`), with the GOOD signal
asserted each time -- the one push that should arrive, not only the noise
that should not:

1. A card that needs him is the only immediate push: one per card, after a
   short settle so an approval the auto-reviewer clears in two seconds never
   buzzes at all. Since 2026-10-01 rules 3-5 never hold a card back
   (`tests/test_needs_you_always_reaches_him.py`); they shape REPLIES.
2. A reply buzzes only when he wrote to that desk and then left: one push
   once he is away, and none if he read it first.
3. Coalesce: what arrives within two minutes of a push goes out as ONE
   summary ("3 agents need you").
4. At most six pushes an hour. Optional quiet hours.
5. Nothing while he is looking: a read, a message from him or an app in the
   foreground within the last two minutes holds everything.
"""

from server import push_policy as pp
from server.owner_alerts import FOR_YOU, NEEDS_YOU


def card(n, agent="cos", source="approval", t=0.0):
    return {"id": f"ask:{n}", "kind": NEEDS_YOU, "source": source,
            "agent": agent, "thread_id": f"direct:{agent}", "card_id": str(n),
            "title": f"{agent} needs you", "body": f"card {n}", "ts": t,
            "cursor": f"{int(t * 1e6):018d}-{n}", "urgent": False}


def reply(n, agent="cos", t=0.0):
    return {**card(n, agent, "message", t), "id": f"msg:{n}", "kind": FOR_YOU,
            "card_id": "", "title": agent, "body": f"reply {n}"}


def policy(**kw):
    return pp.PushPolicy(**{"window": 120, "cap": 6, "settle": 30,
                            "presence": 120, **kw})


def run(p, until, wanted=lambda a: True, start=0, step=1):
    out = []
    t = start
    while t <= until:
        out += p.tick(t, wanted)
        t += step
    return out


def test_a_card_that_needs_him_buzzes_once_after_the_settle():
    p = policy()
    p.offer(card(1, t=0), at=0)
    assert run(p, 29) == [], "an approval the reviewer clears in seconds must not buzz"
    pushes = run(p, 40, start=30)
    assert len(pushes) == 1
    assert pushes[0]["title"] == "cos needs you"
    assert pushes[0]["card_id"] == "1"
    assert pushes[0]["thread_id"] == "direct:cos"
    p.offer(card(1, t=0), at=50)  # the same card offered again
    assert run(p, 400, start=41) == []


def test_a_card_settled_before_the_settle_never_buzzes():
    p = policy()
    p.offer(card(1), at=0)
    assert run(p, 100, wanted=lambda a: False) == []


def test_more_replies_within_two_minutes_become_one_summary():
    p = policy()
    p.offer(reply(1, "cos"), at=0)
    first = run(p, 40)
    assert len(first) == 1
    p.offer(reply(2, "qa"), at=45)
    p.offer(reply(3, "eng"), at=60)
    p.offer(reply(4, "music"), at=70)
    later = run(p, 300, start=41)
    assert len(later) == 1, "three replies inside the window are one push"
    assert later[0]["title"] == "3 replies for you"
    assert later[0]["count"] == 3
    assert later[0]["body"] == "qa, eng and music"


def test_never_more_than_six_replies_an_hour():
    p = policy()
    for n in range(40):
        p.offer(reply(n, agent=f"d{n}", t=n * 150), at=n * 150)
    pushes = run(p, 3599, step=5)
    assert len(pushes) == 6
    # A freed slot lets the backlog out as ONE summary, not a flood, and the
    # next hour is held to six as well.
    after = run(p, 7199, start=3600, step=5)
    assert after[0]["count"] > 1
    assert len(after) <= 6


def test_no_reply_while_he_is_looking():
    p = policy()
    p.seen(at=0)
    p.offer(reply(1), at=10)
    assert run(p, 119) == []
    # He looked, then left without answering: it reaches him once.
    assert len(run(p, 200, start=120)) == 1


def test_a_reply_buzzes_only_once_he_has_left_and_not_if_he_read_it():
    p = policy()
    p.seen(at=0)  # he wrote to the desk from the app
    p.offer(reply(1), at=30)
    assert run(p, 119) == []
    pushes = run(p, 200, start=120)
    assert [x["title"] for x in pushes] == ["cos"]
    assert pushes[0]["kind"] == FOR_YOU

    q = policy()
    q.seen(at=0)
    q.offer(reply(2), at=30)
    read = {"done": False}
    q.seen(at=90)
    read["done"] = True  # the read cleared the thread
    assert run(q, 400, wanted=lambda a: not read["done"]) == []


def test_quiet_hours_hold_until_they_end():
    p = policy(quiet=lambda at: at < 1000)
    p.offer(card(1), at=0)
    assert run(p, 999, step=10) == []
    assert len(run(p, 1100, start=1000, step=10)) == 1


def test_quiet_hours_parse():
    import time
    inside = pp.quiet_window("22:00-07:00", clock=time.gmtime)
    assert inside(23 * 3600) and inside(3 * 3600)
    assert not inside(12 * 3600)
    assert pp.quiet_window("") is None
    assert pp.quiet_window("garbage") is None


def test_the_state_survives_a_restart(tmp_path):
    path = tmp_path / "pushes.json"
    p = policy(path=path)
    p.offer(card(1), at=0)
    p.offer(reply(1), at=0)
    assert len(run(p, 40)) == 2
    again = policy(path=path)
    again.offer(card(1), at=50)  # a restart re-reads the same pending card
    assert run(again, 400, start=41) == []
    for n in range(2, 8):
        again.offer(reply(n, agent=f"d{n}"), at=500 * n)
    assert len(run(again, 3600, start=401, step=5)) == 5, "the cap survives too"
