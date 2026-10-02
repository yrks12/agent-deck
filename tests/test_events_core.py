"""Event wake-ups (Feature C), the pure core: route, dedupe, coalesce, log.

A real event (a Stripe payment, a reply to a desk's email, a sign-up, a failed
deploy) must reach the ONE desk that owns it, once, as its message -- so that
desk can stop polling for it. Everything here runs on a tmp dir and an injected
clock and delivery; nothing touches the real bus.
"""

from __future__ import annotations

import json

import pytest

from server import events


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _ev(ref: str, *, source: str = "stripe", account: str = "acme",
        kind: str = "payment", summary: str = "GBP 9.00 from a@b.c") -> events.Event:
    return events.Event(source=source, account=account, kind=kind,
                        summary=summary, ref=ref, ts=1_000.0)


def _hub(tmp_path, routes, *, window: float = 60.0):
    store = events.Store(tmp_path / "events")
    store.save_routes(routes)
    sent: list[tuple[str, str]] = []

    def deliver(desk: str, text: str) -> str:
        sent.append((desk, text))
        return "delivered"

    clock = Clock()
    return events.Hub(store, deliver=deliver, clock=clock, window=window), sent, clock, store


ROUTES = [
    {"source": "stripe", "account": "acme*", "desk": "acme-lead"},
    {"source": "deploy", "account": "*", "kind": "failed", "desk": "bravo-eng"},
    {"source": "stripe", "account": "*", "desk": "atlas"},
]


def test_the_first_matching_route_wins_and_globs_and_kind_filter():
    routes = [events.Route.from_dict(r) for r in ROUTES]
    assert events.route_for(routes, _ev("1", account="acme-uk")) == "acme-lead"
    assert events.route_for(routes, _ev("2", account="bravo")) == "atlas"
    assert events.route_for(routes, _ev("3", source="DEPLOY", kind="failed")) == "bravo-eng"
    assert events.route_for(routes, _ev("4", source="deploy", kind="succeeded")) is None
    assert events.route_for(routes, _ev("5", source="gmail")) is None


def test_an_event_is_delivered_once_per_ref_even_after_a_restart(tmp_path):
    hub, sent, _, store = _hub(tmp_path, ROUTES)
    assert hub.ingest(_ev("evt_1"))["state"] == "delivered"
    assert hub.ingest(_ev("evt_1"))["state"] == "duplicate"
    again = events.Hub(store, deliver=lambda d, t: sent.append((d, t)) or "delivered",
                       clock=Clock(), window=60.0)
    assert again.ingest(_ev("evt_1"))["state"] == "duplicate"
    assert len(sent) == 1


def test_a_burst_for_one_desk_is_coalesced_into_one_message(tmp_path):
    hub, sent, clock, _ = _hub(tmp_path, ROUTES, window=60.0)
    assert hub.ingest(_ev("a", summary="first"))["state"] == "delivered"
    clock.now += 5
    assert hub.ingest(_ev("b", summary="second"))["state"] == "held"
    clock.now += 5
    assert hub.ingest(_ev("c", summary="third"))["state"] == "held"
    assert hub.flush() == []          # window still open
    assert len(sent) == 1
    clock.now += 60
    flushed = hub.flush()
    assert [r["desk"] for r in flushed] == ["acme-lead"]
    assert len(sent) == 2
    combined = sent[1][1]
    assert "second" in combined and "third" in combined and "first" not in combined
    assert "2 events" in combined
    # Both held events now read delivered in the log.
    states = {r["ref"]: r["state"] for r in hub.store.recent()}
    assert states == {"a": "delivered", "b": "delivered", "c": "delivered"}


def test_another_desk_is_not_held_by_the_first_desks_window(tmp_path):
    hub, sent, _, _ = _hub(tmp_path, ROUTES)
    hub.ingest(_ev("a"))
    assert hub.ingest(_ev("d", source="deploy", kind="failed"))["state"] == "delivered"
    assert [d for d, _ in sent] == ["acme-lead", "bravo-eng"]


def test_an_unrouted_event_is_logged_not_dropped(tmp_path):
    hub, sent, _, store = _hub(tmp_path, ROUTES)
    out = hub.ingest(_ev("g1", source="gmail", kind="reply"))
    assert out == {"ok": True, "state": "unrouted", "desk": None, "ref": "g1"}
    assert sent == []
    [row] = store.recent()
    assert row["state"] == "unrouted" and row["source"] == "gmail"


def test_the_message_says_event_source_summary_ref_and_no_polling():
    text = events.message_for([_ev("pi_123", summary="GBP 9.00 from a@b.c")])
    assert "event" in text.lower()
    assert "stripe" in text and "GBP 9.00 from a@b.c" in text and "pi_123" in text
    assert "do not need to poll" in text


def test_a_failed_delivery_is_logged_as_failed(tmp_path):
    store = events.Store(tmp_path / "events")
    store.save_routes(ROUTES)
    hub = events.Hub(store, deliver=lambda d, t: "failed: could not queue",
                     clock=Clock())
    assert hub.ingest(_ev("x"))["state"] == "failed: could not queue"


@pytest.mark.parametrize("bad", [
    {"source": "", "summary": "s", "ref": "r"},
    {"source": "stripe", "summary": "", "ref": "r"},
    {"source": "stripe", "summary": "s", "ref": ""},
    {"source": "bad source!", "summary": "s", "ref": "r"},
    {"source": "stripe", "summary": "x" * 5000, "ref": "r"},
    "not a dict",
])
def test_a_malformed_event_is_refused_with_a_reason(bad):
    with pytest.raises(events.EventError):
        events.Event.from_dict(bad, now=1.0)


def test_from_dict_defaults_account_kind_and_ts():
    ev = events.Event.from_dict({"source": "signup", "summary": "new user",
                                 "ref": "u1"}, now=42.0)
    assert (ev.account, ev.kind, ev.ts) == ("", "", 42.0)


def test_routes_are_validated_on_save(tmp_path):
    store = events.Store(tmp_path / "events")
    with pytest.raises(events.EventError):
        store.save_routes([{"source": "stripe"}])          # no desk
    store.save_routes([{"source": "stripe", "desk": "atlas"}])
    saved = json.loads((tmp_path / "events" / "routes.json").read_text())
    assert saved["routes"] == [{"source": "stripe", "account": "*", "kind": "*",
                                "desk": "atlas"}]


def test_the_log_and_the_seen_set_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "LOG_MAX", 10)
    monkeypatch.setattr(events, "SEEN_MAX", 10)
    hub, _, clock, store = _hub(tmp_path, [], window=0)
    for i in range(40):
        clock.now += 1
        hub.ingest(_ev(f"r{i}", source="nobody"))
    assert len(store.recent(limit=1000)) <= 20
    assert len(store.seen_refs()) <= 20
