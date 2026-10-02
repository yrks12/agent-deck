"""A card that needs his decision or his hands ALWAYS reaches him: fast, once.

THE COMPLAINT, verbatim: *"Why do I not get notifications when they need me?
Like taking over the screen, and things they need me to decide."*

MEASURED on the box, 2026-10-01 (UTC), every needs-you card of the day joined
against `pushes.json` and the journal:

* 118 cards (42 decisions, 50 approvals, 26 takeover/sign-in handoffs) reached
  the notifier -- none was lost on the way in.
* Only 14 were pushed. 51 cleared themselves inside the settle, which is right.
  The other 53 were held and never pushed: 46 because he was "looking" (any
  app in front anywhere, any read, any message he typed holds EVERY card), 2
  inside the two-minute coalesce window, and 5 because the hold lived in
  memory and the deck restarted 54 times that day.
* The pushes that did go out were mostly summaries ("1 agent needs you · 4
  replies for you") whose tap opened no card at all.

The rule pinned here (`server/push_policy.py`): the cap, the coalesce window
and "he is looking" are for REPLIES. A card that needs him is pushed on its
own, with its own card link, after a short settle, whatever else is going on;
it never spends a reply's slot; and a held card survives a restart.
"""

import re
from pathlib import Path

from server import ntfy, owner_alerts
from server import push_policy as pp
from server.owner_alerts import FOR_YOU, NEEDS_YOU


def card(n, agent="cos", source="decision", t=0.0):
    return {"id": f"{source}:{n}", "kind": NEEDS_YOU, "source": source,
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


# -- the policy ---------------------------------------------------------------

def test_a_card_reaches_him_while_he_is_looking():
    p = policy()
    p.seen(at=0)  # in the app, chatting to another desk
    p.offer(card(1), at=0)
    pushes = run(p, 60)
    assert [x["card_id"] for x in pushes] == ["1"], \
        "a decision card was held because he had an app open elsewhere"


def test_a_decision_or_a_takeover_is_fast_and_an_approval_waits_the_reviewer():
    p = policy()
    p.offer(card(1, source="decision"), at=0)
    p.offer(card(2, source="handoff"), at=0)
    p.offer(card(3, source="approval"), at=0)
    fast = run(p, 15)
    assert sorted(x["card_id"] for x in fast) == ["1", "2"]
    assert [x["card_id"] for x in run(p, 40, start=16)] == ["3"]


def test_cards_are_never_capped_or_coalesced():
    p = policy()
    for n in range(20):
        p.offer(card(n, agent=f"d{n}"), at=n * 10)
    pushes = run(p, 400)
    assert sorted(int(x["card_id"]) for x in pushes) == list(range(20)), \
        "every card is its own push: none capped away, none folded in"
    assert all(x["count"] == 1 for x in pushes)
    assert len({x["id"] for x in pushes}) == 20
    assert len({x["cursor"] for x in pushes}) == 20


def test_a_card_is_never_folded_into_a_reply_summary():
    p = policy()
    p.offer(reply(1), at=0)
    p.offer(reply(2, agent="eng"), at=0)
    p.offer(card(9, agent="growth", source="handoff"), at=5)
    pushes = run(p, 60)
    cards = [x for x in pushes if x["kind"] == NEEDS_YOU]
    assert len(cards) == 1
    assert cards[0]["card_id"] == "9"
    assert cards[0]["thread_id"] == "direct:growth"
    assert cards[0]["count"] == 1
    assert "click" not in cards[0] or "card=9" in cards[0]["click"]
    assert "card=9" in ntfy.click_link(cards[0])


def test_cards_do_not_spend_the_reply_cap():
    p = policy()
    for n in range(10):
        p.offer(card(n, agent=f"d{n}"), at=n * 20)
    assert len(run(p, 300)) == 10
    p.offer(reply(1), at=301)
    assert [x["kind"] for x in run(p, 400, start=301)] == [FOR_YOU]




def test_a_held_card_survives_a_restart(tmp_path):
    path = tmp_path / "pushes.json"
    p = policy(path=path)
    p.offer(card(1), at=0)
    assert run(p, 3) == []
    again = policy(path=path)  # the deck restarted inside the settle
    pushes = run(again, 60, start=4)
    assert [x["card_id"] for x in pushes] == ["1"]
    third = policy(path=path)
    assert run(third, 200, start=61) == [], "pushed once, not once per restart"




# -- every card type a desk can raise ----------------------------------------

def test_every_needs_you_source_the_deck_emits_has_a_mapping():
    """A new kind of card that needs him must be named here, or this fails."""
    api = (Path(__file__).parents[1] / "server" / "api.py").read_text()
    live = set(re.findall(r'self\._live\("([a-z_]+)"', api))
    assert live, "the live-card builder moved: point this test at it"
    literal = set(re.findall(r'"source": "([a-z_]+)"', api))
    assert "decision" in literal, "the logged-card builder moved"
    emitted = live | literal
    assert emitted <= set(owner_alerts.NEEDS_YOU_SOURCES), (
        f"needs-you source(s) with no notification mapping: "
        f"{sorted(emitted - set(owner_alerts.NEEDS_YOU_SOURCES))}")
    for source in owner_alerts.NEEDS_YOU_SOURCES:
        alert = card(1, source=source)
        assert pp.blocking(alert), source
        assert ntfy.payload(alert, "t")["priority"] >= 4, source
    # Fail safe: a needs-you item from a source nobody mapped still buzzes.
    assert pp.blocking(card(1, source="something-new"))
