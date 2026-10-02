"""What a desk says in its own transcript must appear in that desk's thread.

The defect these pin, measured on a live hire: `drift-watch` was interviewed,
wrote out a well-formed opening question -- "what do you actually want me for?"
plus a numbered list -- and the owner's board showed an empty chat and no
unread badge. `Surface._rebuild` builds a thread from two sources and neither
of them carries ordinary assistant prose: `messages.jsonl` only ever held
records the *owner* (or a routine, or a group fanout) wrote, and the comms
index only holds `SendMessage` edges between two agents. Nothing on this
machine has ever written an agent-to-owner record. So the product's opening
move -- the new hire's first question -- was invisible on the only surface the
owner reads, and he had nothing to reply to.

What the fix must get right, and what each test here holds it to:

1. **The prose lands.** The agent's own sentence, out of the API, in the
   agent's own direct thread, attributed to the agent.
2. **Not every line is a message.** A transcript is mostly tool calls, thinking
   and narration ("Let me read the file."). Only a *turn-final* text block --
   an assistant record whose `stop_reason` is `end_turn`, the moment the model
   stopped and handed back to a human -- is the desk talking. Measured on the
   real `drift-watch` transcript: 2 such records in 87KB.
3. **It must not double-post.** The tick re-reads the same file every second.
4. **Unread must rise**, or the badge lies and he never opens the thread.
5. **The reply lands in the same conversation** -- his answer and the desk's
   question read as one thread, in order, not two logs.

Hermetic: roster, bus ledger, message queue and transcript all under tmp_path.
Nothing spawns and nothing touches ~/.claude.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office
from server.harvest import Harvester
from server.roster import Desk, save_roster
from server.sources import comms as comms_mod

TOKEN = "t-not-a-real-credential"
SESSION = "sid-drift"
DESK = "drift-watch"

#: The agent's OWN words, from the live run. Deliberately not a phrase that
#: appears anywhere in `onboard.interview_prompt` -- a check that greps for
#: text the instructions also contain is fooled by the instructions.
QUESTION = (
    "What do you actually want me for? Your hint -- watching the repos under "
    "~/Projects for stale branches -- I can do, but it's a small job and I'd "
    "rather know the real one before I settle into it."
)


def desk_row(name, cwd):
    return Desk(name=name, cwd=str(cwd), engine="claude",
                mission="Watch the repos.", label="Drift",
                charter="You own branch rot.", reports_to=None)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """A private agent-bus: roster, prefs, message queue and ledger."""
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_path(bus, tmp_path):
    path = bus / "roster.json"
    save_roster(path, [desk_row(DESK, tmp_path / "work")])
    return path


@pytest.fixture
def transcript(tmp_path):
    return tmp_path / "transcript.jsonl"


@pytest.fixture
def sessions(transcript):
    """One live session, seated at the desk, with its transcript to hand."""
    return [{"session_id": SESSION, "name": DESK, "pid": 4242,
             "cwd": "/tmp/work", "project": "work", "state": "WORKING",
             "state_since": 1_756_000_000.0,
             "transcript": str(transcript)}]


@pytest.fixture
def harvester(roster_path, tmp_path):
    return Harvester(roster_path, tmp_path / "harvest-offsets.json")


@pytest.fixture
def surface(roster_path, bus, sessions):
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": sessions},
        comms=comms_mod.CommsIndex(),
        roster_path=roster_path,
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


# -- transcript records, in the shape Claude Code actually writes -------------

def assistant(text, *, stop_reason="end_turn", sidechain=False, uuid="u-1"):
    """One assistant record.

    `stop_reason` is stamped on every record of one message, so a text block
    emitted alongside a tool call carries `tool_use` and a turn-final one
    carries `end_turn` -- verified against the real `drift-watch` transcript,
    where the content blocks of a single message are split across records.
    """
    return {
        "type": "assistant", "uuid": uuid, "sessionId": SESSION,
        "isSidechain": sidechain, "timestamp": "2026-09-01T15:11:00.000Z",
        "message": {"id": "msg-1", "role": "assistant",
                    "stop_reason": stop_reason,
                    "content": [{"type": "text", "text": text}]},
    }


def tool_use(uuid="u-2"):
    return {
        "type": "assistant", "uuid": uuid, "sessionId": SESSION,
        "isSidechain": False, "timestamp": "2026-09-01T15:11:01.000Z",
        "message": {"id": "msg-1", "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "toolu_1",
                                 "name": "Read", "input": {}}]},
    }


def write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def append(path, record):
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def tick(harvester, surface, sessions):
    """One daemon tick: harvest every transcript, then fold it into the view."""
    harvester.poll(sessions)
    surface.refresh()


def thread_messages(client, name=DESK):
    res = client.get(f"/v1/threads/direct:{name}/messages", headers=auth())
    assert res.status_code == 200, res.text
    return res.json()["messages"]


# -- 1. the prose lands ------------------------------------------------------

def test_a_desks_opening_question_reaches_the_owners_thread(
    transcript, harvester, surface, sessions, client
):
    """THE detector. The agent asked; the owner must be able to read it."""
    write(transcript, [assistant(QUESTION)])
    tick(harvester, surface, sessions)

    messages = thread_messages(client)
    said = [m for m in messages if "What do you actually want me for?" in m["text"]]
    assert len(said) == 1, (
        f"the desk's opening question is not in its thread: {messages!r}")
    assert said[0]["author"] == DESK
    assert said[0]["role"] == "agent"
    assert said[0]["text"].startswith("What do you actually want me for?")


def test_the_question_raises_the_unread_badge(
    transcript, harvester, surface, sessions
):
    """Without this the badge lies and he never opens the thread."""
    write(transcript, [assistant(QUESTION)])
    tick(harvester, surface, sessions)
    assert surface.agent(DESK)["unread"] >= 1


def test_the_sidebar_preview_carries_the_question(
    transcript, harvester, surface, sessions
):
    write(transcript, [assistant(QUESTION)])
    tick(harvester, surface, sessions)
    assert "What do you actually want me for?" in surface.agent(DESK)["preview"]


# -- 2. not every line is a message ------------------------------------------

def test_narration_alongside_a_tool_call_is_not_a_message(
    transcript, harvester, surface, sessions, client
):
    """Measured on the real transcript: "I'll run the drift scan myself..." is
    a text block carrying `stop_reason: tool_use`, immediately followed by two
    tool calls. It is the agent narrating its own work, not talking to him."""
    write(transcript, [
        assistant("I'll run the drift scan myself and report back.",
                  stop_reason="tool_use", uuid="u-a"),
        tool_use(),
    ])
    tick(harvester, surface, sessions)
    assert thread_messages(client) == []


def test_a_subagents_words_are_not_the_desks_words(
    transcript, harvester, surface, sessions, client
):
    write(transcript, [assistant("Subagent finished the sweep.",
                                 sidechain=True, uuid="u-b")])
    tick(harvester, surface, sessions)
    assert thread_messages(client) == []


def test_a_marker_line_is_stripped_but_the_prose_around_it_survives(
    transcript, harvester, surface, sessions, client
):
    """From the live run: the desk explained its hire in prose and put the
    `YOS_HIRE` JSON on its own line. The owner wants the sentence, never the
    blob -- the blob is already the roster's business, not the chat's."""
    hire = json.dumps({"name": "branch-scout", "label": "Scout",
                       "charter": "You inspect one repository, read-only.",
                       "description": "Inspects one repository.",
                       "cwd": "/tmp/work"})
    write(transcript, [assistant(
        "I'm hiring a per-repo inspector so I stay on ranking and reporting.\n"
        f"YOS_HIRE {hire}", uuid="u-c")])
    tick(harvester, surface, sessions)

    messages = thread_messages(client)
    # Two: the desk's sentence, and the deck's receipt telling this desk it now
    # has a report. The receipt is the fix for a manager that was never told --
    # measured, two desks came up under one and it never mentioned either.
    prose = [m for m in messages if m["author"] == DESK]
    receipts = [m for m in messages if m["author"] != DESK]
    assert len(prose) == 1, messages
    assert prose[0]["text"] == (
        "I'm hiring a per-repo inspector so I stay on ranking and reporting.")
    assert "YOS_HIRE" not in prose[0]["text"]
    assert len(receipts) == 1, messages
    assert "branch-scout" in receipts[0]["text"], receipts


def test_a_bare_marker_turn_says_nothing_to_the_owner(
    transcript, harvester, surface, sessions, client
):
    """Naming itself is not a message. A thread that opens with a JSON blob is
    the same unusable chat as an empty one."""
    body = json.dumps({"name": DESK, "label": "Drift",
                       "charter": "You own branch rot."})
    write(transcript, [assistant(f"YOS_DESK {body}", uuid="u-d")])
    tick(harvester, surface, sessions)
    assert thread_messages(client) == []


# -- 3. exactly once ---------------------------------------------------------

def test_the_same_transcript_polled_again_does_not_double_post(
    transcript, harvester, surface, sessions, client
):
    """The tick re-reads this file every second. A message that appears twice
    on every tick is worse than one that never appears at all."""
    write(transcript, [assistant(QUESTION)])
    for _ in range(4):
        tick(harvester, surface, sessions)

    messages = thread_messages(client)
    assert len(messages) == 1, f"posted {len(messages)} times: {messages!r}"


def test_a_restarted_daemon_does_not_repost_what_it_already_posted(
    transcript, roster_path, tmp_path, surface, sessions, client
):
    """The offsets file outlives the process; a fresh Harvester over the same
    offsets must not replay the transcript into the chat."""
    offsets = tmp_path / "harvest-offsets.json"
    write(transcript, [assistant(QUESTION)])
    Harvester(roster_path, offsets).poll(sessions)
    surface.refresh()
    Harvester(roster_path, offsets).poll(sessions)
    surface.refresh()

    assert len(thread_messages(client)) == 1


# -- 4. the daemon was briefly down ------------------------------------------

def test_words_said_while_the_daemon_was_down_arrive_when_it_returns(
    transcript, roster_path, tmp_path, surface, sessions, client
):
    offsets = tmp_path / "harvest-offsets.json"
    write(transcript, [assistant("First thing.", uuid="u-1")])
    Harvester(roster_path, offsets).poll(sessions)
    surface.refresh()

    # Daemon down. The desk keeps talking.
    append(transcript, assistant("Second thing, said while you were out.",
                                 uuid="u-2"))

    Harvester(roster_path, offsets).poll(sessions)
    surface.refresh()

    texts = [m["text"] for m in thread_messages(client)]
    assert texts == ["First thing.", "Second thing, said while you were out."]


# -- 5. one conversation, in order -------------------------------------------

def test_the_question_and_the_owners_answer_are_one_thread_in_order(
    transcript, harvester, surface, sessions, client
):
    """He answers through POST /v1/threads/direct:<name>/messages. The two
    halves must read as one conversation, not two interleaved logs."""
    write(transcript, [assistant(QUESTION)])
    tick(harvester, surface, sessions)

    posted = client.post(f"/v1/threads/direct:{DESK}/messages",
                         headers=auth(), json={"text": "Option 1. Go."})
    assert posted.status_code == 201, posted.text

    messages = thread_messages(client)
    assert [(m["author"], m["role"]) for m in messages] == [
        (DESK, "agent"), ("owner", "owner")]
    assert messages[1]["text"] == "Option 1. Go."
    assert messages[0]["cursor"] < messages[1]["cursor"]


def test_the_desks_reply_to_that_answer_lands_in_the_same_thread(
    transcript, harvester, surface, sessions, client
):
    """The other direction of the same class: he answered, it replied."""
    write(transcript, [assistant(QUESTION)])
    tick(harvester, surface, sessions)
    client.post(f"/v1/threads/direct:{DESK}/messages",
                headers=auth(), json={"text": "Option 1. Go."})

    append(transcript, assistant("Understood -- end to end it is.", uuid="u-3"))
    tick(harvester, surface, sessions)

    texts = [m["text"] for m in thread_messages(client)]
    assert texts == [QUESTION, "Option 1. Go.",
                     "Understood -- end to end it is."]


# -- a session with no desk speaks for nobody --------------------------------

def test_a_session_with_no_desk_posts_nothing(
    transcript, harvester, surface, client
):
    """Same rule the marker path already lives by: an unnamed session's words
    belong to no thread, so they must not invent one."""
    stranger = [{"session_id": "sid-stranger", "name": "stranger",
                 "transcript": str(transcript)}]
    write(transcript, [assistant("I am nobody in particular.", uuid="u-x")])
    harvester.poll(stranger)
    surface.refresh()

    res = client.get("/v1/threads/direct:stranger/messages", headers=auth())
    assert res.status_code == 404
