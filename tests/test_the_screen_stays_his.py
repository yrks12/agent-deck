"""No ordinary test may take the owner's screen.

Found live, mid-work, in his own words: *"i need my screen now make these tests
when i say i dont need the screen"*. He was typing when a test suite seized the
Mac. AppleScript's `activate` pulls focus off whatever he is doing, and a
`do script` window lands on top of it.

The class sweep is why this file exists rather than one more local guard. The
app repo had the same shape one repo over -- three XCUITest classes that each
took the screen and nothing stopping a fourth -- and it is closed there by
`Tests/DeckKitTests/ScreenGateTests.swift`. Here the convention is `pytest.ini`
(`addopts = -m "not live and not ui"`) plus `_must_not_spawn` in exactly ONE
file, `tests/test_v1_setup_routes.py`. A convention held in one file is not a
guard: a test written next month that calls a start door for real reaches
`osascript`, and the first person to find out is him, losing his screen.

So the guard is on the CALL, not on the caller. Every route to a window in this
codebase ends at `subprocess.run(["osascript", ...])` -- `server/spawn.py` to
open a session, `server/focus.py` to raise one -- and both are wrapped for the
whole default suite. A door added later inherits the guard instead of having to
remember it, which is the same reason `_vouch` lives inside `spawn` and the
channel choice lives inside `spawn.start`.

The escape is deliberate and narrow: a test marked `live` or `ui` is already
deselected by default and already announces that it spends real resources. Those
are the tests he opts into.

Both halves are asserted. A guard that blocked every subprocess would pass "no
window opened" and break the suite in a way someone would then work around --
so the paired test proves ordinary subprocess work still runs.
"""

import shutil
from pathlib import Path

import pytest

from server import approval, focus, pretrust, roster, spawn

# `conftest`, not `tests.conftest`: pytest loads the shared fixtures under the
# bare name, so importing it the other way builds a SECOND module object and
# `ScreenTaken` from it is a different class -- `pytest.raises` then does not
# catch the guard it was written for. Measured while writing this file.
import conftest  # noqa: E402  (import order is the point, see above)

#: `shutil.which` as this machine really answers it, captured at import --
#: before any fixture can replace it. The narrowness check below compares
#: against this, so it states a fact about the box rather than a hard-coded
#: path that would be a different lie on each machine.
_REAL_WHICH = shutil.which


def test_an_ordinary_test_cannot_open_a_window():
    """THE test. Reaching osascript from an unmarked test is a refusal.

    Asserts the presence of the good signal -- a refusal that NAMES the switch
    -- not merely that no window appeared. "No window appeared" is also what a
    test that did nothing looks like.
    """
    with pytest.raises(Exception) as caught:
        spawn.subprocess.run(["osascript", "-e", "return 1"],
                             capture_output=True, text=True)
    message = str(caught.value)
    assert "YOS_SCREEN_IS_FREE" in message, message
    assert "osascript" in message, message


def test_raising_a_window_is_guarded_too():
    """The class, not the instance. `server/focus.py` is the OTHER route to
    his screen: it does not open a window, it brings one to the front, which
    interrupts him just as thoroughly."""
    with pytest.raises(Exception) as caught:
        focus.subprocess.run(["osascript", "-"], capture_output=True, text=True,
                             input="tell application \"Terminal\" to activate")
    assert "YOS_SCREEN_IS_FREE" in str(caught.value)


def test_ordinary_subprocess_work_still_runs():
    """The paired positive. A guard that refused every subprocess would satisfy
    the two tests above and make the suite unusable -- and the reliable outcome
    of an unusable guard is someone disabling it."""
    out = spawn.subprocess.run(["/bin/echo", "still works"],
                               capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout.strip() == "still works"


def test_the_guard_lives_in_the_harness_and_the_product_still_opens_windows():
    """The limit, and the half that is easy to get wrong.

    `spawn.subprocess` and `focus.subprocess` are the SAME stdlib module
    object, so the guard is necessarily global for the duration of a test. That
    is fine -- it is installed by `tests/conftest.py`, which only ever runs
    under pytest -- but it means the limit cannot be shown by calling
    subprocess from here. It is shown where it actually matters instead: the
    product still knows how to open a window on his Mac, because on his Mac
    that IS the feature. Both are asserted as presences.
    """
    here = Path(__file__).resolve().parent
    conftest = (here / "conftest.py").read_text()
    assert "YOS_SCREEN_IS_FREE" in conftest, (
        "the guard is not in the test harness, so either it is missing or it "
        "has been put in the product, where it would break the real deck")

    desk = roster.Desk(name="acme", cwd="/tmp/p", engine="claude", mission="")
    script = spawn.build_applescript(desk)
    assert "do script" in script and "activate" in script, (
        "the product stopped being able to open a Terminal window; the guard "
        "is supposed to bind the suite, not the deck")


# ── the other half of the same guard: no test may start a real agent ─────────
#
# Found by moving the bench to the Linux box, which is the only reason it was
# ever visible. On his Mac every start door takes the Terminal channel, so the
# guard above (osascript) catches everything. On the box `choose_channel`
# picks the headless channel, `spawn.start` calls `spawn_background`, and that
# runs the REAL `claude --bg` -- past every double in the suite, because the
# doubles all stub `spawn_terminal`, the door the box does not use.
#
# Measured on the box: 40 failures, and the message under them was
# `spawn_failed: [Errno 2] No such file or directory: 'claude'`. It failed only
# because `claude` was not on that PATH. Where it IS on PATH -- his Mac, and
# the box under a login shell -- the default suite would have started real
# agents and spent real money, silently.
#
# So the guard is on the CLASS: every engine binary this deck can start a
# session with, on every channel, not the one that happened to be caught.

ENGINE_BINARIES = ["claude", "codex", "opencode"]


#: The shapes that actually START something, one per engine and per channel.
STARTS_A_SESSION = [
    ("claude bare", ["claude"]),
    ("claude prompt", ["claude", "do the thing"]),
    ("claude -p", ["claude", "-p", "do the thing"]),
    ("claude --bg", ["claude", "--bg", "--name", "acme", "do the thing"]),
    ("codex", ["codex", "do the thing"]),
    ("opencode", ["opencode", "--prompt", "do the thing"]),
]


@pytest.mark.parametrize("label,argv", STARTS_A_SESSION,
                         ids=[c[0] for c in STARTS_A_SESSION])
def test_an_ordinary_test_cannot_start_a_real_agent(label, argv):
    """An unmarked test that would start a session is refused by name.

    The sweep is every engine AND both channels -- `claude --bg` is the box's
    shape and the one that escaped, a bare prompt is the Mac's. Asserting on
    the refusal's text is the good signal: "no agent appeared" is also what a
    test that did nothing looks like, and that is exactly how this survived.
    """
    with pytest.raises(Exception) as caught:
        spawn.subprocess.run(argv, capture_output=True, text=True)
    message = str(caught.value)
    assert "YOS_SCREEN_IS_FREE" in message, message
    assert argv[0] in message, message


#: Reading the CLI costs nothing and starts nothing, and the suite depends on
#: it: tests/test_spawn.py asks the real binary which flags it advertises,
#: which is the measurement that keeps build_argv honest.
READS_ONLY = [
    ("--help", ["claude", "--help"]),
    ("--version", ["claude", "--version"]),
    ("agents --json", ["claude", "agents", "--json"]),
]


@pytest.mark.parametrize("label,argv", READS_ONLY, ids=[c[0] for c in READS_ONLY])
def test_reading_the_cli_is_still_allowed(label, argv):
    """The limit on the sweep, and it is not decoration.

    My first version of this guard refused the binary by name and broke seven
    tests that ask the real CLI what it supports. A guard that deletes the
    measurement keeping `build_argv` honest costs more than it saves, and the
    reliable outcome is someone turning it off.
    """
    # The property is that the GUARD permits this, not that the CLI happens to
    # be installed. Asserting `returncode == 0` made this a Mac-shaped test: on
    # the Linux box, under a non-login shell, `claude` is not on PATH and it
    # failed for a reason that has nothing to do with the guard.
    try:
        spawn.subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        pass  # not installed here; the guard still let it through
    except Exception as unexpected:  # pragma: no cover - the actual failure
        assert "YOS_SCREEN_IS_FREE" not in str(unexpected), (
            f"the guard refused a read-only invocation: {unexpected}")
        raise


def test_the_headless_door_is_guarded_on_the_box_too(tmp_path, monkeypatch):
    """The end-to-end shape, stated where it actually bit.

    Pins the headless channel -- the box's channel -- and drives the real
    `spawn.start`. Nothing here stubs `spawn_background`, exactly like the 40
    tests that broke, so this fails if the guard ever stops covering the
    channel his Linux install actually uses.

    `spawn.start` really does run `_approval_settings` and `_vouch` on the way
    to the launcher, and those write. Redirected here rather than tolerated:
    this test exists because a test escaped its double, and it would be a poor
    one if it escaped its own.
    """
    monkeypatch.setattr(approval, "SETTINGS_PATH",
                        tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", tmp_path / "claude.json")
    desk = roster.Desk(name="acme", cwd="/tmp", engine="claude", mission="")
    headless = spawn.Channel("background", "no_osascript", "no Terminal.app")
    with pytest.raises(Exception) as caught:
        spawn.start(desk, roster_path="/tmp/roster.json", channel=headless)
    assert "claude" in str(caught.value), str(caught.value)


def test_saying_this_machine_has_a_terminal_does_not_hand_over_the_screen(
    tmp_path, monkeypatch, osascript_is_on_path
):
    """THE detector for `osascript_is_on_path`, and the reason it is safe.

    Seventeen Mac-door tests need `shutil.which("osascript")` to answer, because
    `spawn_terminal` reads it a second time on its own account and the Linux box
    says None. The obvious way to give them that is also the way to disarm this
    guard for every one of them at once -- and a fixture that quietly turned the
    screen guard off in a third of the files that touch spawning would be worth
    less than the seventeen failures it fixed.

    It does not, because the two work at different layers: the fixture answers a
    QUESTION about PATH, the guard refuses a CALL. Asserted as the good signal --
    `ScreenTaken`, raised by name, with the fixture active and `subprocess.run`
    left real, which is the exact combination the seventeen would hit if any of
    them ever forgot to stub the spawn.
    """
    assert shutil.which("osascript") == "/usr/bin/osascript", "precondition"
    monkeypatch.setattr(approval, "SETTINGS_PATH",
                        tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", tmp_path / "claude.json")
    desk = roster.Desk(name="acme", cwd="/tmp", engine="claude", mission="")
    terminal = spawn.Channel("terminal", "gui_session", "a logged-in Mac")
    with pytest.raises(conftest.ScreenTaken) as caught:
        spawn.start(desk, roster_path="/tmp/roster.json", channel=terminal)
    assert "osascript" in str(caught.value), str(caught.value)


def test_the_pin_answers_only_for_osascript_and_never_for_node(
    osascript_is_on_path
):
    """The narrowness is load-bearing, not tidiness.

    `spawn.start` runs `approval.install()` on its way to either launcher, and
    that resolves `node` through the very `shutil.which` this fixture replaces.
    A blanket stub would hand the approval hook a path to an interpreter that
    does not exist, `tests/test_approve_install.py` would go on passing, and the
    thing it claims to prove -- that a hire starts GOVERNED -- would be false on
    both machines.

    So: the real answer for `node`, whatever this box's answer is, and the real
    answer for a name that genuinely is not installed.
    """
    assert shutil.which("node") == _REAL_WHICH("node")
    assert shutil.which("a-binary-no-machine-has-cd2f1a") is None
