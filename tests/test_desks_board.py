"""Desks reach the board.

The behaviour being fixed: a card vanishes from the deck the instant its
Terminal tab closes, so a named agent cannot exist across a restart. A desk on
the roster must stay on the board as OFFLINE.

That means the collector's own snapshot -- what `/api/state` and the SSE stream
carry -- has to include the desk join, not just the live sessions. Asserting on
`/api/roster` alone would leave the board unchanged and every test green.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import office
from server.collector import Collector
from server.sources.sessions import RawSession

ACME = {
    "name": "acme-growth",
    "cwd": "/Users/samcarter/Projects/acme",
    "engine": "claude",
    "mission": "Own Acme's paid acquisition.",
}


class FakeScanner:
    def __init__(self, items):
        self.items = items

    def scan(self):
        return self.items


class FakePoller:
    def __init__(self, value):
        self.value = value

    def poll(self):
        return self.value


@pytest.fixture
def quiet_office(monkeypatch):
    """No writes to the real bus dir, no git subprocesses."""
    monkeypatch.setattr(office, "publish", lambda cards: None)
    monkeypatch.setattr(office, "pending_counts", lambda: {})
    monkeypatch.setattr(office, "toplevel_for", lambda cwd: "")
    monkeypatch.setattr(office, "branch_for", lambda toplevel: "")


def collector_with(tmp_path, desks, sessions=(), cards=()):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": list(desks)}))
    collector = Collector()
    collector.roster_path = path
    collector.scanner = FakeScanner(list(sessions))
    collector.opencode = FakeScanner([])
    collector.bus = FakePoller({})
    collector.usage = FakePoller({})
    collector.opencode_usage = FakePoller({})
    if cards:
        supplied = list(cards)
        collector._build = lambda session, signals, now: supplied.pop(0)
    return collector


def test_the_snapshot_carries_an_offline_desk(tmp_path, quiet_office):
    snapshot = collector_with(tmp_path, [ACME]).tick()

    assert "desks" in snapshot, "/api/state carries no desks, so the board cannot show any"
    acme = next(d for d in snapshot["desks"] if d["name"] == "acme-growth")
    assert acme["state"] == "OFFLINE"
    assert acme["desk"] is True
    assert acme["mission"] == ACME["mission"]


def test_the_snapshot_still_carries_the_live_sessions(tmp_path, quiet_office):
    snapshot = collector_with(tmp_path, [ACME]).tick()
    assert snapshot["sessions"] == []
    assert snapshot["totals"]["sessions"] == 0


def test_a_desk_in_the_snapshot_shows_its_occupants_state(tmp_path, quiet_office):
    session = RawSession(
        pid=4242, session_id="sid-p", cwd="/Users/samcarter/Projects/acme",
        name="acme-growth", status="busy", kind="interactive", started_at=0,
    )
    card = {
        "session_id": "sid-p", "pid": 4242, "name": "acme-growth",
        "cwd": "/Users/samcarter/Projects/acme", "state": "WORKING",
        "git_branch": "", "agents_running": 0, "usage": {"output": 0},
    }
    collector = collector_with(tmp_path, [ACME], sessions=[session], cards=[card])

    acme = next(d for d in collector.tick()["desks"] if d["name"] == "acme-growth")

    assert acme["state"] == "WORKING"
    assert acme["session_id"] == "sid-p"
    assert acme["pid"] == 4242


def test_the_state_endpoint_exposes_the_desks(monkeypatch):
    monkeypatch.setattr(
        app_mod,
        "_state",
        {"sessions": [], "desks": [{"name": "acme-growth", "state": "OFFLINE",
                                    "desk": True}], "totals": {}},
    )
    body = TestClient(app_mod.app).get("/api/state").json()
    assert body["desks"][0]["name"] == "acme-growth"


# --- the panel --------------------------------------------------------------

WEB = __import__("pathlib").Path(__file__).resolve().parent.parent / "web"


def test_the_deck_page_has_a_desks_panel():
    assert 'id="desks"' in (WEB / "index.html").read_text()


def test_the_client_paints_the_desks_from_the_snapshot():
    source = (WEB / "app.js").read_text()
    assert "desks" in source, "the client never reads the desks the snapshot carries"
    assert "OFFLINE" in source, "the client has no offline state to show"


def test_the_panel_is_styled():
    assert ".desk" in (WEB / "style.css").read_text()
