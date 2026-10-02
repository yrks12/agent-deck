"""`GET /v1/owner/alerts?since=<cursor>` hands every channel the same PUSHES.

The deck decides once whether something buzzes him (`owner_alerts` for what
is about him, `push_policy` for when). ntfy, the iPhone and the Mac all read
this page, so they cannot disagree, and the cap is a cap on him rather than
per device.

The GOOD signal is asserted every time: the one push that should arrive.

1. A card that needs him arrives as one push, with its thread and card.
2. A plain reply he did not ask for never pushes. A reply to his own message
   pushes once, after he has left.
3. A test desk never pushes.
4. Presence holds pushes: `?active=1` from an app in front, a read or a
   message from him.
5. Each push is handed over once. A first poll returns nothing and a cursor
   at the head.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking, decisions, handoff, office, push_policy
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ATLAS_SID = "bfffdc59-5f34-4b6b-b304-300c80cb3c25"


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def surface(bus, clock):
    roster_file = bus / "roster.json"
    roster_file.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None},
        {"name": "scout", "cwd": "/tmp/scout", "engine": "claude",
         "mission": "look", "reports_to": "atlas"},
        {"name": "wake-probe", "cwd": "/tmp/p", "engine": "claude",
         "mission": "be probed", "reports_to": None,
         "label": "wake-probe (engineer test desk)"},
    ]}))
    made = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": [{
            "session_id": ATLAS_SID, "pid": 4242, "name": "atlas",
            "cwd": "/tmp/atlas", "project": "atlas", "state": "WORKING",
            "state_since": 1_756_000_000.0}]},
        comms=comms_mod.CommsIndex(),
        deliver=lambda name, text: True,
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json",
        handoffs_path=bus / "handoffs.json",
        decisions_path=bus / "decisions.json",
        push_policy=push_policy.PushPolicy(settle=30, window=120, cap=6,
                                           presence=120),
        clock=clock,
    )
    made.refresh()
    return made


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def poll(client, since=None, **params):
    if since is not None:
        params["since"] = since
    response = client.get("/v1/owner/alerts", params=params, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def head(client):
    first = poll(client)
    assert first["alerts"] == [], "a fresh install must not be buzzed with history"
    return first["next_since"]


def advance(surface, clock, seconds, step=5):
    for _ in range(int(seconds // step)):
        clock.now += step
        surface.refresh()


def test_a_card_that_needs_him_arrives_as_one_push(client, bus, surface, clock):
    since = head(client)
    ask = asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                        subject="make deploy", cwd="/tmp/atlas")
    surface.refresh()
    assert poll(client, since)["alerts"] == [], "held for the settle"
    advance(surface, clock, 40)

    body = poll(client, since)
    assert len(body["alerts"]) == 1
    push = body["alerts"][0]
    assert push["kind"] == "needs_you"
    assert push["card_id"] == ask.id
    assert push["thread_id"] == "direct:atlas"
    assert push["title"] == "atlas needs you"
    assert poll(client, body["next_since"])["alerts"] == [], "handed over once"


def test_a_decision_card_pushes_and_a_settled_one_does_not(client, bus, surface, clock):
    since = head(client)
    card = decisions.create("atlas", "Ship v2 tonight?",
                            [{"label": "Ship"}, {"label": "Wait"}],
                            path=bus / "decisions.json")
    surface.refresh()
    advance(surface, clock, 40)
    alerts = poll(client, since)["alerts"]
    assert [a["card_id"] for a in alerts] == [card["id"]]

    later = poll(client, since)["next_since"]
    other = decisions.create("atlas", "And v3?", [{"label": "Yes"}, {"label": "No"}],
                             path=bus / "decisions.json")
    surface.refresh()
    decisions.answer(other["id"], "Yes", path=bus / "decisions.json")
    advance(surface, clock, 400)
    assert poll(client, later)["alerts"] == []


def test_a_plain_reply_is_quiet_and_an_asked_one_reaches_him_once(client, bus, surface, clock):
    since = head(client)
    office.send(office.OWNER_INBOX, "Status: all green.", sender="atlas",
                extra={"spoke": True})
    surface.refresh()
    advance(surface, clock, 400)
    assert poll(client, since)["alerts"] == [], "a reply he did not ask for buzzed"

    surface.send("direct:atlas", "deploy it")  # he wrote, from the app
    office.send(office.OWNER_INBOX, "Deployed.", sender="atlas",
                extra={"spoke": True})
    office.send(office.OWNER_INBOX, "Logs clean too.", sender="atlas",
                extra={"spoke": True})
    surface.refresh()
    advance(surface, clock, 100)
    assert poll(client, since)["alerts"] == [], "he is still in the app"
    advance(surface, clock, 100)
    alerts = poll(client, since)["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "for_you"
    assert "Deployed." in alerts[0]["body"]


def test_a_test_desk_never_pushes(client, bus, surface, clock):
    since = head(client)
    asking.record(bus / "asks.json", agent="wake-probe", tool="Bash",
                  subject="ls", cwd="/tmp/p")
    decisions.create("wake-probe", "Probe pick?", [{"label": "Red"}, {"label": "Blue"}],
                     path=bus / "decisions.json")
    office.send(office.OWNER_INBOX, "One. Two.", sender="wake-probe",
                extra={"spoke": True})
    surface.refresh()
    advance(surface, clock, 400)
    assert poll(client, since)["alerts"] == []
    # The good signal: a real desk's card in the same hour still reaches him.
    asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                  subject="make deploy", cwd="/tmp/atlas")
    surface.refresh()
    advance(surface, clock, 40)
    assert [a["agent"] for a in poll(client, since)["alerts"]] == ["atlas"]


def owed_reply(surface, text="Deployed."):
    surface.send("direct:atlas", "deploy it")  # he wrote, from the app
    office.send(office.OWNER_INBOX, text, sender="atlas", extra={"spoke": True})
    surface.refresh()


# An app in front holds REPLIES. A card that needs him is never held for it
# (tests/test_needs_you_always_reaches_him.py).
def test_an_app_in_front_holds_replies_until_he_leaves(client, bus, surface, clock):
    since = head(client)
    owed_reply(surface)
    for _ in range(8):
        poll(client, since, active=1)  # the phone, in front, every 12s
        advance(surface, clock, 12, step=12)
    assert poll(client, since)["alerts"] == []
    advance(surface, clock, 130)
    assert len(poll(client, since)["alerts"]) == 1


def test_a_read_counts_as_looking(client, bus, surface, clock):
    since = head(client)
    owed_reply(surface)
    response = client.post("/v1/agents/atlas/read", json={}, headers=auth())
    assert response.status_code == 200, response.text
    advance(surface, clock, 100)
    assert poll(client, since)["alerts"] == []


def test_the_page_says_whether_ntfy_delivers(client, monkeypatch):
    monkeypatch.delenv("DECK_NTFY_URL", raising=False)
    assert poll(client)["ntfy"] is False
    monkeypatch.setenv("DECK_NTFY_URL", "http://192.0.2.1:8090/deck-x")
    assert poll(client)["ntfy"] is True


def test_a_cursor_that_is_not_ours_is_refused(client):
    response = client.get("/v1/owner/alerts", params={"since": "yesterday"},
                          headers=auth())
    assert response.status_code == 400


def test_the_route_needs_the_token(client):
    assert client.get("/v1/owner/alerts").status_code in (401, 403)
