"""A new line reaches the open chat whatever else the apps are asking.

The owner, 2026-10-01: "sometimes I need to get out of the chat and come back,
otherwise my messages disappear, and also the agents' messages."

`Surface.refresh()` is a diff: it reports a message once, on the tick that
first sees it, and never again. Every GET ran a refresh *and threw its events
away*, as did every write (`_refresh_holding` kept only `decision` frames). So
a desk's line that landed between two background ticks was eaten by whichever
request came first -- and the apps poll constantly -- and the owner's own sent
line was always eaten by the send that wrote it. The open chat never heard of
either; only reopening it (a fresh page fetch) showed them.
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def surface(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": "chief", "cwd": "/tmp/p", "engine": "claude",
         "mission": "run it", "reports_to": None}]}))
    made = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp/p", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0}]},
        comms=comms_mod.CommsIndex(),
        deliver=lambda name, text: True,
        roster_path=roster,
        prefs_path=tmp_path / "agent_prefs.json",
    )
    made.refresh()
    return made


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


@pytest.fixture
def stream(surface):
    """An open SSE subscriber, as the open chat is."""
    queue = asyncio.Queue(maxsize=64)
    surface._subscribers.add(queue)
    return queue


def texts_reaching_the_stream(surface, stream) -> list[str]:
    """What was published, plus what the next background tick publishes."""
    payloads = []
    while not stream.empty():
        payloads.append(json.loads(stream.get_nowait()))
    payloads += surface.refresh()  # the background loop's next tick
    return [p["message"]["text"] for p in payloads if p["type"] == "message"]


def test_a_desk_line_is_not_eaten_by_a_poll_that_lands_first(
        surface, client, stream):
    office.send(api_mod.OWNER, "the deploy is green", sender="chief")
    # The apps poll between ticks; any GET used to swallow the frame.
    assert client.get("/v1/agents", headers=AUTH).status_code == 200

    assert "the deploy is green" in texts_reaching_the_stream(surface, stream)


def test_his_own_sent_line_reaches_the_stream(surface, client, stream):
    sent = client.post("/v1/threads/direct:chief/messages", headers=AUTH,
                       json={"text": "ship it", "as": api_mod.OWNER})
    assert sent.status_code == 201, sent.text

    assert "ship it" in texts_reaching_the_stream(surface, stream)


def test_each_line_is_published_once_not_once_per_poll(surface, client, stream):
    office.send(api_mod.OWNER, "one line", sender="chief")
    client.get("/v1/agents", headers=AUTH)
    client.get("/v1/threads", headers=AUTH)

    assert texts_reaching_the_stream(surface, stream).count("one line") == 1
