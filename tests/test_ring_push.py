"""A desk's ring rides the ONE push policy, at once, and once per ring.

A ring lasts 30 s, so the settle, presence, coalesce and hourly-cap rules that
hold an ordinary card would swallow it: `PushPolicy.ring` pushes it now. It is
still deduped by the same offered memory, on disk, so a restart or a second
sweep never rings him twice for one ring. ntfy posts it at the top priority
with a click link that opens the app into the call.
"""

from server import ntfy
from server import push_policy as pp

RING = {"id": "ring:ring_abc", "kind": "needs_you", "source": "ring",
        "agent": "atlas", "thread_id": "direct:atlas", "card_id": "",
        "message_id": "", "ring_id": "ring_abc", "urgent": True,
        "title": "Atlas is calling — prod db is down",
        "body": "Tap to answer. It rings for 30 seconds.", "ts": 100.0}


def test_a_ring_is_pushed_at_once_even_while_he_is_looking(tmp_path):
    policy = pp.PushPolicy(path=tmp_path / "pushes.json")
    policy.seen(at=100.0)
    push = policy.ring(RING, at=100.0)
    assert push is not None and push["source"] == "ring"
    assert push["ring_id"] == "ring_abc" and push["cursor"]
    assert policy.pushes[-1] is push


def test_a_ring_is_pushed_once_even_after_a_restart(tmp_path):
    first = pp.PushPolicy(path=tmp_path / "pushes.json")
    assert first.ring(RING, at=100.0) is not None
    assert first.ring(RING, at=101.0) is None
    again = pp.PushPolicy(path=tmp_path / "pushes.json")
    assert again.ring(RING, at=102.0) is None
    assert len(again.pushes) == 1


def test_a_ring_does_not_use_up_the_hourly_cap_for_cards(tmp_path):
    policy = pp.PushPolicy(path=tmp_path / "pushes.json", cap=1, settle=0)
    policy.ring(RING, at=100.0)
    card = {"id": "ask:1", "kind": "needs_you", "source": "approval",
            "agent": "atlas", "card_id": "1", "thread_id": "direct:atlas"}
    policy.offer(card, at=100.0)
    assert len(policy.tick(100.0, lambda a: True)) == 1


def test_ntfy_rings_at_top_priority_and_opens_the_app_into_the_call():
    body = ntfy.payload(RING, "deck-k3y")
    assert body["priority"] == 5
    assert body["title"] == "Atlas is calling — prod db is down"
    assert body["click"].startswith("agentdeck://call?")
    assert "ring=ring_abc" in body["click"] and "thread=direct%3Aatlas" in body["click"]
    assert body["tags"] == ["telephone_receiver"]
