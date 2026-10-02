"""Author truth: who a message says it is from must be who sent it (K1, K2).

The defect: `api._message_from_record` folded every sender in `OWNER_SENDERS`
(`sam`, `owner`, `routine`, `deck`) into `author: "owner", role: "owner"`, so a
schedule firing, a "Hired: ..." notice and an engineer's test message were all
drawn as things the owner typed. And a POST through the owner's token could
only ever be stamped `sam`.

Hermetic: tmp bus dir, hand-written snapshot, no daemon.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None}
SNAP = {"generated_at": 1_756_000_100.0, "sessions": []}


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


AUTH = {"Authorization": f"Bearer {TOKEN}"}
THREAD = "/v1/threads/direct:atlas/messages"


def page(client):
    return client.get(THREAD, headers=AUTH).json()["messages"]


@pytest.mark.parametrize("sender,author,role", [
    ("owner", "owner", "owner"),
    ("owner", "owner", "owner"),
    ("routine", "routine", "system"),
    ("deck", "deck", "system"),
    ("engineer", "engineer", "system"),
])
def test_every_sender_reads_as_itself(bus, monkeypatch, sender, author, role):
    _, client = make(bus, monkeypatch)
    office.send("atlas", "hello", sender=sender)
    (message,) = page(client)
    assert (message["author"], message["role"]) == (author, role)


def test_a_desk_reply_is_an_agent(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    office.send("owner", "done", sender="atlas")
    (message,) = page(client)
    assert (message["author"], message["role"]) == ("atlas", "agent")


def test_channel_and_kind_are_always_present(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    office.send("atlas", "hi", sender="owner")
    (message,) = page(client)
    assert message["channel"] == "text" and message["kind"] == "text"


def test_no_owner_role_message_has_a_non_owner_sender(bus, monkeypatch):
    """A-5 in miniature: role owner  <=>  the record's from is sam/owner."""
    _, client = make(bus, monkeypatch)
    for sender in ("owner", "owner", "routine", "deck", "engineer", "atlas"):
        office.send("atlas", f"from {sender}", sender=sender)
    for message in page(client):
        sender = message["text"].split()[-1]
        assert (message["role"] == "owner") == (sender in {"owner", "owner"})


def test_engineer_is_not_an_owner_sender():
    assert "engineer" not in office.OWNER_SENDERS
    assert not office.is_owner("engineer")
    assert office.mark_for("engineer") == office.ENGINEER_MARK
    assert "FROM THE ENGINEER" in office.ENGINEER_MARK
    framed = office.attribute("do the thing", "engineer")
    assert framed.startswith(office.ENGINEER_MARK)
    assert "owner's authority" not in framed


def test_post_as_engineer_is_recorded_and_returned_as_engineer(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.post(THREAD, headers=AUTH, json={"text": "ping", "as": "engineer"})
    assert r.status_code == 201
    assert r.json()["message"]["author"] == "engineer"
    assert r.json()["message"]["role"] == "system"
    record = json.loads((bus / "messages.jsonl").read_text().splitlines()[0])
    assert record["from"] == "engineer"


def test_post_defaults_to_owner(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.post(THREAD, headers=AUTH, json={"text": "ping"})
    assert r.json()["message"]["author"] == "owner"


@pytest.mark.parametrize("value", ["routine", "deck", "atlas", "", 5])
def test_any_other_sender_is_refused(bus, monkeypatch, value):
    _, client = make(bus, monkeypatch)
    r = client.post(THREAD, headers=AUTH, json={"text": "x", "as": value})
    assert r.status_code == 400 and r.json()["reason"] == "bad_sender"
    assert (bus / "messages.jsonl").read_text() == ""


def test_voice_needs_a_call_id(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.post(THREAD, headers=AUTH, json={"text": "x", "channel": "voice"})
    assert r.status_code == 400 and r.json()["reason"] == "missing_call_id"
    r = client.post(THREAD, headers=AUTH, json={"text": "x", "channel": "fax"})
    assert r.status_code == 400


def test_voice_message_is_framed_for_the_desk_and_clean_for_the_app(
        bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    r = client.post(THREAD, headers=AUTH, json={
        "text": "status of acme", "channel": "voice", "call_id": "call_abc"})
    assert r.status_code == 201
    record = json.loads((bus / "messages.jsonl").read_text().splitlines()[0])
    assert record["text"] == "status of acme\n(said on a live call)"
    assert record["channel"] == "voice" and record["call_id"] == "call_abc"
    message = r.json()["message"]
    assert message["channel"] == "voice" and message["text"] == "status of acme"


def test_system_lines_get_a_labelled_preview_and_no_unread_badge(
        bus, monkeypatch):
    surface, client = make(bus, monkeypatch)
    office.send("atlas", "Morning report\nsecond line", sender="routine")
    row = client.get("/v1/agents", headers=AUTH).json()["agents"][0]
    assert row["preview"] == "Routine: Morning report second line"
    assert row["unread"] == 0
    office.send("atlas", "Hired: x", sender="deck")
    row = client.get("/v1/agents", headers=AUTH).json()["agents"][0]
    assert row["preview"].startswith("Deck: ")


def test_system_messages_to_a_desk_still_carry_delivery(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    office.send("atlas", "tick", sender="routine")
    (message,) = page(client)
    assert message["delivery"] is not None


def test_asleep_is_an_injected_fact_and_defaults_to_awake(bus, monkeypatch):
    _, client = make(bus, monkeypatch)
    assert client.get("/v1/agents", headers=AUTH).json()["agents"][0][
        "state"] == "OFFLINE"


def test_an_asleep_desk_reads_asleep_and_its_message_is_waking(
        bus, monkeypatch):
    _, client = make(bus, monkeypatch, asleep=lambda name: name == "atlas")
    office.send("atlas", "hello", sender="owner")
    assert client.get("/v1/agents", headers=AUTH).json()["agents"][0][
        "state"] == "ASLEEP"
    (message,) = page(client)
    assert message["delivery"]["state"] == "sent"
    assert message["delivery"]["reason"] == "waking"
    assert "woken" in message["delivery"]["what"]
