"""The brief is said; this is what makes the model DO it (B-2, B-3, B-4, B-6).

THE DEFECT. The K7 brief ("How you sound") reached atlas's system prompt word
for word -- and atlas still acknowledged nothing, wrote 5-8 sentence replies
with bullets, and offered "two options ... Pick one by number" as prose.
MEASURED on the box, atlas session 20573e47 (2026-09-30) and a throwaway desk
hired to repeat the probes as the owner:

1. The deck tools did not exist when the turn began. Claude Code defers MCP
   tools behind ToolSearch and connects MCP servers without blocking start:
   `mcp__deck__say` first appeared in a `deferred_tools_delta` MID-TURN, after
   the desk's first Bash call (atlas: 10 minutes after start; the throwaway
   desk: after its first tool call). An acknowledgement that needs a
   ToolSearch round trip first is one the model does not send. The CLI's own
   remedy is the server field `alwaysLoad: true`: tools always in the prompt,
   startup waits for the connection (5 s cap).
2. `ask` was scoped to three escalations (money / send / plan), and the brief
   gave a numbered list as the fallback. "Give me two options and let me pick"
   is none of the three, so the desk took the fallback, verbatim.
3. The only reply-shape instruction is stated once, 17 000 characters into the
   system prompt; every turn then carries ~700 characters of trust framing and
   nothing about length -- and a restarted desk is seeded with up to 24 000
   characters of its OWN old multi-paragraph replies, the strongest few-shot
   in its context. So the answer rides with his message, per turn, the way
   `ENVELOPE_NOTE` already does, and the replay says not to copy its register.

And the probes themselves were invalid on real desks: the engineer mark says
"not a task", which is right for atlas and useless for acceptance. A desk
flagged `test` gets a mark that lets the engineer's probe be treated as a task
-- still without owner authority -- and no real desk can ever receive it.

Hermetic: tmp dirs only; no session is started.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from server import deck_mcp, hire, office, spawn
from server.roster import Desk, load_roster, save_roster

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-office.js"

CHIEF = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m",
             label="COS", charter="Run the company for Sam.")
PROBE = Desk(name="probe-chief", cwd="/tmp", engine="claude", mission="m",
             label="Probe", charter="Test desk.", test=True)


# -- 1. the deck tools are there when the turn starts ------------------------


def _servers(desk: str = "atlas") -> dict:
    return json.loads(deck_mcp.config(desk))["mcpServers"]


def test_the_deck_tools_are_never_deferred():
    """`say` must be callable on the first request, not after a ToolSearch."""
    assert _servers()[deck_mcp.NAME].get("alwaysLoad") is True


def test_only_the_deck_server_is_forced_into_the_prompt():
    """The computer's six tools stay deferred: nothing needs them in the first
    second of a turn, and every always-loaded schema is context on every
    request."""
    assert "alwaysLoad" not in _servers()["computer"]


def test_the_installed_cli_still_knows_always_load():
    """A drift guard on somebody else's field name: if Anthropic renames it,
    the flag silently stops working and every test above stays green."""
    root = Path(os.environ.get("DECK_CLI_BINARY") or
                Path.home() / ".local" / "share" / "claude" / "versions")
    builds = ([root] if root.is_file() else
              sorted(p for p in root.iterdir() if p.is_file())
              if root.is_dir() else [])
    if not builds:
        pytest.skip("no Claude Code bundle installed to measure against")
    assert b"alwaysLoad" in builds[-1].read_bytes()


# -- 2. the brief says when, concretely --------------------------------------


def test_say_has_a_trigger_the_model_can_see():
    """"More than a few seconds" is a forecast the model cannot make; "before
    your first tool call" is an event it can."""
    assert "first tool call" in hire.HOW_YOU_SOUND


def test_a_choice_he_asked_for_is_an_ask_not_a_list():
    lower = hire.HOW_YOU_DECIDE.lower()
    assert "asks you to pick" in lower
    assert "never a numbered list" in lower


def test_the_ask_tool_itself_is_not_scoped_to_escalations_only():
    ask = next(t for t in deck_mcp.TOOLS if t["name"] == "ask")
    assert "asks to pick" in ask["description"].lower()


def test_the_no_invention_rule_is_not_a_sentence_every_reply():
    """Measured: 7 of 10 probe replies spent a sentence on "I'm not going to
    make any up". The rule stays; the announcement goes."""
    assert "don't announce it" in hire.HOW_YOU_SOUND.lower()


# -- 3. the reply shape rides with his message, every turn -------------------


def test_his_typed_message_carries_the_reply_shape():
    turn = office.attribute("status of vilsa?", "owner")
    assert office.REPLY_SHAPE in turn
    assert turn.index("status of vilsa?") < turn.index(office.REPLY_SHAPE)


def test_the_reply_shape_names_the_four_moves():
    lower = office.REPLY_SHAPE.lower()
    for move in ("`say`", "three", "`ask`", "who"):
        assert move in lower, move


@pytest.mark.parametrize("sender", ["deck", "routine", "engineer", "sid-peer"])
def test_only_his_own_words_get_the_reply_shape(sender):
    """A receipt from the deck or a peer's status ping is not him waiting."""
    assert office.REPLY_SHAPE not in office.attribute("x", sender, who="p")


def test_the_replay_says_not_to_copy_its_own_register():
    assert "register" in spawn.RESEAT_LEAD
    assert "How you sound" in spawn.RESEAT_LEAD


# -- 4. the engineer on a test desk -------------------------------------------


def test_the_test_engineer_mark_makes_it_a_task_without_his_authority():
    mark = office.ENGINEER_TEST_MARK
    assert mark.startswith(office.MARK)
    assert "test desk" in mark.lower()
    assert "task" in mark.lower()
    assert office.OWNER_AUTHORITY not in mark
    assert not office.is_owner(office.ENGINEER_TEST)
    assert office.mark_for(office.ENGINEER_TEST) == mark


def test_the_test_engineer_gets_the_reply_shape_like_him():
    """It is standing in for him on a desk that exists to be measured."""
    assert office.REPLY_SHAPE in office.attribute("hi", office.ENGINEER_TEST)


def test_only_a_test_desk_is_told_about_the_test_mark():
    assert office.ENGINEER_TEST_MARK in hire.brief(PROBE)
    assert office.ENGINEER_TEST_MARK not in hire.brief(CHIEF)


def test_the_test_flag_round_trips_and_defaults_off(tmp_path):
    path = tmp_path / "roster.json"
    save_roster(path, [CHIEF, PROBE])
    loaded = {d.name: d for d in load_roster(path)}
    assert loaded["probe-chief"].test is True
    assert loaded["atlas"].test is False


def test_hire_can_flag_a_test_desk(tmp_path):
    path = tmp_path / "roster.json"
    desk = hire.hire(path, name="probe-chief", label="Probe", charter="t",
                     cwd=str(tmp_path), engine="claude", reports_to=None,
                     test=True)
    assert desk.test is True
    assert load_roster(path)[0].test is True


# -- 5. the queued path says the same as the socket path ---------------------


def _hook(tmp_path: Path, records: list[dict]) -> str:
    home = tmp_path / "claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    now = time.time()
    (bus / "office.json").write_text(json.dumps({
        "generated_at": now,
        "sessions": {"sid-desk": {"name": "atlas", "cwd": "/srv/w",
                                  "toplevel": "/srv/w", "branch": "main",
                                  "state": "IDLE",
                                  "address": "uds:/tmp/1.sock"}}}))
    with (bus / "messages.jsonl").open("w") as fh:
        for record in records:
            fh.write(json.dumps({"ts": now, "to": "sid-desk", **record}) + "\n")
    done = subprocess.run(
        ["node", str(HOOK)], capture_output=True, text=True, timeout=10,
        input=json.dumps({"session_id": "sid-desk",
                          "hook_event_name": "UserPromptSubmit"}),
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(home)})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_hook_carries_the_reply_shape_for_him_only(tmp_path):
    seen = _hook(tmp_path, [{"id": "m1", "from": "owner", "text": "hello"},
                            {"id": "m2", "from": "drift-watch", "text": "hi"}])
    assert seen.count(office.REPLY_SHAPE) == 1
    assert seen.index("hello") < seen.index(office.REPLY_SHAPE) < seen.index(
        office.peer_mark("drift-watch"))


def test_the_hook_frames_the_test_engineer_byte_equal(tmp_path):
    seen = _hook(tmp_path, [{"id": "m1", "from": office.ENGINEER_TEST,
                             "text": "status of vilsa?"}])
    assert office.ENGINEER_TEST_MARK in seen.splitlines()
    assert office.REPLY_SHAPE in seen
    assert office.OWNER_AUTHORITY not in seen


# -- 6. the API is the only door, and it checks the roster -------------------


TOKEN = "t-secret-not-a-real-credential"


def _api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import api as api_mod
    from server.sources import comms as comms_mod

    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    save_roster(tmp_path / "roster.json", [CHIEF, PROBE])
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=tmp_path / "roster.json",
        prefs_path=tmp_path / "prefs.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def _post(client, desk, sender):
    return client.post(f"/v1/threads/direct:{desk}/messages",
                       headers={"Authorization": f"Bearer {TOKEN}"},
                       json={"text": "status of vilsa?", "as": sender})


def test_the_engineer_on_a_test_desk_is_recorded_as_the_test_engineer(
        tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    r = _post(client, "probe-chief", "engineer")
    assert r.status_code == 201, r.text
    record = json.loads((tmp_path / "messages.jsonl").read_text().splitlines()[0])
    assert record["from"] == office.ENGINEER_TEST
    assert r.json()["message"]["role"] == "system"
    assert r.json()["message"]["thread_id"] == "direct:probe-chief"


def test_the_engineer_on_a_real_desk_is_still_only_the_engineer(
        tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    assert _post(client, "atlas", "engineer").status_code == 201
    record = json.loads((tmp_path / "messages.jsonl").read_text().splitlines()[0])
    assert record["from"] == office.ENGINEER


def test_the_test_engineer_cannot_be_named_over_http(tmp_path, monkeypatch):
    """The roster decides; a caller cannot ask for the stronger mark."""
    client = _api(tmp_path, monkeypatch)
    assert _post(client, "atlas", office.ENGINEER_TEST).status_code == 400


def test_the_reply_shape_keeps_the_typo_guess_first():
    """MEASURED after the first cut: "answer in the first word" alone pushed
    the guess behind the answer -- "I don't know. I'm assuming you mean
    listing-closer" -- and B-6 scores the FIRST sentence. The guess leads, and
    the way out is one clause."""
    shape = office.REPLY_SHAPE
    assert "Assuming you mean" in shape
    assert "if not, say which" in shape
    assert shape.index("Assuming you mean") < shape.index("first word")
