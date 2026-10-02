"""One door, two channels: the deck must hire on a box with no Terminal.app.

**The defect this pins.** Every door that puts a process at a desk calls
`spawn.spawn_terminal` -- `server/api.py::Surface._start` and
`server/app.py::roster_start` are the only two callers, and both do. That runs
`osascript`, which is macOS-only. The second install of this deck (Linux,
systemd, authenticated, answering 200) has therefore hired NOBODY, ever: every
hire on it dies at the last step. Meanwhile `spawn_background` -- the headless
`claude --bg` path -- already existed, was tested, and had no caller at all.

**The class, not the instance.** "Linux has no osascript" is the instance. The
class is "this process cannot open a window", and a Mac is in it too: over ssh,
or under a launchd daemon, `osascript` is right there on PATH and there is still
no session to open a window in. So the choice is made from a capability read --
`osascript` on PATH *and* `launchctl managername` reporting a logged-in Aqua
session -- never from `sys.platform`.

**Measured against the real CLI, on this Mac, 2026-09-02, claude 2.1.258:**

  $ claude --bg --name seedprobe-a --append-system-prompt '...' \\
        'Reply with exactly the word BANANAQUIT and nothing else.'
  backgrounded · 17ec00d6 · seedprobe-a
  $ claude logs 17ec00d6
  ... ❯ Reply with exactly the word BANANAQUIT and nothing else.
  ... ⏺ BANANAQUIT

The seed DOES survive `--bg` as the trailing positional. `spawn_background` just
never passed it. The control, same session, no seed:

  backgrounded · 0b697cee · seedprobe-b (idle — send a prompt to start)

-- the CLI says outright what a seedless headless hire is. Both probes were
`claude rm`'d. That run also measured the thing `spawn_background` got wrong:
stdout is a five-line BANNER, so `out.stdout.strip()` is the whole banner, not
an id. Nothing had ever run it.

Hermetic: tmp roster, tmp `.claude.json`, tmp approval settings, tmp ledger.
Nothing spawns and no window opens -- the one `subprocess.run` that would is
stubbed, and the channel is injected rather than read off this machine.
"""

import json
import shutil
import subprocess
import sys

import pytest

from server import approval, office, pretrust, roster, spawn

SEED = "Ask the owner what this desk is for then name yourself"
BOSS = "uds:/tmp/deck/acme-growth.sock"

TERMINAL = spawn.Channel("terminal", "gui_session", "aqua")
HEADLESS = spawn.Channel("background", "no_osascript", "no osascript on PATH")


class _Ran:
    """What `subprocess.run` returns when the spawn is happy. The stdout is the
    REAL banner `claude --bg` printed on this Mac, byte for byte."""

    returncode = 0
    stdout = ("backgrounded · 0b697cee · seedprobe-b\n"
              "  claude attach 0b697cee    open in this terminal\n"
              "  claude logs 0b697cee      show recent output\n")
    stderr = ""


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """The deck's own directory, relocated: the real one holds the live
    `events.jsonl` every session on this Mac appends to, and these tests run the
    real spawn path, which installs the approver on its way."""
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(approval, "SETTINGS_PATH", tmp_path / "approve.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(office, "address_for", lambda name: BOSS if name else "")
    return tmp_path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A stand-in for `~/.claude.json`; the real one is never touched."""
    path = tmp_path / "dot-claude.json"
    path.write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", path)
    return path


@pytest.fixture
def desk(bus, config):
    """The kind of desk an agent-hired junior always gets: a direct child of the
    deck's own `workspaces/`, seconds old, that the CLI has never seen."""
    workspace = bus / "workspaces" / "branch-scout"
    workspace.mkdir(parents=True)
    made = roster.Desk(name="branch-scout", cwd=str(workspace), engine="claude",
                       mission="Watch the unmerged branches.",
                       reports_to="acme-growth")
    roster.save_roster(bus / "roster.json", [made])
    return made


@pytest.fixture
def spawned(monkeypatch, config, osascript_is_on_path):
    """Intercepts the ONE call that would open a window or start a real agent,
    and nothing else, recording the command line and the config as it stood at
    that instant.

    Deliberately NOT a stub of `spawn_terminal`/`spawn_background`: the claim
    under test is that the shared path closes every gate, so a test that
    replaced that path would be grading its own stub.

    `osascript_is_on_path` is what lets the `[terminal]` half of every sweep
    below run on a machine that has no Terminal.app. The channel here is
    INJECTED -- that is the whole design of this file -- but `spawn_terminal`
    then re-reads `shutil.which("osascript")` on its own account and refuses.
    MEASURED on the box, same commit as the Mac's clean run: five `[terminal]`
    parameters failed with `SpawnError: osascript is not on PATH` while every
    `[background]` parameter passed, so the sweep was only ever half a sweep
    there -- on the one machine whose channel is the headless one. The pin is
    inert on the headless half: nothing on that path asks the question.
    """
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append({"cmd": " ".join(argv), "argv": argv,
                     "config_at_spawn": config.read_bytes()})
        return _Ran()

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    return seen


def _start(desk, bus, channel, seed=SEED):
    return spawn.start(desk, roster_path=bus / "roster.json", seed=seed,
                       channel=channel)


# -- the capability check: both branches, driven on this Mac -----------------


def test_a_box_with_no_osascript_takes_the_headless_channel():
    """The Linux box. There is no Terminal.app to open, so the only channel
    that can hire is the headless one."""
    picked = spawn.choose_channel(which=lambda name: None,
                                  manager=lambda: "")
    assert picked.name == "background"
    assert picked.reason == "no_osascript"
    assert "osascript" in picked.detail


def test_a_mac_with_no_gui_session_takes_the_headless_channel_too():
    """The other half of the class, and the reason this is not a platform
    check: ssh into this Mac, or run under launchd, and `osascript` is right
    there on PATH with no session to open a window in."""
    picked = spawn.choose_channel(which=lambda name: "/usr/bin/osascript",
                                  manager=lambda: "Background")
    assert picked.name == "background"
    assert picked.reason == "no_gui_session"
    assert "Background" in picked.detail


def test_a_logged_in_mac_still_opens_a_real_window():
    picked = spawn.choose_channel(which=lambda name: "/usr/bin/osascript",
                                  manager=lambda: "Aqua")
    assert picked.name == "terminal"
    assert picked.reason == "gui_session"


@pytest.mark.skipif(sys.platform != "darwin", reason="the control needs a Mac")
def test_the_channel_is_not_chosen_from_the_platform_string():
    """THE detector for the shortcut that rebuilds the bug one machine over.
    The control is the machine itself: `sys.platform` really is "darwin" here,
    the capability read really does say no window, and the capability has to
    win. `if sys.platform == "darwin": terminal` fails this test on a Mac --
    which is where anyone would run it."""
    assert sys.platform == "darwin", "precondition"
    picked = spawn.choose_channel(which=lambda name: None, manager=lambda: "")
    assert picked.name == "background"


@pytest.mark.skipif(shutil.which("launchctl") is None, reason="no launchctl here")
def test_the_default_probe_reads_this_machine_rather_than_assuming_it():
    """The control for the probe itself: what `spawn` believes about this box
    has to equal what the box says when asked from the shell."""
    truth = subprocess.run(["launchctl", "managername"], capture_output=True,
                           text=True, timeout=10)
    assert spawn._launchd_manager() == truth.stdout.strip()


# -- the seed, on both channels ---------------------------------------------


@pytest.mark.parametrize("channel", [TERMINAL, HEADLESS], ids=lambda c: c.name)
def test_the_seed_reaches_the_desk_on_either_channel(desk, bus, spawned, channel):
    """On the box the seed IS the job -- it is the hire brief and the interview
    prompt. `spawn_background` passed no seed at all, and `claude --bg` with no
    prompt prints `(idle — send a prompt to start)`: the hire sits there knowing
    nothing. Measured; see this module's docstring."""
    _start(desk, bus, channel)
    assert len(spawned) == 1, "the desk must still actually start"
    assert SEED in spawned[0]["cmd"], \
        f"the seed never reached the {channel.name} channel: {spawned[0]['cmd']}"


def test_the_headless_seed_is_the_trailing_positional_the_cli_reads(desk, bus,
                                                                    spawned):
    """`claude [options] [command] [prompt]`. A flag appended after the seed
    would swallow it -- the same class of fault as `opencode <seed>` reading the
    seed as a path."""
    _start(desk, bus, HEADLESS)
    argv = spawned[0]["argv"]
    assert argv[-1] == SEED
    assert argv.count(SEED) == 1
    assert "--bg" in argv


# -- every gate, on both channels -------------------------------------------


@pytest.mark.parametrize("channel", [TERMINAL, HEADLESS], ids=lambda c: c.name)
def test_every_gate_survives_the_channel_it_is_hired_through(desk, bus, spawned,
                                                             channel):
    """The sweep. `--name` is THE join every lookup on this deck is keyed on;
    `--settings` is what puts the hire under the approval hook; the boss address
    is what makes its brief resolvable after a rename. A headless agent has no
    window to show a dialog in, which makes an ungoverned one HARDER to notice.
    Asserts each signal is PRESENT, never that an error is absent."""
    _start(desk, bus, channel)
    cmd = spawned[0]["cmd"]
    assert "--name branch-scout" in cmd
    assert "--settings" in cmd
    assert str(approval.SETTINGS_PATH) in cmd
    assert BOSS in cmd, "the junior was given no address to reach its boss"
    assert not [flag for flag in spawn.BANNED_FLAGS if flag in cmd]


@pytest.mark.parametrize("channel", [TERMINAL, HEADLESS], ids=lambda c: c.name)
def test_the_workspace_is_trusted_before_either_channel_starts(desk, bus,
                                                               spawned, channel):
    """Trusting it afterwards is trusting it too late: the window (or the
    headless session) is already sitting on the trust dialog."""
    _start(desk, bus, channel)
    at_spawn = json.loads(spawned[0]["config_at_spawn"])
    assert at_spawn["projects"][desk.cwd]["hasTrustDialogAccepted"] is True


@pytest.mark.parametrize("channel", [TERMINAL, HEADLESS], ids=lambda c: c.name)
def test_the_pretrust_verdict_rides_back_in_the_shape_a_row_already_reads(
    desk, bus, spawned, channel
):
    """`started but stuck` versus `never started`. `api.Surface._start` writes
    this onto the desk row, so the headless channel has to publish the same four
    keys or a row goes blank on the box."""
    verdict = _start(desk, bus, channel)["pretrust"]
    assert set(verdict) == {"ok", "reason", "detail", "muted"}
    assert verdict["ok"] is True
    assert verdict["muted"] == []


# -- one result shape both existing callers can consume ----------------------


@pytest.mark.parametrize("channel", [TERMINAL, HEADLESS], ids=lambda c: c.name)
def test_both_channels_answer_with_the_same_keys(desk, bus, spawned, channel):
    """`app.roster_start` returns this dict straight out as the response body.
    A key that appears on one channel and not the other is a client that works
    on the Mac and breaks on the box."""
    result = _start(desk, bus, channel)
    assert set(result) == {"ok", "channel", "detail", "agent_id", "pretrust"}
    assert result["ok"] is True
    assert result["channel"] == channel.name


def test_the_headless_answer_carries_the_id_the_cli_printed_not_the_banner(
    desk, bus, spawned
):
    """MEASURED: `claude --bg` prints a five-line banner on stdout, so
    `stdout.strip()` is the banner. `claude attach <that>` addresses nothing --
    the deck could start a headless hire and then never reach it again."""
    result = _start(desk, bus, HEADLESS)
    assert result["agent_id"] == "0b697cee"


# -- failure slugs stay specific and actionable ------------------------------


def test_a_headless_start_that_printed_no_id_is_a_refusal_not_a_success(
    desk, bus, monkeypatch, config
):
    class _Quiet:
        returncode = 0
        stdout = "Starting background service…\n"
        stderr = ""

    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: _Quiet())
    with pytest.raises(spawn.SpawnError) as raised:
        _start(desk, bus, HEADLESS)
    assert raised.value.reason == "no_agent_id"


def test_a_missing_osascript_is_never_reported_as_osascript_failed(
    desk, bus, monkeypatch, config
):
    """`osascript_failed` on a box with no osascript is a lie: it says the
    window manager refused when the truth is there is no window manager, and it
    sends whoever reads it to look at AppleScript instead of at the channel.

    `subprocess.run` here raises what the OS really raises when the binary is
    absent, so removing the guard does not merely fail to raise -- it produces
    the wrong slug, which is the defect stated exactly."""
    def no_such_binary(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'osascript'")

    monkeypatch.setattr(spawn.shutil, "which", lambda name: None)
    monkeypatch.setattr(spawn.subprocess, "run", no_such_binary)
    with pytest.raises(spawn.SpawnError) as raised:
        spawn.spawn_terminal(desk, roster_path=bus / "roster.json")
    assert raised.value.reason == "osascript_missing"
    assert "osascript" in raised.value.detail


def test_a_desk_no_channel_can_start_names_both_halves_of_why(bus, config,
                                                              spawned):
    """The box with an opencode desk on it: no GUI terminal AND no `--bg` for
    that engine. `background_unsupported` alone would send someone hunting for
    a flag; the answer is that neither door exists here."""
    workspace = bus / "workspaces" / "scribe"
    workspace.mkdir(parents=True)
    scribe = roster.Desk(name="scribe", cwd=str(workspace), engine="opencode",
                         mission="write")
    with pytest.raises(spawn.SpawnError) as raised:
        _start(scribe, bus, HEADLESS)
    assert raised.value.reason == "no_channel"
    assert "opencode" in raised.value.detail
    assert "--bg" in raised.value.detail
    assert HEADLESS.detail in raised.value.detail
