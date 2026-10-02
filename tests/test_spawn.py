import shutil
import subprocess

import pytest
from server.roster import Desk
from server.spawn import build_argv, build_applescript

ACME = Desk(name="acme-growth", cwd="/p", engine="claude", mission="ads",
            model="claude-opus-5")


def test_claude_argv_names_the_session_and_the_model():
    argv = build_argv(ACME, background=False)
    assert argv[0] == "claude"
    assert "--model" in argv and "claude-opus-5" in argv


def test_background_argv_uses_the_background_flag():
    """Claude Code's own durable-agent primitive. Asserts the GOOD signal --
    the flag is present -- rather than merely that nothing crashed."""
    assert "--bg" in build_argv(ACME, background=True)


def test_no_argv_ever_skips_permissions():
    """The constraint that makes the whole approval layer meaningful. Sweep the
    class, not the instance: every engine, both modes."""
    banned = {"--dangerously-skip-permissions",
              "--dangerously-bypass-approvals-and-sandbox",
              "--dangerously-bypass-hook-trust"}
    for engine in ("claude", "opencode", "codex"):
        for background in (True, False):
            desk = Desk(name="n", cwd="/p", engine=engine, mission="")
            assert not banned & set(build_argv(desk, background=background))


def test_an_unknown_engine_is_a_clear_error_not_a_broken_command():
    with pytest.raises(ValueError):
        build_argv(Desk(name="n", cwd="/p", engine="emacs", mission=""), background=False)


def test_applescript_quotes_a_cwd_containing_a_space():
    """A path like /Users/x/My Projects must not split into two arguments."""
    script = build_applescript(Desk(name="n", cwd="/Users/x/My Projects",
                                    engine="claude", mission=""))
    assert "My Projects" in script
    assert script.count('"') % 2 == 0


# ── the argv against the CLI that will actually read it ────────────────────
#
# Every test above asserts the argv against *this file's* idea of each CLI's
# interface. That is exactly how `opencode <seed>` survived: the list was the
# one the builder meant to emit, and opencode read the seed as a directory to
# start in. The tests below close that gap -- they read `<cli> --help` on the
# machine that will run the command.


OPENCODE = Desk(name="scribe", cwd="/p", engine="opencode", mission="write",
                model="anthropic/claude-opus-5")

SEED = "Ask the owner what this desk is for, then name yourself."


def _cli_help(binary: str) -> str:
    """`<binary> --help` as text. Skips -- never fakes -- when it is absent.

    Spends nothing: `--help` starts no session and opens no TUI. Takes ~0.1-0.3s
    for each of the three CLIs on this machine.
    """
    path = shutil.which(binary)
    if path is None:
        pytest.skip(f"{binary} is not installed here; nothing to pin the argv against")
    try:
        done = subprocess.run([path, "--help"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not run `{binary} --help`: {exc}")
    return (done.stdout or "") + (done.stderr or "")


def test_opencode_gets_the_seed_as_a_prompt_not_as_a_project_path():
    """`opencode [project]` -- a bare positional is a DIRECTORY TO START IN,
    not a message. Seeding a desk by appending the seed made opencode try to
    open a folder named after the interview prompt, and the desk was never
    asked anything. Nothing crashed, which is why the argv-shaped tests above
    all stayed green.

    Asserts the GOOD signal: `--prompt` is there and the seed is its value.
    """
    argv = build_argv(OPENCODE, background=False, seed=SEED)
    assert "--prompt" in argv, f"the seed is not a prompt to opencode: {argv}"
    assert argv.count(SEED) == 1, "the seed must appear exactly once"
    assert argv[argv.index("--prompt") + 1] == SEED
    assert argv[-1] != SEED or argv[-2] == "--prompt", \
        "the seed must never be a bare positional -- that is a path to opencode"


def test_an_unseeded_opencode_desk_starts_exactly_as_it_always_has():
    argv = build_argv(OPENCODE, background=False)
    assert argv == ["opencode", "--model", "anthropic/claude-opus-5"]


@pytest.mark.parametrize("engine,background", [
    ("claude", False), ("claude", True), ("opencode", False), ("codex", False),
])
def test_every_flag_the_builder_emits_is_one_the_cli_advertises(engine, background):
    """The anti-rot pin for the flags. A flag this builder invents is a flag
    the CLI rejects (or worse, silently ignores) at spawn time."""
    text = _cli_help(engine)
    desk = Desk(name="n", cwd="/p", engine=engine, mission="m",
                model="anthropic/claude-opus-5")
    argv = build_argv(desk, background=background, seed=SEED)
    unknown = [a for a in argv if a.startswith("--") and a not in text]
    assert unknown == [], f"`{engine} --help` does not advertise {unknown}"


def test_the_claude_flags_the_identity_fix_rests_on_still_exist():
    """`--append-system-prompt` is where a desk's identity lives: a message can
    be compacted away, a system prompt is re-sent on every request and cannot
    (server/compaction.py). `--bg` is the durable-agent primitive
    `spawn_background` refuses to run without. Read off `claude --help` here,
    not off memory."""
    text = _cli_help("claude")
    assert "--append-system-prompt" in text
    assert "--bg" in text
    assert "--model" in text


def test_the_engines_that_take_a_bare_positional_say_so_and_opencode_does_not():
    """WHY opencode is the odd one out, pinned against the three usage lines.
    If opencode ever starts reading its positional as a message this fails and
    somebody re-reads the help instead of guessing."""
    assert "[prompt]" in _cli_help("claude").lower()
    assert "[prompt]" in _cli_help("codex").lower()

    opencode = _cli_help("opencode")
    assert "path to start opencode in" in opencode, \
        "opencode's positional is no longer documented as a path -- re-read it"
    assert "--prompt" in opencode
