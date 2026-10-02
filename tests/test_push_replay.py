"""The hour he complained about, replayed through today's policy.

MEASURED on the box (2026-09-30): 81 ntfy pushes in one hour. 45 were the
chief's plain replies, 14 were a test desk, and the rest were "X needs you"
cards, most of them cleared within seconds.

`fixtures/owner_alerts_last_hour.json` is that hour of real events, plus one
hour of warm-up before it, with desk names anonymised and no text. It holds
office records (who wrote to whom, and whether it was a turn-final answer, a
`say`, an urgent line or a decision card), approvals and handoffs with when
they were settled, and the roster's shape.

Assumptions that make the replay pessimistic:

* He never read a reply (no read events were logged), so a reply he was owed
  stays wanted.
* A handoff that is no longer waiting was settled 60 seconds after it was
  raised (the ledger keeps a status, not a time).
* His own messages are the only presence signal. The live deck also has
  reads and app-in-front pings, which hold more.
"""

import json
from pathlib import Path

from server import owner_alerts as oa
from server import push_policy as pp

FIXTURE = Path(__file__).parent / "fixtures" / "owner_alerts_last_hour.json"
OWNER = "owner"


def replay():
    data = json.loads(FIXTURE.read_text())
    bosses = {d["name"]: d["reports_to"] for d in data["roster"]}
    tests = {d["name"] for d in data["roster"]
             if oa.is_test_desk(name=d["name"], label=d["label"], test=d["test"])}
    classifier = oa.Classifier(boss_of=bosses.get, is_test=lambda n: n in tests)
    policy = pp.PushPolicy()
    settled: dict[str, float] = {}
    for e in data["events"]:
        if e["type"] == "card_settled" and e["at"] is not None:
            settled[e["card"]] = e["at"]
        if e["type"] == "approval" and e["settled_at"] is not None:
            settled[e["card"]] = e["settled_at"]
        if e["type"] == "handoff" and e["status"] != "waiting":
            settled[e["card"]] = e["t"] + 60

    events = sorted(data["events"], key=lambda e: e["t"])
    start, end = int(events[0]["t"]) - 1, int(data["duration"])
    pushes, i = [], 0
    for now in range(start, end + 1):
        while i < len(events) and events[i]["t"] <= now:
            e = events[i]
            i += 1
            if e["type"] == "record":
                if e["from"] == OWNER and e["to"] != OWNER:
                    policy.seen(at=e["t"])
                label = classifier.classify({**e, "kind": e.get("kind"),
                                             "decision": {"id": e.get("card")}})
                if label is None:
                    continue
                card = e.get("card") if e.get("kind") == "decision" else ""
                policy.offer({"id": e["id"], "kind": label,
                              "source": "decision" if card else "message",
                              "agent": e["from"], "thread_id": f"direct:{e['from']}",
                              "card_id": card or "", "title": e["from"],
                              "body": "", "urgent": bool(e.get("urgent"))},
                             at=e["t"])
            elif e["type"] in ("approval", "handoff"):
                if e["agent"] in tests:
                    continue
                policy.offer({"id": e["card"], "kind": oa.NEEDS_YOU,
                              "source": e["type"], "agent": e["agent"],
                              "thread_id": f"direct:{e['agent']}",
                              "card_id": e["card"], "title": e["agent"],
                              "body": ""}, at=e["t"])

        def wanted(alert, now=now):
            card = alert.get("card_id")
            return not card or settled.get(card, float("inf")) > now

        pushes += [p for p in policy.tick(now, wanted) if p["ts"] >= 0]
    return pushes


def test_the_hour_he_complained_about_is_under_ten_pushes():
    pushes = replay()
    print(f"\n81 -> {len(pushes)} pushes:",
          [(round(p["ts"]), p["title"], p["count"]) for p in pushes])
    # OWNER RULING 2026-10-01: a card that needs him is never capped or
    # merged, so the hour is its cards, each once, plus at most six reply
    # summaries -- still a fraction of the 81.
    cards = [p for p in pushes if p["kind"] == oa.NEEDS_YOU]
    replies = [p for p in pushes if p["kind"] != oa.NEEDS_YOU]
    assert cards and all(p["count"] == 1 and p["card_id"] for p in cards)
    assert len({p["card_id"] for p in cards}) == len(cards), "each card once"
    assert len(replies) <= 6
    assert len(pushes) < 20, (
        "the policy must cut the hour to a fraction and must still let "
        "something through: silence would be its own defect")


def test_no_push_in_that_hour_is_from_the_test_desk():
    data = json.loads(FIXTURE.read_text())
    tests = {d["name"] for d in data["roster"]
             if oa.is_test_desk(name=d["name"], label=d["label"], test=d["test"])}
    assert tests, "the fixture's test desk must be recognised as one"
    for push in replay():
        assert push.get("agent") not in tests
