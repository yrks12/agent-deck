"""The deck must be able to address a desk it created.

Hiring through `POST /v1/agents/interview` opens a real Terminal window and a
real session, and then the deck could not say one word to it. A message to the
new hire's thread came back `{"ok": true, "delivered": false}`; the desk sat at
`OFFLINE` while its window was demonstrably alive; the harvester banked its
transcript without ever acting on it. All of that is ONE fault with one cause,
measured off the live session registry on 2026-09-01:

    desk on the roster ................ new-hire-511bba
    session Claude Code registered .... new-hire-511bba-59   ("nameSource":"derived")

`claude` names a session after its cwd and appends a disambiguator. The deck
never told it otherwise. Every join in this codebase is keyed on that name --

  * `roster.occupancy`  seats a desk only when a session's `name` equals it,
    so the desk read OFFLINE while its window was up;
  * `app._try_inject`   scans the cards for `name == target`, finds nothing,
    and that is exactly where `delivered: false` is produced;
  * `harvest.poll`      takes `actor = session["name"]` and drops the line
    unless the actor is a desk, so the new hire's own `YOS_DESK` -- the line
    that puts it on the board under its chosen name -- was never applied;
  * `hooks/cc-office.js` matches queued mail on `[session_id, session.name]`,
    so even the queue fallback could not reach it on its next turn.

The transport was never broken: pid 5114 had a live `messagingSocketPath`, and
injecting by *pid* worked the whole time. Only the name lookup was broken.

Two halves, both pinned below.

**Birth.** `spawn.build_argv` asks `claude` for the desk's own name (`--name`),
so the registry entry the CLI writes is the one the deck is going to look for.

**Rename.** A desk that names itself changes the roster; the running session
keeps the name it was born with, and `claude` cannot rename a live session. So
the rename ledger `onboard` already keeps has to be read on the way in --
`onboard.reseat` -- or the desk falls straight back to OFFLINE the moment it
succeeds at the one thing the interview asked it to do.

Every assertion here is on the GOOD signal: `delivered is True` and the bytes
on the socket, a seated desk with a pid, the roster actually carrying the new
name. Never on the absence of an error.
"""

import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import manager, office, onboard, roster, spawn
from server.harvest import Harvester
from server.roster import Desk, load_roster, save_roster

# The shape the interview door actually creates: a placeholder name and a
# workspace the deck allocated under it. This is the desk that could not be
# spoken to.
HIRED = Desk(
    name="new-hire-511bba",
    cwd="/Users/samcarter/.claude/agent-bus/workspaces/new-hire-511bba",
    engine="claude",
    mission="You have just been hired and nothing about you has been decided.",
    label="New",
)

SEED = "Ask the owner what this desk is for, then name yourself."


def registered_name(desk: Desk) -> str:
    """The name Claude Code will register for a session the deck spawns.

    Read off the argv the deck emits rather than assumed: this is the whole
    contract under test, and a test that hard-codes the name would still pass
    on the day the flag is dropped.
    """
    argv = spawn.build_argv(desk, background=False, seed=SEED)
    assert "--name" in argv, (
        "the deck does not tell `claude` what to call this session, so the "
        f"registry will name it after the cwd and no join can find it: {argv}"
    )
    return argv[argv.index("--name") + 1]


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """The office queue, redirected off ~/.claude."""
    messages = tmp_path / "messages.jsonl"
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", messages)
    return messages


@pytest.fixture
def client(bus):
    return TestClient(app_mod.app)


def _wait_for(received, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received:
            return True
        time.sleep(0.05)
    return False


# ── birth: the session carries the desk's name ─────────────────────────────


def test_the_deck_names_the_session_it_spawns_after_the_desk():
    """THE root detector. Without this every name-keyed join below misses."""
    assert registered_name(HIRED) == HIRED.name


def test_the_name_flag_is_one_claude_actually_advertises():
    """Anti-rot, read off the CLI on this machine -- the same guard
    `tests/test_spawn.py` puts on `--append-system-prompt` and `--bg`. A flag
    we invented would be rejected at spawn time, and the desk would never open
    at all."""
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("claude is not installed here; nothing to pin the argv against")
    try:
        done = subprocess.run([binary, "--help"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not run `claude --help`: {exc}")
    assert "--name" in (done.stdout or "") + (done.stderr or "")


def test_the_board_seats_the_session_the_deck_spawned(tmp_path):
    """The desk showed OFFLINE with its window up. Assert the good signal: a
    live state and the pid, so the board, routines and the harvester all have
    something to address."""
    card = {"session_id": "sid-hire", "name": registered_name(HIRED),
            "pid": 5114, "state": "WORKING", "cwd": HIRED.cwd}
    row = next(r for r in roster.occupancy([HIRED], [card]) if r.get("desk"))
    assert row["state"] == "WORKING"
    assert row["pid"] == 5114
    assert row["session_id"] == "sid-hire"


def test_the_deck_delivers_to_a_desk_it_just_spawned(
    client, bus, fake_socket, monkeypatch
):
    """`delivered: false` in one test. The owner answers the new hire's
    question through the deck; the bytes have to reach the session's socket."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    monkeypatch.setitem(app_mod._state, "sessions", [
        {"session_id": "sid-hire", "name": registered_name(HIRED), "pid": pid,
         "cwd": HIRED.cwd},
    ])

    res = client.post("/api/message",
                      json={"to": HIRED.name, "text": "Option 3 -- watch and report."})

    assert res.status_code == 200
    assert res.json()["delivered"] is True
    assert _wait_for(received), "nothing reached the new hire's socket"
    # His answer, under his mark. A brand-new hire is exactly the desk with no
    # history to fall back on, so the frame saying it is him -- not a peer
    # relaying for him -- matters most on the very first message it gets.
    assert json.loads(received[0].decode())["message"]["content"] == \
        office.attribute("Option 3 -- watch and report.", "owner")


def test_the_harvester_hears_the_session_the_deck_spawned(tmp_path):
    """The other half of `delivered: false`: the return path. A new hire's
    `YOS_DESK` line is what puts it on the board under its own name, and the
    harvester drops it unless the session's name is a desk."""
    roster_path = tmp_path / "roster.json"
    save_roster(roster_path, [HIRED])
    transcript = tmp_path / "hire.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{
            "type": "text",
            "text": "YOS_DESK " + json.dumps({
                "name": "pr-watch", "label": "Monitor",
                "charter": "You watch the owner's open pull requests.",
            }),
        }]},
    }) + "\n")

    harvester = Harvester(roster_path, tmp_path / "offsets.json")
    applied = harvester.poll([{
        "session_id": "sid-hire", "name": registered_name(HIRED),
        "cwd": HIRED.cwd, "transcript": str(transcript),
    }])

    assert [r["result"] for r in applied] == ["patched"]
    assert [d.name for d in load_roster(roster_path)] == ["pr-watch"]


# ── rename: the seat follows the desk ──────────────────────────────────────


def test_a_desk_that_named_itself_keeps_the_session_sitting_at_it(tmp_path):
    """The step straight after the deliverable. The roster now says `pr-watch`
    and the running session is still called `new-hire-511bba` -- `claude` has
    no way to rename a live session. Without reading the rename ledger the
    desk goes dark again the instant it succeeds."""
    named = Desk(**{**HIRED.__dict__, "name": "pr-watch"})
    aliases = {HIRED.name: "pr-watch"}
    cards = [{"session_id": "sid-hire", "name": registered_name(HIRED),
              "pid": 5114, "state": "WORKING", "cwd": HIRED.cwd}]

    onboard.reseat(cards, aliases)

    assert cards[0]["name"] == "pr-watch"
    row = next(r for r in roster.occupancy([named], cards) if r.get("desk"))
    assert row["state"] == "WORKING"
    assert row["pid"] == 5114


def test_reseating_leaves_a_session_that_never_renamed_alone():
    """The rest of the board is not the deck's to rewrite."""
    cards = [{"name": "acme-os-6f", "pid": 1}, {"name": "pr-watch", "pid": 2}]
    onboard.reseat(cards, {"new-hire-511bba": "pr-watch"})
    assert [c["name"] for c in cards] == ["acme-os-6f", "pr-watch"]


def test_the_deck_delivers_to_a_desk_after_it_named_itself(
    client, bus, fake_socket, monkeypatch
):
    """End to end on the far side of the rename: the owner's next message goes
    to the name the agent chose, and reaches the session born under the
    placeholder."""
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    cards = [{"session_id": "sid-hire", "name": registered_name(HIRED),
              "pid": pid, "cwd": HIRED.cwd}]
    onboard.reseat(cards, {HIRED.name: "pr-watch"})
    monkeypatch.setitem(app_mod._state, "sessions", cards)

    res = client.post("/api/message", json={"to": "pr-watch", "text": "status?"})

    assert res.json()["delivered"] is True
    assert _wait_for(received), "nothing reached the renamed desk's socket"
    assert json.loads(received[0].decode())["message"]["content"] == \
        office.attribute("status?", "owner")


# ── the question the owner can answer ──────────────────────────────────────


def test_the_interview_forbids_a_question_only_a_keyboard_can_answer():
    """Fault A. The new hire asked its question in Claude Code's interactive
    menu widget -- arrow keys, Enter to select. Nobody is at that window; the
    owner answers from the deck, and an injected message cannot press a key.
    The instruction has to say so, because the output is not ours to
    post-process."""
    text = onboard.interview_prompt("Watch my open PRs", "Revenue is the point.")
    low = text.lower()
    assert "plain prose" in low, f"the interview never says how to ask:\n{text}"
    for banned in ("interactive", "menu", "keypress"):
        assert banned in low, f"the interview does not rule out a {banned}:\n{text}"


def test_the_interview_still_offers_the_numbered_shapes_in_prose():
    """Forbidding the widget must not cost the list. The shapes are what let
    the owner answer with one word instead of writing a brief -- they just
    have to be typed out, not rendered."""
    import re
    text = onboard.interview_prompt("Watch my open PRs", "Revenue is the point.")
    numbered = [ln for ln in text.splitlines() if re.match(r"\s*\d+[.)]\s", ln)]
    assert len(numbered) >= 3, f"the concrete shapes are gone:\n{text}"


# ── the wiring: the tick is where the reseat has to happen ─────────────────


def _stub(**methods):
    return type("Stub", (), {k: (lambda self, _v=v: _v) for k, v in methods.items()})()


def test_the_collectors_tick_reseats_a_desk_that_named_itself(tmp_path, monkeypatch):
    """`reseat` fixes nothing until the collector calls it. The tick is the
    single point every consumer reads through -- the desk join, the cards
    `_try_inject` scans, the board `office.publish` writes for the office hook,
    and the list the harvester is handed -- so one call there is the whole fix.

    Pinned on the snapshot, not on the function: a helper nothing calls is how
    this fault survived a whole build cycle in the first place.
    """
    from server import collector as collector_mod
    from server.sources.sessions import RawSession

    monkeypatch.setattr(office, "publish", lambda cards: None)
    monkeypatch.setattr(office, "pending_counts", lambda: {})
    monkeypatch.setattr(office, "toplevel_for", lambda cwd: "")
    monkeypatch.setattr(office, "branch_for", lambda toplevel: "")

    roster_path = tmp_path / "roster.json"
    save_roster(roster_path, [Desk(**{**HIRED.__dict__, "name": "pr-watch"})])
    (tmp_path / onboard.ALIASES_FILENAME).write_text(
        json.dumps({"version": 1, "renames": {HIRED.name: "pr-watch"}}))

    born_as = registered_name(HIRED)
    card = {"session_id": "sid-hire", "pid": 5114, "name": born_as,
            "cwd": HIRED.cwd, "state": "WORKING", "git_branch": "",
            "agents_running": 0, "usage": {"output": 0}}

    collector = collector_mod.Collector()
    collector.roster_path = roster_path
    collector.scanner = _stub(scan=[RawSession(
        pid=5114, session_id="sid-hire", cwd=HIRED.cwd, name=born_as,
        status="busy", kind="interactive", started_at=0)])
    collector.opencode = _stub(scan=[])
    collector.bus = _stub(poll={})
    collector.usage = _stub(poll={})
    collector.opencode_usage = _stub(poll={})
    collector._build = lambda session, signals, now: card

    snapshot = collector.tick()

    assert [c["name"] for c in snapshot["sessions"]] == ["pr-watch"], \
        "the card still carries the name the session was born with"
    seat = next(d for d in snapshot["desks"] if d["name"] == "pr-watch")
    assert seat["state"] == "WORKING"
    assert seat["pid"] == 5114
