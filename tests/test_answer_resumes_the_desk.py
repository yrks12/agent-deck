"""Answering an approval must restart the agent. Tapping it must be enough.

The defect these pin, measured three times in one live run. `Surface.answer_ask`
writes the rule (or leaves the unspent `once` grant) and returns. It queues
nothing. And the denial the desk already received says, correctly:

    Do not retry it in a loop -- say in your reply that you are waiting on ask
    <id> and carry on with something else.

So a well-behaved agent ends its turn and waits. After each of the three
answers in that run, nothing moved in the ledger until a human sent a follow-up
chat message. **Two of the five interventions in the entire run existed only
because of this.** On a phone the owner taps Approve, the card clears, the rule
is written -- and the work does not resume, with nothing on the screen telling
him a further message is needed.

The shape the fix has to get right, and what each test holds it to:

1. **It names what was approved.** A desk can be blocked on several things at
   once. "You may proceed" is useless; the message has to carry the ask id, the
   tool and the exact subject, so the agent resumes the right one.
2. **It fires for a grant and only for a grant.** `always` writes an allow rule
   and `once` leaves an unspent grant -- both mean go. `never` grants nothing,
   and telling an agent to resume something just forbidden is worse than
   silence.
3. **It cannot loop.** The nudge is stamped on the ask (`resumed_at`) and sent
   at most once per ask, ever; a second answer is already a 409. If the agent
   retries and is denied again, that is a NEW ask with a new id, which is a new
   decision by the owner rather than a machine going round.
4. **He can see it.** The nudge lands in his own direct thread with the desk,
   because it is his decision reaching the desk -- not a hidden side effect.

Read at the ledger and at the API. Nothing here reads a Terminal screen.
Hermetic: tmp bus, hand-written snapshot, nothing spawned.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking, office
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {
    "name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}
ACME = str(Path.home() / "Projects" / "acme")
SUBJECT = "gh pr create --title x"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF]}))
    return path


@pytest.fixture
def delivered():
    """Every (name, text) the daemon would have injected into a live session."""
    return []


@pytest.fixture
def surface(bus, roster_file, delivered):
    return api_mod.Surface(
        snapshot=lambda: {
            "generated_at": 1_756_000_100.0,
            "sessions": [{
                "session_id": "sid-chief", "pid": 4242, "name": "chief",
                "cwd": "/tmp/p", "project": "p", "state": "WORKING",
                "state_since": 1_756_000_000.0,
            }],
        },
        comms=comms_mod.CommsIndex(),
        deliver=lambda name, text: bool(delivered.append((name, text)) or True),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json",
        routines_path=bus / "routines.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def ask(bus):
    return asking.record(bus / "asks.json", agent="chief", tool="Bash",
                         subject=SUBJECT, cwd=ACME)


def queued_for(bus, name):
    """Messages sitting on the office queue for `name`. The ledger, not the UI."""
    out = []
    for line in (bus / "messages.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("to") == name and record.get("text"):
            out.append(record)
    return out


def answer(client, ask_id, reply):
    return client.post(f"/v1/approvals/{ask_id}", headers=auth(),
                       json={"reply": reply})


# -- 1. the desk is told, and told which thing --------------------------------

def test_answering_always_tells_the_desk_to_resume(client, bus, ask):
    """THE detector. He taps approve; the work restarts with no chat message."""
    assert answer(client, ask.id, "always").status_code == 200

    nudges = queued_for(bus, "chief")
    assert len(nudges) == 1, (
        "answering the ask queued nothing for the desk, so the agent -- which "
        "was told not to retry in a loop -- is still sitting there waiting for "
        f"a human to type at it: {nudges!r}")


def test_the_resume_names_the_exact_action_that_was_approved(client, bus, ask):
    """A desk can be blocked on several things. 'Go ahead' is not an answer."""
    answer(client, ask.id, "always")
    text = queued_for(bus, "chief")[0]["text"]

    assert ask.id in text, f"the nudge does not name the ask: {text!r}"
    assert SUBJECT in text, f"the nudge does not name the action: {text!r}"
    assert "Bash" in text, f"the nudge does not name the tool: {text!r}"


def test_the_resume_tells_the_agent_what_to_do_if_it_is_denied_again(
    client, bus, ask
):
    """The loop guard is in the words as well as in the ledger."""
    answer(client, ask.id, "always")
    text = queued_for(bus, "chief")[0]["text"].lower()
    assert "do not retry" in text, (
        f"nothing stops the agent looping on a second denial: {text!r}")


def test_a_once_answer_resumes_too(client, bus, ask):
    """`once` writes no rule -- it leaves an unspent grant. Same need."""
    answer(client, ask.id, "once")
    assert len(queued_for(bus, "chief")) == 1


# -- 2. only a grant resumes --------------------------------------------------

def test_never_resumes_nothing(client, bus, ask):
    """A refusal grants nothing. Telling a desk to proceed would be a lie."""
    assert answer(client, ask.id, "never").status_code == 200
    assert queued_for(bus, "chief") == [], (
        "a denial told the desk to carry on with the thing it was just denied")


# -- 3. it cannot loop --------------------------------------------------------

def test_one_ask_is_resumed_at_most_once(client, bus, ask):
    """A duplicate tap -- or a replayed WhatsApp reply -- must not re-nudge."""
    answer(client, ask.id, "always")
    second = answer(client, ask.id, "always")
    assert second.status_code == 409
    assert second.json()["reason"] == "already_answered"
    assert len(queued_for(bus, "chief")) == 1


def test_the_ask_records_that_it_was_resumed(client, bus, ask):
    """The guard is a durable fact on the ledger, not just control flow."""
    answer(client, ask.id, "always")
    settled = asking.find(bus / "asks.json", ask.id)
    assert settled.resumed_at, (
        f"nothing on the ask says the desk was told: {settled!r}")


# -- 4. it goes straight in when the desk is live, and he can see it ----------

def test_a_live_desk_is_told_directly_rather_than_on_its_next_turn(
    client, bus, ask, delivered
):
    """Queued-only means the agent waits for a turn it has no reason to take."""
    answer(client, ask.id, "always")
    assert [name for name, _ in delivered] == ["chief"], (
        f"the live desk was never handed the resume: {delivered!r}")
    assert ask.id in delivered[0][1]


def test_the_resume_is_visible_in_the_owners_own_thread(client, surface, ask):
    """Read from the API. What his tap did must not be invisible to him."""
    answer(client, ask.id, "always")
    surface.refresh()
    res = client.get("/v1/threads/direct:chief/messages", headers=auth())
    assert res.status_code == 200, res.text
    texts = [m["text"] for m in res.json()["messages"]]
    assert any(ask.id in t and SUBJECT in t for t in texts), (
        f"his approval never showed up in his thread with the desk: {texts!r}")


def test_the_answer_response_says_the_desk_was_told(client, ask):
    """The card can say 'restarted', instead of clearing and looking finished."""
    body = answer(client, ask.id, "always").json()
    assert body["resumed"] is True


def test_the_answer_response_says_a_denial_told_nobody(client, ask):
    body = answer(client, ask.id, "never").json()
    assert body["resumed"] is False


# -- 5. what the LIVE run found ----------------------------------------------
#
# Both of these passed above and failed against a real hired agent, because the
# fixture ask carries the desk's name in `agent` and a real one does not.
# `app._permission` fills `agent` from the hook payload, and `cc-permission.js`
# sends no desk name -- so it falls through to the session id. Measured: ask
# `ny8z4` came back with `agent: "8edb89dc-6be9-4daf-b90e-cf88c4630410"`.


@pytest.fixture
def ask_by_session_id(bus):
    """An ask the way the permission hook actually records one.

    `agent` is the raw session id, because that is what `app._permission` gets.
    Everything downstream keys on the DESK name -- the owner's thread, the
    unread count, `app._try_inject`'s socket lookup -- so a resume addressed to
    the id lands nowhere he will ever look.
    """
    return asking.record(bus / "asks.json", agent="sid-chief", tool="Bash",
                         subject=SUBJECT, cwd=ACME)


def test_a_resume_for_a_session_id_still_reaches_the_desk(
    client, bus, ask_by_session_id, delivered
):
    """He must see it in his thread with `chief`, not in one named by a UUID."""
    body = answer(client, ask_by_session_id.id, "always").json()
    assert body["resumed"] is True
    assert queued_for(bus, "chief"), (
        "the resume was addressed to the raw session id, so it is not in the "
        f"desk's thread and nothing on the board shows it: {queued_for(bus, 'sid-chief')!r}")
    assert [name for name, _ in delivered] == ["chief"], (
        "injection looks the desk up by NAME; a session id finds no socket")


def test_the_answered_ask_it_hands_back_carries_the_resume_stamp(client, ask):
    """`resumed: true` beside `resumed_at: null` is the API disagreeing with
    itself. Measured on the live run -- the response said both."""
    body = answer(client, ask.id, "always").json()
    assert body["resumed"] is True
    assert body["ask"]["resumed_at"], (
        f"the ask handed back says it was never resumed: {body['ask']!r}")
