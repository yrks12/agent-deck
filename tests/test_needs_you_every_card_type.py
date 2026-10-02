"""Every card a desk can raise that needs him reaches the page every channel
reads (`GET /v1/owner/alerts`): each alone, with its card link, while he
is in the app, and even when it was raised while the deck was restarting.

MEASURED 2026-10-01: 54 deck restarts in a day; 5 cards that were mid-settle
or raised during one never reached him. The policy half of this is
`tests/test_needs_you_always_reaches_him.py`.
"""

from server import asking, decisions, handoff, ntfy
from server import push_policy as pp
from server.owner_alerts import NEEDS_YOU

from tests.test_owner_alerts_endpoint import (  # noqa: F401  (fixtures)
    advance, bus, client, clock, head, poll, surface)


def test_every_card_type_reaches_the_page_while_he_is_in_the_app(
        client, bus, surface, clock):
    since = head(client)
    expected = {}
    made = decisions.create("atlas", "Ship v2 tonight?",
                            [{"label": "Ship"}, {"label": "Wait"}],
                            path=bus / "decisions.json")
    expected[made["id"]] = "decision"
    ask = asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                        subject="make deploy", cwd="/tmp/atlas")
    expected[ask.id] = "approval"
    for kind in handoff.KINDS:
        h = handoff.raise_handoff(bus / "handoffs.json", agent="atlas",
                                  kind=kind, needs=f"needs {kind}",
                                  state="nothing done", where="https://x.test")
        expected[h.id] = "handoff"
    surface.refresh()
    for _ in range(10):  # he is in the app the whole time
        poll(client, since, active=1)
        advance(surface, clock, 5)
    alerts = poll(client, since)["alerts"]
    got = {a["card_id"]: a["source"] for a in alerts}
    assert got == expected, "each card is its own push, none held or merged"
    for a in alerts:
        assert a["kind"] == NEEDS_YOU and a["count"] == 1
        assert a["thread_id"] == "direct:atlas"
        body = ntfy.payload(a, "t")
        assert body["priority"] >= 4
        assert f"card={a['card_id']}" in body["click"]


def test_a_card_raised_while_the_deck_restarts_still_reaches_him(
        bus, surface, clock, tmp_path):
    from server import api as api_mod
    from server.sources import comms as comms_mod
    surface.refresh()
    made = decisions.create("atlas", "Pay the invoice?",
                            [{"label": "Pay"}, {"label": "Hold"}],
                            path=bus / "decisions.json")
    # The deck went down before it ever saw the card, and came back.
    reborn = api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), deliver=lambda name, text: True,
        roster_path=bus / "roster.json", prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json", handoffs_path=bus / "handoffs.json",
        decisions_path=bus / "decisions.json",
        push_policy=pp.PushPolicy(settle=30, window=120, cap=6, presence=120,
                                  path=tmp_path / "pushes.json"),
        clock=clock)
    clock.now = made["ts"] + 5
    reborn.refresh()
    advance(reborn, clock, 30)
    pushed = [p for p in reborn.pushes.pushes if p.get("card_id") == made["id"]]
    assert len(pushed) == 1
    assert pushed[0]["title"] == "atlas needs you"
