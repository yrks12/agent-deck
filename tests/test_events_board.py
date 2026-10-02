"""Event wake-ups (Feature C): the web board's Events view.

`/events` draws recent events, the desk each went to and its state, and an
editable routing table. Its data comes through `/api/events*`, which the
board's own auth middleware gates like every other `/api` route.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import deckauth, events


@pytest.fixture
def board(monkeypatch, tmp_path):
    store = events.Store(tmp_path / "events")
    store.save_routes([{"source": "deploy", "desk": "eng"}])
    hub = events.Hub(store, deliver=lambda d, t: "delivered")
    hub.ingest(events.Event(source="deploy", account="o/r", kind="failed",
                            summary="Deploy failed", ref="9", ts=1.0))
    monkeypatch.setattr(app_mod, "_event_hub", hub)
    return TestClient(app_mod.app), store


def test_the_events_data_is_gated_like_every_api_route(board, monkeypatch):
    client, _ = board
    monkeypatch.setattr(deckauth, "authorised", lambda request: False)
    assert client.get("/api/events").status_code == 401
    assert client.put("/api/events/routes", json={"routes": []}).status_code == 401


def test_the_board_reads_events_and_edits_routes(board, monkeypatch):
    client, store = board
    monkeypatch.setattr(deckauth, "authorised", lambda request: True)
    body = client.get("/api/events").json()
    assert body["events"][0]["desk"] == "eng" and body["events"][0]["state"] == "delivered"
    assert body["routes"][0]["source"] == "deploy"
    r = client.put("/api/events/routes",
                   json={"routes": [{"source": "gmail", "desk": "outreach"}]})
    assert r.status_code == 200
    assert [x.desk for x in store.routes()] == ["outreach"]
    assert client.put("/api/events/routes", json={"routes": "x"}).status_code == 400


def test_the_events_page_is_served_and_linked_from_the_deck():
    client = TestClient(app_mod.app)
    page = client.get("/events")
    assert page.status_code == 200 and "events.js" in page.text
    assert 'href="/events"' in (app_mod.WEB_DIR / "index.html").read_text()
    script = (app_mod.WEB_DIR / "events.js").read_text()
    assert "/api/events" in script and "/api/events/routes" in script
