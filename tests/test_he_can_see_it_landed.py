"""Detector: every message he sends carries, ON THE THREAD, what became of it.

THE COST. The box's Claude login expired and every desk started and died
instantly. He typed "hi?", "whats goingon?", "hello?", "?", "hello" into his
chief's thread and got nothing back -- and the thread drew all five exactly
like messages that had landed. His words: *"i cant see sent/read"*. He asked
whether the deck was offline. It had been, for hours.

The deck already knew. `office.pending_counts` is documented as "undelivered
messages per recipient, for the deck's sent/pending marker" and
`POST /v1/threads/{id}/messages` has answered `delivery.state` since the last
pass at this. Neither fact ever reached `GET /v1/threads/{id}/messages`, which
is the one call the app makes to DRAW the conversation -- so the screen he was
actually looking at had nothing to say.

THREE STATES, each one something the deck can point at evidence for:

  * `delivered` -- an `{"ack": id}` record exists in `messages.jsonl`. Written
    by exactly two things: `office.ack`, after the bytes went down a live
    socket, and `hooks/cc-office.js:ackMessages`, when a session took the
    message into a turn. Both mean a session has it.
  * `sent` -- no ack, and a session is seated that can still take it.
  * `undelivered` -- no ack, and nothing at that desk can take it. WITH A
    REASON, because "undelivered" on its own is the blank thread again in one
    word.

NOT `read`. The two ack writers are the socket push and the turn injection, and
both append the same bare `{"ts", "ack"}` record. Nothing on disk distinguishes
"a session took it" from "the model read it", so a `read` state would be a
guess wearing a receipt's clothes. Two live states, not three.

THE GOOD SIGNAL matters as much as the failure one.
`test_a_message_to_a_live_desk_reads_delivered` is what stops this shipping as
a system that marks everything undelivered and calls it honesty.

THE TRANSITION is the test that proves the state is COMPUTED and not STAMPED.
A state written at send time would have frozen his five messages at whatever
was true the second he pressed return, which is the same lie one layer along.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {"name": "chief", "cwd": "/tmp/p", "engine": "claude",
         "mission": "run it", "reports_to": None}
HEMINGWAY = {"name": "hemingway", "cwd": "/tmp/p", "engine": "claude",
             "mission": "write", "reports_to": "chief"}

#: `chief` is seated and working; `hemingway` has nobody at its desk -- the
#: shape of the whole box the night his login expired.
LIVE = {"session_id": "sid-chief", "pid": 4242, "name": "chief",
        "cwd": "/tmp/p", "project": "p", "state": "WORKING",
        "state_since": 1_756_000_000.0}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF, HEMINGWAY]}))
    return path


@pytest.fixture
def snapshot():
    return {"generated_at": 1_756_000_100.0, "sessions": [dict(LIVE)]}


@pytest.fixture
def sockets():
    """Which desks have a socket that takes bytes. `chief` does, and only chief."""
    return {"chief"}


@pytest.fixture
def surface(bus, roster_file, snapshot, sockets):
    return api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        deliver=lambda name, text: name in sockets,
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def thread(client, name):
    """The thread the app draws, exactly as the app fetches it."""
    got = client.get(f"/v1/threads/direct:{name}/messages", headers=auth())
    assert got.status_code == 200, got.text
    return got.json()["messages"]


def send(client, name, text):
    got = client.post(f"/v1/threads/direct:{name}/messages",
                      json={"text": text}, headers=auth())
    assert got.status_code == 201, got.text
    return got.json()


# -- the failure state, which is the whole bug -------------------------------


def test_a_message_to_a_desk_with_no_session_reads_undelivered(client):
    """His five unanswered messages, on the thread that drew them as fine."""
    send(client, "hemingway", "hi?")
    (message,) = thread(client, "hemingway")
    delivery = message["delivery"]
    assert delivery["state"] == "undelivered", (
        f"a desk with nobody at it took this message, apparently: {delivery!r}")


def test_the_undelivered_reason_is_specific_and_not_an_empty_shrug(client):
    """`undelivered` with no reason is the blank thread again, in one word."""
    send(client, "hemingway", "whats goingon?")
    (message,) = thread(client, "hemingway")
    delivery = message["delivery"]
    assert delivery["reason"] == "no_session", (
        f"no usable reason on an undelivered message: {delivery!r}")
    assert "hemingway" in delivery["what"], (
        "the sentence shown to the owner does not say which desk went dark: "
        f"{delivery['what']!r}")


# -- the good signal ---------------------------------------------------------


def test_a_message_to_a_live_desk_reads_delivered(client):
    """Without this, "mark everything undelivered" would pass the whole file."""
    send(client, "chief", "you are my chief of staff")
    (message,) = thread(client, "chief")
    assert message["delivery"]["state"] == "delivered", (
        f"a live desk took this and the thread says otherwise: {message!r}")
    assert message["delivery"]["reason"] == ""


def test_a_live_desk_that_has_not_taken_it_yet_reads_sent_not_undelivered(
    client, sockets
):
    """Seated, socket shut, message on the queue: `hooks/cc-office.js` hands it
    over on that desk's next turn, so calling this undelivered would be a false
    alarm of exactly the kind this change exists to stop."""
    sockets.clear()
    send(client, "chief", "when you get a moment")
    (message,) = thread(client, "chief")
    assert message["delivery"]["state"] == "sent", (
        f"a seated desk was written off: {message['delivery']!r}")


# -- the transition: computed, never stamped ---------------------------------


def test_a_message_taken_after_the_desk_came_back_reads_delivered(
    client, bus, snapshot
):
    """The one that proves the state is live.

    Sent while `hemingway` is dark, then the desk restarts and the office hook
    acks it into a turn. The thread must move.
    """
    sent = send(client, "hemingway", "hello")
    (before,) = thread(client, "hemingway")
    assert before["delivery"]["state"] == "undelivered"

    # Byte for byte what `hooks/cc-office.js:ackMessages` appends when it hands
    # a pending message to a turn.
    with (bus / "messages.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": 1_756_000_200.0,
                             "ack": sent["message"]["id"]}) + "\n")
    snapshot["sessions"].append({
        "session_id": "sid-hem", "pid": 5151, "name": "hemingway",
        "cwd": "/tmp/p", "project": "p", "state": "IDLE",
        "state_since": 1_756_000_190.0})

    (after,) = thread(client, "hemingway")
    assert after["delivery"]["state"] == "delivered", (
        f"the state was stamped at write time and never moved: "
        f"{after['delivery']!r}")


# -- the class: every door onto the queue, not just the app's ----------------


def test_a_message_queued_by_another_door_carries_a_state_too(client, bus):
    """`office.send` is the ONE queue. The routine scheduler
    (`app._deliver_routine`), the approval nudge and the handoff resume
    (`api._resume_desk`, `_settle_handoff`) and the hire notice
    (`harvest._tell_the_boss`) all write through it as `routine` or `deck` and
    never touch the client surface. A state that lived only on the app's own
    POST would leave every one of those drawn as though it had landed."""
    office.send("hemingway", "your 9am fired", sender="routine")
    (message,) = thread(client, "hemingway")
    assert message["role"] == "system", "the deck's own senders are not him typing"
    assert message["delivery"]["state"] == "undelivered"
    assert message["delivery"]["reason"] == "no_session"


def test_the_post_response_and_the_thread_agree(client):
    """One computation, both doors. The phone path (`app._send_to_desk`) reads
    the POST result and tells him what happened; the app reads the thread. Two
    implementations would drift, and the half that drifted would be the half
    nobody watches."""
    posted = send(client, "hemingway", "?")
    (drawn,) = thread(client, "hemingway")
    assert posted["message"]["delivery"] == drawn["delivery"]


# -- what does NOT get a state ----------------------------------------------


def test_what_a_desk_said_to_him_carries_no_delivery_state(client, bus):
    """He is reading it; there is nothing to report. The key is still present
    and `null`, so a client reads a value rather than having to know a key can
    be missing."""
    office.send(office.OWNER_INBOX, "done", sender="chief")
    drawn = thread(client, "chief")
    assert drawn, "the desk's own line never reached his thread"
    assert drawn[-1]["role"] == "agent"
    assert drawn[-1]["delivery"] is None
