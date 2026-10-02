"""A desk that names itself must still be reachable by the junior it hires.

**The defect this pins (D3).** `spawn.build_argv` sets `--name` once, on the
command line, and `claude --help` (2.1.252) calls that flag a *display name* set
at start; there is no `rename` command and `claude agents` has no rename option.
So a desk that does the one thing the interview asked of it -- name itself --
leaves Claude Code's own session registry saying the old name for as long as the
process lives. Measured from disk: `~/.claude/sessions/60832.json` still read
`"name":"new-hire-e6a764"` a full minute after the ledger recorded the rename to
`drift-watch`.

`onboard.reseat` already fixes the *deck's* view, and that is enough for
everything the deck delivers itself. It cannot help agent-to-agent traffic,
because `SendMessage` resolves its `to` inside the CLI, against the CLI's
registry, with the deck nowhere in the call. Measured live: `branch-scout` was
told to report to `drift-watch`, ran `ListAgents`, and got back *"no reachable
session named `drift-watch`. The 35 peers listed include nothing by that name
(closest live sessions are `new-hire-e6a764`, ...)"*.

**So the fault is not in the junior, and not in `ListAgents`.** It is in
`hire.brief()`: the deck's own sentence telling a new hire who its boss is hands
over `desk.reports_to` -- a display name -- as if it were an address, and the
deck is the very thing that lets that name change. `build_argv` puts that
sentence in `--append-system-prompt`, which is re-sent on every request and
cannot be compacted away, so a wrong address is burned in for the life of the
session.

**The fix, and why this one.** Address the boss by something stable and keep the
name as a label. Measured, not chosen from the air: `SendMessage` accepts
`to: "uds:/tmp/cc-socks/<pid>.sock"` -- 17,167 such sends across this machine's
transcripts -- and `~/.claude/sessions/<pid>.json` publishes that path as
`messagingSocketPath`. `ListAgents` says so itself, printing every peer as
`name [id]` under the line "the name other sessions use to message it". A socket
address cannot go stale on a rename, because a rename does not touch it.

**The GOOD signal: arrival.** Not "the brief mentions an address", and certainly
not "no error was raised" -- the live failure raised nothing either, it just
answered that nobody was there. These take the address the deck burned into the
junior's system prompt, write to it, and assert the boss's own socket received
the bytes.

Hermetic: tmp roster, tmp office board, a real AF_UNIX socket in a tmp dir.
Nothing spawns and nothing reads ~/.claude.
"""

import dataclasses
import json
import re
import socket
import time

import pytest

from server import approval, manager, office, roster, spawn

BOSS_PID = 424242
#: What `claude --name` was given when the boss started, and what the CLI's
#: registry still says. The roster has moved on; this has not.
REGISTRY_NAME = "new-hire-e6a764"
#: What the desk called itself, and the only name the junior is ever told.
CHOSEN_NAME = "drift-watch"


@pytest.fixture
def board(tmp_path, short_tmp, fake_socket, monkeypatch):
    """The deck as it stands one tick after the boss renamed itself.

    `fake_socket` binds a real `<dir>/<pid>.sock` and records every byte written
    to it -- the same shape Claude Code binds per session.
    """
    sock_dir, pid, received = fake_socket
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(office, "toplevel_for", lambda cwd: cwd)
    monkeypatch.setattr(office, "branch_for", lambda top: "")

    # The collector's tick, after `onboard.reseat`: the card carries the name
    # the desk goes by NOW and the pid of the process still registered under
    # the old one. That divergence IS the defect.
    office.publish([{"session_id": "sid-boss", "name": CHOSEN_NAME,
                     "cwd": str(tmp_path), "pid": pid, "state": "IDLE",
                     "git_branch": ""}])
    return tmp_path, received


@pytest.fixture
def junior(board):
    tmp_path, _ = board
    workspace = tmp_path / "workspaces" / "branch-scout"
    workspace.mkdir(parents=True)
    return roster.Desk(name="branch-scout", cwd=str(workspace),
                       engine="claude", mission="",
                       label="branch", charter="Watch the unmerged branches.",
                       reports_to=CHOSEN_NAME)


#: Both channels a junior can be hired through, and what each one's own binary
#: prints. `spawn_background` refuses `no_agent_id` without an id it can read.
#:
#: PARAMETRIZED, not pinned to the Mac. Whether the junior is told a reachable
#: address for its boss has nothing to do with Terminal.app -- and the box is
#: the install where the headless channel is the one EVERY hire takes. Pinning
#: this file to `terminal` would have left the only machine that actually hires
#: with no test that its juniors can reach anybody, which is exactly the
#: blindness D3 was about in the first place.
CHANNELS = [
    (spawn.Channel("terminal", "gui_session", "a logged-in Mac"), "tab 3"),
    (spawn.Channel("background", "no_osascript", "no Terminal.app"),
     "backgrounded · 0b697cee · branch-scout\n"),
]


@pytest.fixture(params=CHANNELS, ids=[c.name for c, _ in CHANNELS])
def brief_from_spawn(request, monkeypatch, osascript_is_on_path):
    """`(desk, roster_path) -> the command line the hire was started with`.

    Starts the junior for real, bar the one call that would open a window or
    launch a real agent. The command line is JOINED rather than indexed, so one
    reader serves both channels: on the terminal channel argv is
    `["osascript", "-e", <script>]` with the desk's command line inside the
    script, and on the headless channel argv IS the desk's command line.
    """
    channel, stdout = request.param

    class _Started:
        returncode, stderr = 0, ""

    _Started.stdout = stdout
    commands: list[str] = []

    monkeypatch.setattr(
        spawn.subprocess, "run",
        lambda argv, **kw: commands.append(
            " ".join(str(a) for a in argv)) or _Started())

    def start(desk, roster_path) -> str:
        commands.clear()
        spawn.start(desk, roster_path=roster_path, channel=channel)
        assert commands, "nothing was spawned"
        return commands[0]

    return start


def address_in(text: str) -> str:
    found = re.findall(r"uds:[^\s'\"\\]+", text)
    assert found, f"the junior was given no address to reach its boss:\n{text}"
    assert len(set(found)) == 1, f"more than one address offered: {set(found)}"
    return found[0]


# -- the detector ------------------------------------------------------------


def test_a_message_to_the_address_in_the_brief_arrives_at_the_renamed_boss(
    board, junior, brief_from_spawn
):
    """THE detector for D3. The junior is told one thing about how to reach its
    boss; that thing has to work after the rename that the interview door makes
    every hire perform."""
    tmp_path, received = board
    address = address_in(brief_from_spawn(junior, tmp_path / "roster.json"))

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(address.removeprefix("uds:"))
        client.sendall(manager.frame("branch-scout reporting in"))

    for _ in range(100):
        if received:
            break
        time.sleep(0.02)  # the listener records once the connection closes
    assert received, "the boss never received anything"
    assert b"branch-scout reporting in" in received[0]


def test_the_address_is_the_process_not_the_name(board, junior,
                                                 brief_from_spawn):
    """Why this removes the failure instead of compensating for it. The address
    is derived from the live process, so the desk can rename itself again --
    twice, ten times -- and every junior already briefed still reaches it."""
    tmp_path, _ = board
    before = address_in(brief_from_spawn(junior, tmp_path / "roster.json"))

    office.publish([{"session_id": "sid-boss", "name": "drift-and-watch",
                     "cwd": str(tmp_path), "pid": BOSS_PID, "state": "IDLE",
                     "git_branch": ""}])
    after = address_in(brief_from_spawn(
        dataclasses.replace(junior, reports_to="drift-and-watch"),
        tmp_path / "roster.json"))

    assert before == after


def test_the_name_is_still_there_as_a_label(board, junior, brief_from_spawn):
    """The name stays -- it is what the boss is *called*, and the sentence that
    names it is the one thing stopping every desk escalating to the owner. What
    changes is that it is no longer the only thing offered as an address."""
    tmp_path, _ = board
    assert CHOSEN_NAME in brief_from_spawn(junior, tmp_path / "roster.json")


def test_a_boss_who_is_not_live_costs_the_junior_a_label_not_a_start(
    board, junior, brief_from_spawn
):
    """Honest degradation. A boss with no session on the board has no socket to
    publish, and the deck must not invent one -- an address that resolves to
    nothing is worse than none, because the junior would use it."""
    tmp_path, _ = board
    office.publish([])

    script = brief_from_spawn(junior, tmp_path / "roster.json")

    assert "uds:" not in script
    assert CHOSEN_NAME in script


def test_the_board_publishes_the_address_the_cli_itself_publishes(board):
    """The join to the CLI's own registry, stated once. `office.json` must
    carry exactly the `messagingSocketPath` that `~/.claude/sessions/<pid>.json`
    advertises, or the deck is handing out an address of its own invention."""
    tmp_path, _ = board
    published = json.loads((tmp_path / "office.json").read_text())
    entry = published["sessions"]["sid-boss"]
    assert entry["address"] == f"uds:{manager.socket_path(BOSS_PID)}"
    assert entry["name"] == CHOSEN_NAME
