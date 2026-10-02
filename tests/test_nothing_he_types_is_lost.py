"""Two defects the owner hit in one sitting, both about a message going quiet.

    "why is my messages getting disappeared?"   "why am i idle?"

**Defect 1 -- the opening instruction is never written down.** MEASURED on the
box (10.99.0.1, `/home/deckop/.claude/agent-bus/messages.jsonl`, 2026-09-06):
desk `new-hire-82d9ab` holds exactly two records, and the first of them is the
agent asking `What do you want me to actually do with "what's going on across
projec...`. The words it is quoting are the owner's, they reached the session,
and they are in no record anywhere. `Surface.interview_agent` builds them into
`onboard.interview_prompt` and hands the result to `spawn.start(..., seed=...)`.
That is a delivery. Nothing on that path is a *recording*, so the first thing he
ever said to that desk is not in the conversation he is looking at.

The fix is not "send it as a message": the seed already carries it into the
session, and queueing it unacked would have `hooks/cc-office.js` read it back to
the desk on its first turn as though he had said it twice. It is recorded AND
acked in the same breath -- present in the thread, spent for delivery.

**Defect 2 -- a message that misses the socket is lost on the box.** MEASURED,
same box, same night:

    /home/deckop/.claude/settings.json               -> 0 matches for cc-office
    /home/deckop/.claude/settings.local.json         -> does not exist
    /home/deckop/.claude/agent-bus/approve-settings.json
        hooks: ['Elicitation', 'PermissionRequest', 'PostCompact', 'PreToolUse']

`hooks/cc-office.js` is the ONLY reader of the office queue, and it runs on
`UserPromptSubmit`/`SessionStart`. Registered nowhere on that machine, so a
message that could not go down a desk's socket was written into a file nothing
would ever read. `delivered: false` -- documented in `docs/client-api.md` as
"queued, not lost" -- meant *lost*, and the desk sat IDLE with the owner told
nothing. It works on his Mac only because his personal `~/.claude/settings.json`
happens to carry the hook; the deck must not depend on that.

THE GOOD SIGNAL, asserted throughout, never the absence of a bad one:

  * his opening words ARE in the thread, exactly once, authored by him;
  * `cc-office.js` IS named in the document `approval.install()` writes, on the
    two events that deliver mail;
  * an undelivered message IS reported as undelivered, in prose a client can
    put on the screen;
  * every door that delivers to a desk leaves the record IN the queue when the
    socket is not there -- so the hook, now registered, will read it.

Hermetic: tmp bus, tmp roster, no spawn, no window, no node.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import approval, groups, office, roster, routines, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

#: What he typed into the "+" door. The box's own case, shortened.
OPENING = "what's going on across projects"

CHIEF = {"name": "chief", "cwd": "/tmp", "engine": "claude",
         "mission": "run it", "label": "Negotiator", "charter": "Own the deal.",
         "reports_to": None}

VOUCHED = {"ok": True, "reason": "trusted", "detail": "", "muted": []}


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
def snapshot():
    return {"generated_at": 1_756_000_100.0, "sessions": [{
        "session_id": "sid-chief", "pid": 4242, "name": "chief", "cwd": "/tmp",
        "project": "p", "state": "WORKING", "state_since": 1_756_000_000.0,
    }]}


@pytest.fixture
def spawned(monkeypatch):
    """Every spawn recorded instead of performed, on BOTH channels -- the box
    takes the headless one and that is the install this file is about."""
    calls: list[dict] = []

    def fake(desk, *, seed="", **kw):
        calls.append({"desk": desk, "seed": seed, **kw})
        # `agent_id` because the headless launcher returns one and `spawn.start`
        # reads it; a stub that omitted it would describe a spawner that cannot
        # exist and the red below would be the stub's, not the door's.
        return {"ok": True, "detail": "stubbed", "agent_id": "0b697cee",
                "pretrust": VOUCHED}

    monkeypatch.setattr(api_mod.spawn, "choose_channel", lambda **kw:
                        spawn.Channel("background", "no_osascript", "no gui"))
    monkeypatch.setattr(api_mod.spawn, "spawn_terminal", fake)
    monkeypatch.setattr(api_mod.spawn, "spawn_background", fake)
    return calls


@pytest.fixture
def deaf():
    """A desk with no live socket: every delivery attempt fails. The box's
    ordinary state, and the state `delivered: false` is supposed to describe."""
    return lambda name, text: False


@pytest.fixture
def surface(bus, roster_file, snapshot, deaf):
    return api_mod.Surface(snapshot=lambda: snapshot,
                           comms=comms_mod.CommsIndex(), deliver=deaf,
                           roster_path=roster_file,
                           prefs_path=bus / "agent_prefs.json",
                           groups_path=bus / "groups.json")


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def queued_for(name):
    """Records still waiting in the office queue for `name` -- what the hook
    would read on that desk's next turn."""
    out = []
    acked = set()
    for line in (office.MESSAGES_FILE.read_text().splitlines()):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("ack"):
            acked.add(rec["ack"])
        elif rec.get("to") == name:
            out.append(rec)
    return [r for r in out if r["id"] not in acked]


# ── defect 1: the opening instruction is written down ──────────────────────


def test_the_opening_instruction_is_in_the_thread_exactly_once(
    client, tmp_path, spawned
):
    """The whole complaint. He typed it, the agent answered it, and the
    conversation he reopened did not contain it."""
    body = client.post("/v1/agents/interview",
                       json={"role_hint": OPENING, "cwd": str(tmp_path)},
                       headers=auth()).json()

    page = client.get(f"/v1/threads/{body['thread_id']}/messages",
                      headers=auth()).json()
    his = [m for m in page["messages"] if m["role"] == "owner"]

    assert [m["text"] for m in his] == [OPENING]
    assert his[0]["author"] == api_mod.OWNER


def test_the_opening_instruction_is_recorded_but_not_delivered_a_second_time(
    client, tmp_path, spawned
):
    """Recorded AND spent. The seed already put these words in front of the
    session; leaving the record unacked would have `hooks/cc-office.js` read
    them back on the desk's first turn as a second instruction from him."""
    body = client.post("/v1/agents/interview",
                       json={"role_hint": OPENING, "cwd": str(tmp_path)},
                       headers=auth()).json()

    assert queued_for(body["name"]) == []


def test_the_seed_still_carries_what_he_typed(client, tmp_path, spawned):
    """The seed is still the seed. Recording it must not have replaced the one
    delivery that actually reaches the new session."""
    client.post("/v1/agents/interview",
                json={"role_hint": OPENING, "cwd": str(tmp_path)},
                headers=auth())

    assert OPENING in spawned[0]["seed"]


def test_a_door_opened_with_no_instruction_records_nothing(
    client, tmp_path, spawned
):
    """He is allowed to open the door and say nothing. An empty record in his
    thread is a message he did not send."""
    body = client.post("/v1/agents/interview", json={"role_hint": "  ",
                                                     "cwd": str(tmp_path)},
                       headers=auth()).json()

    page = client.get(f"/v1/threads/{body['thread_id']}/messages",
                      headers=auth()).json()
    assert [m for m in page["messages"] if m["role"] == "owner"] == []


# ── defect 2a: the hook that reads the queue is installed by the deck ──────


MAIL_EVENTS = ("UserPromptSubmit", "SessionStart")


@pytest.mark.parametrize("event", MAIL_EVENTS)
def test_every_hired_desk_gets_the_office_hook_on_the_mail_events(event):
    """`hooks/cc-office.js` is the only reader of the office queue, and it
    reads on these two events. MEASURED on the box: registered on neither, in
    any scope, so every queued message there was written to nobody."""
    document = approval.settings_document("/usr/bin/node")

    commands = [h["command"]
                for entry in document["hooks"][event]
                for h in entry["hooks"]]
    assert any("cc-office.js" in c for c in commands), commands


def test_the_office_hook_the_deck_registers_is_a_real_file(bus, monkeypatch):
    """A settings file naming a script that is not there registers a hook whose
    every invocation is a failed spawn -- `install()` already refuses that for
    its other four, and the fifth joins the same guard."""
    monkeypatch.setattr(approval, "SETTINGS_PATH", bus / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", bus / "events.jsonl")

    assert approval.OFFICE_HOOK.is_file()

    result = approval.install()
    assert result.ok, result.detail
    written = json.loads((bus / "approve-settings.json").read_text())
    assert any("cc-office.js" in h["command"]
               for entry in written["hooks"]["UserPromptSubmit"]
               for h in entry["hooks"])


# ── defect 2b: undelivered is visible ──────────────────────────────────────


def test_a_message_that_missed_the_socket_is_reported_undelivered(client):
    """`delivered: false` and `delivered: true` looked identical on his screen.
    The response now carries something he can be shown."""
    response = client.post("/v1/threads/direct:chief/messages",
                           json={"text": "ship it"}, headers=auth())
    note = response.json()["delivery"]

    assert note["state"] == "queued"
    assert note["waiting"] == ["chief"]
    assert note["reached"] == []
    assert "chief" in note["what"]


def test_a_delivered_message_says_delivered(surface, client, monkeypatch):
    """The other half. A client that renders `delivery` must not mark a message
    that really landed as waiting."""
    monkeypatch.setattr(surface, "_deliver", lambda name, text: True)

    note = client.post("/v1/threads/direct:chief/messages",
                       json={"text": "ship it"}, headers=auth()).json()["delivery"]

    assert note["state"] == "delivered"
    assert note["reached"] == ["chief"]
    assert note["waiting"] == []


def test_a_group_message_says_which_desks_are_still_waiting(client):
    """Same vocabulary on the fan-out. A group where nobody is seated is the
    common case on the box, and `queued: [...]` alone said nothing about it."""
    client.post("/v1/groups", json={"name": "all-hands", "members": ["chief"]},
                headers=auth())

    note = client.post("/v1/threads/group:all-hands/messages",
                       json={"text": "standup"}, headers=auth()).json()["delivery"]

    assert note["state"] == "queued"
    assert note["waiting"] == ["chief"]
    assert "chief" in note["what"]


# ── defect 2c: the class of doors, not the one that was found ──────────────


def test_every_door_that_delivers_to_a_desk_leaves_the_record_in_the_queue(
    surface, client, bus, monkeypatch
):
    """The sweep. Four doors write to a desk, and a socket that is not there
    must cost the *delivery*, never the *record* -- the hook is what picks it
    up afterwards, and it can only pick up what is still queued.

    `api._resume_desk` is the fifth and takes the identical shape (queue with
    `office.send`, deliver, ack only on success); it needs a settled ask to
    reach, so it is covered by `tests/test_answer_resumes_the_desk.py`.
    """
    client.post("/v1/groups", json={"name": "all-hands", "members": ["chief"]},
                headers=auth())
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: False)
    monkeypatch.setattr(app_mod, "_canonical_agent", lambda name: name)

    surface.send("direct:chief", "one")
    surface.send_to_group("all-hands", "two")
    app_mod._deliver_routine(routines.Routine(
        id="r1", agent="chief", prompt="three", trigger={"cron": "0 9 * * *"}))
    groups.broadcast(groups.find(groups.load_groups(bus / "groups.json"),
                                 "all-hands"), "four",
                     sender=api_mod.OWNER, deliver=lambda n, t: False)

    assert sorted(r["text"] for r in queued_for("chief")) == [
        "four", "one", "three", "two"]
