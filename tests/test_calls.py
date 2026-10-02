"""K6 -- a live voice call: start, end, and what the desk is told.

Hermetic: tmp bus dir, a roster on disk, a recording waker. No daemon, no
`claude`. The deck's own lines are written as sender `deck` (a system voice),
never as the owner: a desk must not be able to mistake the framing for him.
"""

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import calls, office, owner

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None}
VOICE = {"id": "com.apple.voice.premium.en-GB.Malcolm", "rate": 1.1}


class Rig:
    def __init__(self, tmp_path, monkeypatch, desk=None, injected=False):
        monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
        monkeypatch.setattr(office, "BUS_DIR", tmp_path)
        (tmp_path / "messages.jsonl").write_text("")
        (tmp_path / "roster.json").write_text(
            json.dumps({"version": 1, "agents": [desk or DESK]}))
        monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
        self.dir = tmp_path
        self.woken = []
        self.woke = threading.Event()
        self.injected = []
        self.clock = [1000.0]
        self.can_inject = injected

        def wake(name):
            self.woken.append(name)
            self.woke.set()

        def inject(name, text):
            self.injected.append((name, text))
            return self.can_inject

        self.build = lambda: calls.build_router(
            roster_path=lambda: tmp_path / "roster.json", wake=wake,
            inject=inject, clock=lambda: self.clock[0])
        self.client = self.new_client()

    def new_client(self):
        app = FastAPI()
        api_mod.register(app, surface=api_mod.Surface(
            snapshot=lambda: {"generated_at": 1.0, "sessions": []},
            roster_path=self.dir / "roster.json",
            prefs_path=self.dir / "prefs.json"), background=False)
        app.include_router(self.build())
        return TestClient(app)

    def records(self):
        return [json.loads(l) for l in
                (self.dir / "messages.jsonl").read_text().splitlines() if l.strip()]

    def deck_lines(self):
        return [r for r in self.records() if r.get("from") == "deck"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def start(rig, agent="atlas"):
    return rig.client.post("/v1/calls", headers=AUTH, json={"agent": agent})


def test_start_returns_the_call_thread_and_the_desk_voice(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, desk={**DESK, "voice": VOICE})
    r = start(rig)
    assert r.status_code == 201
    body = r.json()
    assert body["call_id"].startswith("call_") and len(body["call_id"]) == 17
    assert body["thread_id"] == "direct:atlas"
    assert body["voice"] == VOICE


def test_start_without_a_stored_voice_returns_the_empty_default(rig):
    assert start(rig).json()["voice"] == {"id": "", "rate": 1.0}


def test_start_sends_the_deck_line_as_the_deck_not_the_owner(rig):
    start(rig)
    (line,) = rig.deck_lines()
    assert line["to"] == "atlas"
    assert line["text"].startswith(f"[Agent Deck] {owner.title()} started a live voice call.")
    assert "one or two plain spoken sentences" in line["text"]
    assert "`say`" in line["text"]
    assert not [r for r in rig.records() if r.get("from") in ("owner", "owner")]


def test_start_wakes_the_desk_with_reason_call(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    seen = []
    rig.build = lambda: calls.build_router(
        roster_path=lambda: tmp_path / "roster.json",
        wake=lambda name: (seen.append(name), rig.woke.set()),
        inject=lambda n, t: False)
    rig.client = rig.new_client()
    start(rig)
    assert rig.woke.wait(2) and seen == ["atlas"]


def test_a_desk_with_a_live_socket_is_told_now_and_the_line_is_acked(
        tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, injected=True)
    start(rig)
    assert rig.injected and rig.injected[0][0] == "atlas"
    assert "live voice call" in rig.injected[0][1]
    assert [r for r in rig.records() if r.get("ack")], "delivered line not acked"
    assert not rig.woken


def test_wake_reason_is_call_on_the_default_waker():
    import inspect
    from server import app as app_mod
    src = inspect.getsource(app_mod)
    assert 'reason="call"' in src


def test_unknown_agent_is_404(rig):
    r = start(rig, agent="nobody")
    assert r.status_code == 404 and r.json()["reason"] == "unknown_agent"


def test_start_needs_an_agent(rig):
    r = rig.client.post("/v1/calls", headers=AUTH, json={})
    assert r.status_code == 400 and r.json()["reason"] == "missing_agent"


def test_calls_need_the_bearer_token(rig):
    r = rig.client.post("/v1/calls", json={"agent": "atlas"})
    assert r.status_code == 401
    r = rig.client.post("/v1/calls/call_x/end")
    assert r.status_code == 401


def test_end_posts_the_closing_line_and_records_duration_and_turns(rig):
    call_id = start(rig).json()["call_id"]
    for text in ("status of acme", "and the deploy?"):
        rig.client.post("/v1/threads/direct:atlas/messages", headers=AUTH, json={
            "text": text, "channel": "voice", "call_id": call_id})
    # A typed message and a voice line from another call are not turns.
    rig.client.post("/v1/threads/direct:atlas/messages", headers=AUTH,
                    json={"text": "typed"})
    rig.client.post("/v1/threads/direct:atlas/messages", headers=AUTH, json={
        "text": "other", "channel": "voice", "call_id": "call_other"})
    rig.clock[0] += 12.345
    r = rig.client.post(f"/v1/calls/{call_id}/end", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "call_id": call_id, "duration_ms": 12345,
                        "turns": 2}
    closing = rig.deck_lines()[-1]
    assert closing["text"] == (
        "[Agent Deck] The call ended. This channel is closed from now on, so "
        "anything still owed goes in the chat.")


def test_end_unknown_call_is_404(rig):
    r = rig.client.post("/v1/calls/call_000000000000/end", headers=AUTH)
    assert r.status_code == 404 and r.json()["reason"] == "unknown_call"


def test_end_twice_is_409(rig):
    call_id = start(rig).json()["call_id"]
    assert rig.client.post(f"/v1/calls/{call_id}/end", headers=AUTH).status_code == 200
    r = rig.client.post(f"/v1/calls/{call_id}/end", headers=AUTH)
    assert r.status_code == 409 and r.json()["reason"] == "already_ended"
    assert len([l for l in rig.deck_lines() if "call ended" in l["text"]]) == 1


def test_an_open_call_survives_a_deck_restart(rig):
    call_id = start(rig).json()["call_id"]
    rig.clock[0] += 5
    restarted = rig.new_client()  # a fresh router, nothing in memory
    r = restarted.post(f"/v1/calls/{call_id}/end", headers=AUTH)
    assert r.status_code == 200 and r.json()["duration_ms"] == 5000


def test_the_router_is_mounted_on_the_real_app(monkeypatch):
    from server import app as app_mod
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    c = TestClient(app_mod.app)
    # An unmounted route is a 404/405; a mounted one is refused for auth.
    assert c.post("/v1/calls", headers={"authorization": ""},
                  json={"agent": "x"}).status_code == 401
    assert c.post("/v1/calls/call_x/end",
                  headers={"authorization": ""}).status_code == 401
