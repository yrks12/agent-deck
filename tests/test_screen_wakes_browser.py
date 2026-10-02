"""A stopped desk browser comes back when someone needs it.

`server/browser_reaper.py` stops idle desk browsers to keep the box alive
(measured 2026-10-01: 13 Chromiums, swap full, 15-24 s per frame). That is
only acceptable if stopping costs the owner nothing but a short wait:

* opening the desk's screen starts it again, and says "waking" meanwhile
  rather than "no computer";
* the screen and the computer tools mark the desk as used, so the reaper does
  not stop a browser under the owner's eyes or mid-task;
* a new container makes room under the cap before it starts.

Hermetic: no Docker. `sandbox` and the reaper are stubbed at their seams.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import browser_reaper, desk_computer, office, sandbox
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(browser_reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(browser_reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(browser_reaper, "MEMORY_PATH", tmp_path / "memory.json")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def client(bus, monkeypatch):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=bus / "agent_prefs.json", asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json", routines_path=bus / "routines.json",
    )
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_opening_a_stopped_desks_screen_asks_for_its_browser(client, monkeypatch):
    """Asks, not wakes: at 19:24 on 2026-10-01 the owner clicked through nine
    desks and every poll started a browser. `ask` wakes one only once he has
    stayed on it (tests/test_desk_browser_memory.py)."""
    asked, woken = [], []
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    monkeypatch.setattr(browser_reaper, "ask",
                        lambda desk: asked.append(desk) or True, raising=False)
    monkeypatch.setattr(browser_reaper, "wake",
                        lambda desk: woken.append(desk) or True)
    body = client.get("/v1/agents/acme/screen", headers=AUTH).json()
    assert asked == ["acme"] and woken == []
    assert body["computer"]["running"] is False
    assert body["computer"]["waking"] is True


def test_a_running_desk_is_not_woken_and_says_so(client, monkeypatch):
    woken = []
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    monkeypatch.setattr(browser_reaper, "wake",
                        lambda desk: woken.append(desk) or True)
    body = client.get("/v1/agents/acme/screen", headers=AUTH).json()
    assert woken == []
    assert body["computer"]["waking"] is False


def test_watching_the_screen_marks_the_desk_as_used(client, monkeypatch, bus):
    monkeypatch.setattr(sandbox, "frame", lambda desk: JPEG)
    assert client.get("/v1/agents/acme/screen.jpg", headers=AUTH).status_code == 200
    assert "acme" in browser_reaper.last_uses(["acme"])


def test_taking_over_marks_the_desk_as_used(client, monkeypatch):
    monkeypatch.setattr(sandbox, "send_input", lambda desk, action: None)
    response = client.post("/v1/agents/acme/screen/input", headers=AUTH,
                           json={"action": "click", "x": 1, "y": 1})
    assert response.status_code == 200
    assert "acme" in browser_reaper.last_uses(["acme"])


# ── the computer tools' door ────────────────────────────────────────────────


class _Stop(Exception):
    pass


def test_ensure_makes_room_before_starting_and_marks_use(monkeypatch, bus):
    order = []
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    monkeypatch.setattr(browser_reaper, "make_room",
                        lambda desk: order.append(("room", desk)) or [])

    def start(desk):
        order.append(("start", desk))
        raise _Stop()

    monkeypatch.setattr(sandbox, "start", start)
    with pytest.raises(_Stop):
        desk_computer.ensure("acme")
    assert order == [("room", "acme"), ("start", "acme")]
    assert "acme" in browser_reaper.last_uses(["acme"])


def test_ensure_on_a_running_desk_does_not_make_room(monkeypatch, bus):
    calls = []
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    monkeypatch.setattr(browser_reaper, "make_room",
                        lambda desk: calls.append(desk) or [])
    monkeypatch.setattr(desk_computer.ContainerCDP, "page_target",
                        lambda self: SimpleNamespace())
    monkeypatch.setattr(desk_computer.ContainerCDP, "wait_for_page",
                        lambda self, timeout=None: None)
    desk_computer.ensure("acme")
    assert calls == []
