"""A desk's voice and drawn character (K6), and the POST answer to a sleeper.

1. `PATCH /v1/agents/{name}` takes `voice` {"id", "rate"} and `avatar`
   {"shape", "color"}, stored on the roster desk (`voice`, `avatar_look`), and
   both come back on `GET /v1/agents/{name}` and on the sidebar rows.

   `avatar` was ALREADY a field: an opaque string (a picture) that the
   installed app sends on every settings save, `null` included, and decodes
   as `String?`. So the object form is accepted on PATCH, a string/null keeps
   meaning the picture, and the drawn character comes back under its own key,
   `avatar_look` -- an object under `avatar` would fail the installed app's
   decode of the whole row.

2. MEASURED live on atlas (2026-09-30): a message to an ASLEEP desk answered
   the POST with "atlas is not at a desk right now ... waiting and will be
   read on the next turn there" while the deck was in fact waking it. The
   POST's `delivery` must say what is happening: it is being woken.

Hermetic: tmp bus dir, hand-written snapshot, no daemon.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, roster
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None}
SNAP = {"generated_at": 1_756_000_100.0, "sessions": []}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    (tmp_path / "roster.json").write_text(
        json.dumps({"version": 1, "agents": [DESK]}))
    return tmp_path


def make(bus, monkeypatch, **kw):
    surface = api_mod.Surface(
        snapshot=lambda: SNAP, comms=comms_mod.CommsIndex(),
        roster_path=bus / "roster.json", prefs_path=bus / "prefs.json", **kw)
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return surface, TestClient(app)


def desk(bus):
    (found,) = roster.load_roster(bus / "roster.json")
    return found


VOICE = {"id": "com.apple.voice.premium.en-GB.Malcolm", "rate": 1.1}
LOOK = {"shape": "hexagon", "color": 3}


# ── voice and avatar ────────────────────────────────────────────────────────


def test_nothing_set_reads_null_so_the_client_derives_it(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    got = client.get("/v1/agents/atlas", headers=AUTH).json()
    assert got["voice"] is None and got["avatar_look"] is None


def test_voice_is_stored_on_the_desk_and_read_back(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.patch("/v1/agents/atlas", headers=AUTH, json={"voice": VOICE})
    assert r.status_code == 200 and r.json()["voice"] == VOICE
    assert desk(bus).voice == VOICE
    assert client.get("/v1/agents/atlas", headers=AUTH).json()["voice"] == VOICE
    row = client.get("/v1/agents", headers=AUTH).json()["agents"][0]
    assert row["voice"] == VOICE


def test_avatar_object_is_the_drawn_character(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.patch("/v1/agents/atlas", headers=AUTH, json={"avatar": LOOK})
    assert r.status_code == 200
    assert r.json()["avatar_look"] == LOOK
    assert desk(bus).avatar_look == LOOK
    # The picture field keeps its type for the installed app.
    assert r.json()["avatar"] is None
    row = client.get("/v1/agents", headers=AUTH).json()["agents"][0]
    assert row["avatar_look"] == LOOK and row["avatar"] is None


def test_the_installed_apps_settings_save_does_not_wipe_the_character(
        bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    client.patch("/v1/agents/atlas", headers=AUTH, json={"avatar": LOOK})
    # What `AgentPatch` sends on every save: the picture string, or null.
    r = client.patch("/v1/agents/atlas", headers=AUTH, json={
        "label": "Chief", "charter": "Own it.", "section": "Work",
        "notifications": True, "pinned": False, "avatar": None})
    assert r.status_code == 200 and r.json()["avatar_look"] == LOOK
    r = client.patch("/v1/agents/atlas", headers=AUTH,
                     json={"avatar": "chief.png"})
    assert r.json()["avatar"] == "chief.png"
    assert r.json()["avatar_look"] == LOOK


def test_null_clears_voice_and_avatar_look(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    client.patch("/v1/agents/atlas", headers=AUTH,
                 json={"voice": VOICE, "avatar": LOOK})
    r = client.patch("/v1/agents/atlas", headers=AUTH,
                     json={"voice": None, "avatar_look": None})
    assert r.json()["voice"] is None and r.json()["avatar_look"] is None
    assert desk(bus).voice is None and desk(bus).avatar_look is None


@pytest.mark.parametrize("payload,reason", [
    ({"voice": {"id": 5, "rate": 1.0}}, "bad_voice"),
    ({"voice": {"id": "x", "rate": 9}}, "bad_voice"),
    ({"voice": {"id": "x", "rate": True}}, "bad_voice"),
    ({"voice": "Malcolm"}, "bad_voice"),
    ({"avatar": {"shape": "star", "color": 1}}, "bad_avatar"),
    ({"avatar": {"shape": "blob", "color": 12}}, "bad_avatar"),
    ({"avatar": {"shape": "blob"}}, "bad_avatar"),
    ({"avatar_look": "blob"}, "bad_avatar"),
])
def test_a_bad_voice_or_avatar_is_400_and_changes_nothing(
        bus, monkeypatch, payload, reason):
    _, client = make(bus, monkeypatch)
    r = client.patch("/v1/agents/atlas", headers=AUTH, json=payload)
    assert r.status_code == 400 and r.json()["reason"] == reason
    assert desk(bus).voice is None and desk(bus).avatar_look is None


def test_voice_on_a_session_that_is_not_a_desk_is_409(bus, monkeypatch):
    snap = {"sessions": [{"name": "stray", "session_id": "s1", "state": "IDLE",
                          "cwd": "/tmp", "pid": 1}]}
    surface = api_mod.Surface(
        snapshot=lambda: snap, comms=comms_mod.CommsIndex(),
        roster_path=bus / "roster.json", prefs_path=bus / "prefs.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    r = TestClient(app).patch("/v1/agents/stray", headers=AUTH,
                              json={"voice": VOICE})
    assert r.status_code == 409 and r.json()["reason"] == "not_a_desk"


# ── the POST answer to a sleeping desk ──────────────────────────────────────


def test_posting_to_an_asleep_desk_says_it_is_being_woken(bus, monkeypatch):
    _, client = make(bus, monkeypatch, asleep=lambda name: name == "atlas",
                     deliver=lambda name, text: False)
    r = client.post("/v1/threads/direct:atlas/messages", headers=AUTH,
                    json={"text": "hello", "as": "engineer"})
    delivery = r.json()["delivery"]
    assert delivery["what"] == "atlas was asleep and is being woken to read this."
    assert delivery["reason"] == "waking"
    assert "not at a desk" not in delivery["what"]
    assert delivery["waiting"] == ["atlas"]


def test_an_offline_desk_that_cannot_be_woken_keeps_the_queued_sentence(
        bus, monkeypatch):
    _, client = make(bus, monkeypatch, deliver=lambda name, text: False)
    r = client.post("/v1/threads/direct:atlas/messages", headers=AUTH,
                    json={"text": "hello", "as": "engineer"})
    delivery = r.json()["delivery"]
    assert delivery["state"] == "queued" and delivery["reason"] == ""
    assert "not at a desk" in delivery["what"]
