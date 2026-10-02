"""Every app hears a ring from the one feed it already polls.

`GET /v1/owner/alerts` carries `ringing` (what an incoming-call screen draws,
for as long as it rings) and, once, a `call` push -- the Mac's notification,
the iPhone's local notification and ntfy all read that same push. A test
desk's ring is neither: probes never ring him.
"""

import json

import pytest

from server import api as api_mod
from server import office, ringing

T0 = 1_790_000_000.0
DESKS = [{"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "m",
          "label": "Atlas", "charter": "c", "reports_to": None},
         {"name": "ring-probe", "cwd": "/tmp/p", "engine": "claude",
          "mission": "m", "label": "probe", "charter": "c",
          "reports_to": "atlas", "test": True}]


@pytest.fixture
def surface(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    (tmp_path / "roster.json").write_text(json.dumps({"version": 1, "agents": DESKS}))
    ringing.update({"when": "anytime", "who": "any"}, at=T0)
    s = api_mod.Surface(snapshot=lambda: {"generated_at": 1.0, "sessions": []},
                        roster_path=tmp_path / "roster.json",
                        prefs_path=tmp_path / "prefs.json", clock=lambda: T0 + 1)
    s.refresh()
    return s


def test_a_ring_is_in_the_feed_while_it_rings_and_pushed_once(surface):
    head = surface.owner_alerts(None)["next_since"]
    rid = ringing.place("atlas", "prod db is down", "urgent", chief="atlas",
                        at=T0)["ring_id"]
    [push] = surface.push_rings()
    assert surface.push_rings() == [], "one push per ring"
    page = surface.owner_alerts(head)
    assert [r["id"] for r in page["ringing"]] == [rid]
    [alert] = page["alerts"]
    assert alert["source"] == "ring" and alert["ring_id"] == rid
    assert alert["kind"] == "needs_you", "an app that predates rings still decodes the page"
    assert alert["title"] == "Atlas is calling — prod db is down"
    assert alert["thread_id"] == "direct:atlas"


def test_a_test_desk_never_rings_him(surface):
    ringing.place("ring-probe", "hello", "urgent", chief="atlas", at=T0)
    assert surface.push_rings() == []
    assert surface.owner_alerts(None)["ringing"] == []


def test_the_deck_loop_sweeps_and_pushes_rings_every_tick(monkeypatch):
    """A ring nobody answers must become the fallback even with no app open."""
    import inspect

    from server import app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.ringing, "sweep", lambda: calls.append("sweep"))
    monkeypatch.setattr(app_mod._client_surface, "push_rings",
                        lambda: calls.append("push"))
    app_mod._ring_tick()
    assert calls == ["sweep", "push"]
    assert "_ring_tick" in inspect.getsource(app_mod._loop)
