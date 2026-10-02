"""A ring's life: placed, then answered, declined or missed -- never a dead end.

Declined, or unanswered for 30 s, the desk's reason is posted to his thread
as a line from the desk, and the desk is told he did not pick up. A refused
ring (calls off, urgent only, the daily cap) posts the reason too; a repeat
of a reason already rung today posts nothing, so a looping desk cannot spam.
"""

import json

import pytest

from server import office, ringing

T0 = 1_790_000_000.0   # 2026-09-21 ~10:13 New York: outside quiet hours


@pytest.fixture(autouse=True)
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    ringing.update({"when": "urgent"}, at=T0)
    return tmp_path


def records(bus):
    return [json.loads(l) for l in (bus / "messages.jsonl").read_text().splitlines()]


def ring(reason="prod db is down", urgency="urgent", desk="atlas", at=T0):
    return ringing.place(desk, reason, urgency, chief="atlas", at=at)


def test_a_ring_is_live_for_thirty_seconds(bus):
    placed = ring()
    assert placed["ok"] and placed["state"] == "ringing"
    [live] = ringing.live(at=T0 + 1)
    assert live["id"] == placed["ring_id"] and live["agent"] == "atlas"
    assert live["reason"] == "prod db is down" and live["urgent"] is True
    assert live["thread_id"] == "direct:atlas"
    assert live["expires_at"] == T0 + 30
    assert ringing.live(at=T0 + 31) == []
    assert records(bus) == [], "a ring posts nothing until it is settled"


def test_answered_it_posts_nothing_and_cannot_be_answered_twice(bus):
    rid = ring()["ring_id"]
    assert ringing.answer(rid, at=T0 + 5)["state"] == "answered"
    assert ringing.live(at=T0 + 6) == []
    with pytest.raises(ringing.RingError) as err:
        ringing.answer(rid, at=T0 + 6)
    assert err.value.reason == "not_ringing" and err.value.status == 409
    assert ringing.sweep(at=T0 + 60) == []
    assert records(bus) == []


def test_declined_it_posts_the_reason_to_his_thread_and_tells_the_desk(bus):
    ringing.decline(ring()["ring_id"], at=T0 + 3)
    to_him, to_desk = records(bus)
    assert to_him["to"] == office.OWNER_INBOX and to_him["from"] == "atlas"
    assert "prod db is down" in to_him["text"]
    assert to_desk["to"] == "atlas" and to_desk["from"] == "deck"
    assert "declined" in to_desk["text"]


def test_unanswered_for_thirty_seconds_it_is_missed_with_the_fallback(bus):
    rid = ring()["ring_id"]
    assert ringing.sweep(at=T0 + 29) == []
    [missed] = ringing.sweep(at=T0 + 30)
    assert missed["id"] == rid and missed["state"] == "missed"
    assert ringing.sweep(at=T0 + 90) == [], "missed once"
    assert [r["to"] for r in records(bus)] == [office.OWNER_INBOX, "atlas"]
    with pytest.raises(ringing.RingError):
        ringing.answer(rid, at=T0 + 31)


def test_a_refused_ring_still_reaches_his_thread(bus):
    out = ring(urgency="normal")
    assert out["ok"] is False and out["reason"] == "urgent_only"
    assert out["posted_to_thread"] is True
    [posted] = records(bus)
    assert posted["from"] == "atlas" and posted["text"] == "prod db is down"


def test_one_ring_per_reason_and_three_a_day(bus):
    assert ring("Prod DB is down!")["ok"]
    again = ring("prod db is down", at=T0 + 40)
    assert again["reason"] == "already_rang" and not again["posted_to_thread"]
    assert ring("billing failed", at=T0 + 50)["ok"]
    assert ring("cert expires", at=T0 + 60)["ok"]
    capped = ring("disk full", at=T0 + 70)
    assert capped["reason"] == "daily_cap" and capped["posted_to_thread"]
    assert ring("disk full", at=T0 + 86400)["ok"], "a new day, a new count"


def test_another_desk_is_turned_away_without_posting(bus):
    out = ring(desk="qa")
    assert out["reason"] == "not_allowed" and records(bus) == []


@pytest.mark.parametrize("reason,urgency", [("", "urgent"), ("x" * 201, "urgent"),
                                            ("db", "panic")])
def test_a_bad_ring_is_refused(reason, urgency):
    with pytest.raises(ringing.RingError):
        ring(reason, urgency)
