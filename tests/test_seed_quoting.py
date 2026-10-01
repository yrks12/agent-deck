"""A multi-line seed with quotes in it must reach the CLI byte-for-byte.

**Why this file exists.** A spawned window showed zsh continuation prompts
(`quote>`) while it read the command, because `onboard.interview_prompt` is 43
lines long and contains an apostrophe, and it passes through TWO escaping
layers: `shlex.quote` for the shell, then `_as_applescript_string` for the
AppleScript literal. `claude` still started, so the question was whether that
was ugly or broken.

**The verdict is: cosmetic.** Measured end to end on the real path -- the real
`build_applescript`, a real `osascript`, a real Terminal window, with `claude`
swapped for a script that dumps its argv. A seed carrying apostrophes, double
quotes, a backslash, a backtick, `$HOME`, `$(id -u)`, a semicolon, a pipe and
four newlines arrived **identical**, and the system prompt with it. The
`quote>` lines are zsh echoing its own line-continuation state as it reads a
multi-line single-quoted argument; the argument itself is correct and the
command runs correctly.

**Why a test anyway.** "Currently correct" and "cannot silently break" are
different properties, and the second is the one worth having. Two escaping
layers around attacker-shaped text is precisely where a well-meaning edit to
`build_applescript` does real damage -- and the damage would be a new hire
briefed with a truncated prompt, which looks like a bad agent rather than a bad
quote.

These run the shell leg for real and hermetically: the AppleScript literal is
un-escaped (the exact inverse of `_as_applescript_string`), then handed to a
real shell with the executable swapped for an argv dump. Everything except
Terminal.app's own `do script` is exercised in-process. That last leg is the
part measured live rather than here, and it is called out as such.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server import onboard, spawn
from server.roster import Desk

SHELL = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"

HOSTILE = [
    pytest.param("the owner's inbox", id="apostrophe"),
    pytest.param('say "hello" loudly', id="double-quotes"),
    pytest.param("a backslash \\ and a backtick `id`", id="backslash-backtick"),
    pytest.param("$HOME and $(id -u) and ${PATH}", id="expansions"),
    pytest.param("semi; colon && pipe | redirect > /tmp/nope", id="operators"),
    pytest.param("first line\nsecond line\nthird", id="newlines"),
    pytest.param("mixed: it's \"both\" \\ and\nover $(two) lines", id="mixed"),
    pytest.param("", id="empty"),
]


def _unescape_applescript(literal: str) -> str:
    """The exact inverse of `spawn._as_applescript_string`, which escapes only
    a backslash and a double quote. Modelling the reader is what lets the shell
    leg run without a GUI."""
    out, i = [], 0
    while i < len(literal):
        if literal[i] == "\\" and i + 1 < len(literal):
            out.append(literal[i + 1])
            i += 2
        else:
            out.append(literal[i])
            i += 1
    return "".join(out)


def _shell_command(desk: Desk, seed: str, settings: str = "") -> str:
    script = spawn.build_applescript(desk, seed=seed, settings=settings)
    start = script.index('do script "') + len('do script "')
    end = script.index('"\n  activate')
    return _unescape_applescript(script[start:end])


@pytest.fixture
def dump(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for `claude` that records the argv it was actually handed."""
    out = tmp_path / "argv.txt"
    exe = tmp_path / "fake-claude"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(repr(sys.argv[1:]))\n")
    exe.chmod(0o755)
    return exe, out


def _run(desk: Desk, seed: str, dump, settings: str = "") -> list[str]:
    exe, out = dump
    command = _shell_command(desk, seed, settings)
    assert "&& claude " in command, "the executable moved; this test is vacuous"
    command = command.replace("&& claude ", f"&& {exe} ", 1)
    result = subprocess.run([SHELL, "-c", command], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert out.exists(), f"the command never ran: {result.stderr}"
    return eval(out.read_text())


@pytest.fixture
def desk(tmp_path: Path) -> Desk:
    work = tmp_path / "workspace"
    work.mkdir()
    return Desk(name="quote-probe", cwd=str(work), engine="claude",
                mission="m", label="", charter="c", reports_to=None)


@pytest.mark.parametrize("seed", HOSTILE)
def test_the_seed_reaches_the_cli_byte_for_byte(desk, dump, seed):
    """GOOD signal: the argument the CLI receives *equals* the seed. Not "no
    error" -- a truncated prompt raises nothing at all, it just briefs a new
    hire with half its instructions."""
    got = _run(desk, seed, dump)
    want = spawn.build_argv(desk, background=False, seed=seed)[1:]

    assert got == want
    if seed:
        assert got[-1] == seed


@pytest.mark.parametrize("seed", HOSTILE)
def test_the_seed_survives_alongside_the_approval_settings_flag(desk, dump,
                                                                seed, tmp_path):
    """The path a real hire now takes. `--settings <file>` is what puts a desk
    under the approval hook, so it is on every spawned command line -- and it
    adds another argument through the same two escaping layers. A settings path
    with a space in it is the ordinary case, not the exotic one: the deck writes
    that file under a directory named after the desk."""
    settings = tmp_path / "deck settings" / "approval.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")

    got = _run(desk, seed, dump, settings=str(settings))
    want = spawn.build_argv(desk, background=False, seed=seed,
                            settings=str(settings))[1:]

    assert got == want
    assert str(settings) in got
    if seed:
        assert got[-1] == seed


def test_the_real_interview_prompt_survives(desk, dump):
    """The actual seed every interviewed hire is given -- 40-odd lines with an
    apostrophe in the worked example. This is the one that drew `quote>`."""
    seed = onboard.interview_prompt("handle my email", "We build things.")
    assert "\n" in seed and "'" in seed, "the hazard left the prompt; retire this"

    got = _run(desk, seed, dump)

    assert got[-1] == seed
    assert got == spawn.build_argv(desk, background=False, seed=seed)[1:]


def test_a_workspace_path_with_a_space_and_a_quote_still_lands(dump, tmp_path):
    """`cd` is quoted by the same mechanism. A cwd the deck allocated is tame,
    but a cwd the owner stated is whatever he named it."""
    work = tmp_path / "his own 'stuff'"
    work.mkdir()
    desk = Desk(name="q", cwd=str(work), engine="claude", mission="m",
                label="", charter="c", reports_to=None)

    got = _run(desk, "hello", dump)

    assert got[-1] == "hello"
