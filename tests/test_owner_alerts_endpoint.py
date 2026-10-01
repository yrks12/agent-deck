"""`GET /v1/owner/alerts?since=<cursor>`: what is new FOR HIM, and nothing else.

The phone cannot hold a stream open in the background: iOS grants a refresh
task a few seconds, now and then. So the question it asks has to be one cheap
call -- "what needs me, or was said to me, since the last thing you told me
about?" -- and the answer has to be right three ways:

1. It carries every class that may buzz him: a desk's answer, a decision card,
   a pending approval, a waiting handoff -- each with the thread to open.
2. It carries nothing else: progress lines and desk<->desk chatter stay out.
3. The same thing is never handed over twice. `next_since` moves past what was
   returned, so the second poll is empty -- the dedupe a notification needs.

A first poll with no cursor returns nothing and a cursor at the head: a phone
that just installed the app must not be buzzed with a month of history.

Hermetic: tmp bus, hand-written snapshot, nothing spawned.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking, decisions, handoff, office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ATLAS_SID = "bfffdc59-5f34-4b6b-b304-300c80cb3c25"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def surface(bus):
    roster_file = bus / "roster.json"
    roster_file.write_text(json.dumps({"version": 1, "agents": [
        {"name": "atlas", "cwd": "/tmp/atlas", "engine": "claude",
         "mission": "run it", "reports_to": None},
        {"name": "scout", "cwd": "/tmp/scout", "engine": "claude",
         "mission": "look", "reports_to": "atlas"},
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


def poll(client, since=None):
    params = {} if since is None else {"since": since}
    response = client.get("/v1/owner/alerts", params=params, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def head(client):
    first = poll(client)
    assert first["alerts"] == [], "a fresh install must not be buzzed with history"
    return first["next_since"]


def test_a_first_poll_is_quiet_even_with_history(client, bus):
    office.send(office.OWNER_INBOX, "old news", sender="atlas",
                extra={"spoke": True})
    body = poll(client)
    assert body["alerts"] == []
    assert api_mod._CURSOR.match(body["next_since"])
    assert poll(client, body["next_since"])["alerts"] == []


def test_a_desks_answer_is_handed_over_with_its_thread(client, bus):
    since = head(client)
    office.send(office.OWNER_INBOX, "Deployed. /healthz is 200.",
                sender="atlas", extra={"spoke": True})

    alerts = poll(client, since)["alerts"]

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "for_you"
    assert alert["source"] == "message"
    assert alert["agent"] == "atlas"
    assert alert["thread_id"] == "direct:atlas"
    assert "Deployed" in alert["body"]
    assert alert["id"].startswith("msg:")


def test_every_thing_that_needs_him_is_there(client, bus, surface):
    since = head(client)
    card = decisions.create("atlas", "Ship v2 tonight?",
                            [{"label": "Ship"}, {"label": "Wait"}],
                            path=bus / "decisions.json")
    ask = asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                        subject="rm -rf build", cwd="/tmp/atlas")
    block = handoff.raise_handoff(bus / "handoffs.json", agent="atlas",
                                  kind="login", needs="Sign in to GitHub",
                                  state="nothing pushed yet",
                                  where="https://github.com")

    alerts = poll(client, since)["alerts"]
    by_source = {a["source"]: a for a in alerts}

    assert set(by_source) == {"decision", "approval", "handoff"}
    assert all(a["kind"] == "needs_you" for a in alerts)
    assert by_source["decision"]["card_id"] == card["id"]
    assert by_source["approval"]["card_id"] == ask.id
    assert by_source["handoff"]["card_id"] == block.id
    assert all(a["thread_id"] == "direct:atlas" for a in alerts)
    assert "Ship v2 tonight?" in by_source["decision"]["body"]
    assert "GitHub" in by_source["handoff"]["body"]


def test_progress_and_chatter_stay_out(client, bus):
    since = head(client)
    office.send(office.OWNER_INBOX, "On it -- checking Acme first.",
                sender="atlas", extra={"said": True})
    office.send("scout", "look at the logs", sender="atlas")
    office.send(office.OWNER_INBOX, "scanned 40 files", sender="scout",
                extra={"spoke": True})

    assert poll(client, since)["alerts"] == []


def test_the_same_thing_is_never_handed_over_twice(client, bus):
    since = head(client)
    office.send(office.OWNER_INBOX, "Done.", sender="atlas",
                extra={"spoke": True})
    asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                  subject="make deploy", cwd="/tmp/atlas")

    first = poll(client, since)
    assert len(first["alerts"]) == 2
    ids = [a["id"] for a in first["alerts"]]
    assert len(set(ids)) == 2
    assert first["alerts"] == sorted(first["alerts"], key=lambda a: a["cursor"])

    second = poll(client, first["next_since"])
    assert second["alerts"] == [], "a second poll re-announced what he was told"
    assert second["next_since"] == first["next_since"]


def test_an_ask_he_already_answered_is_not_news(client, bus):
    since = head(client)
    ask = asking.record(bus / "asks.json", agent="atlas", tool="Bash",
                        subject="make deploy", cwd="/tmp/atlas")
    asking.answer(bus / "asks.json", ask.id, "once")

    assert poll(client, since)["alerts"] == []


def test_a_cursor_that_is_not_ours_is_refused(client):
    response = client.get("/v1/owner/alerts", params={"since": "yesterday"},
                          headers=auth())
    assert response.status_code == 400


def test_the_route_needs_the_token(client):
    assert client.get("/v1/owner/alerts").status_code in (401, 403)
