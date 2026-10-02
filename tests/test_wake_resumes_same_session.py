"""The argv that wakes a desk, and the two doors that must call it.

MEASURED on the box, 2026-09-30, claude 2.1.285, throwaway desk `wake-probe`:

    claude --bg --resume de8b1457-... --name wake-probe --settings ... \\
        --mcp-config ... --append-system-prompt ... "<note>"
    -> backgrounded · b3f54a1c · wake-probe
    -> note: background session de8b1457 keeps its own saved options, so the
       flags you passed started a copy as b3f54a1c. Without flags, the same
       command continues de8b1457 itself.

    claude --bg --resume de8b1457-... "<note>"
    -> backgrounded · de8b1457 · wake-probe
    -> note: woke session de8b1457 with its saved options (--name, --settings,
       --mcp-config, --append-system-prompt, --model, --permission-mode).

So the flags the contract first sketched (`--name`, `--settings`,
`--mcp-config`) are exactly what turns a wake into a fork with a NEW session
id. The job already carries them. A wake passes none.

The two doors: `app._try_inject`'s fallback (every `Surface` delivery -- his
messages, the deck's, a group's) and `app._deliver_routine`. Before this, both
queued the record and stopped.
"""

from __future__ import annotations

import inspect
import os
import time
from pathlib import Path

import pytest

from server import app as app_mod
from server import api, routines as routines_mod, spawn, wake


SID = "de8b1457-afd1-4b8f-919b-cb2bf935bb4e"


class _Ran:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


BANNER = ("backgrounded · de8b1457 · wake-probe\n"
          "  claude attach de8b1457    open in this terminal\n")
WOKE = ("note: woke session de8b1457 with its saved options (--name, "
        "--settings, --mcp-config, --append-system-prompt, --model, "
        "--permission-mode).\n")


def capture(monkeypatch, result: _Ran) -> list:
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return result

    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    return seen


# -- the argv -------------------------------------------------------------------


def test_the_resume_names_the_session_and_passes_no_saved_option(monkeypatch):
    seen = capture(monkeypatch, _Ran(0, BANNER, WOKE))
    got = spawn.resume_background(SID, cwd="/srv/w/wake-probe", seed="NOTE")
    [(argv, kwargs)] = seen
    assert argv == ["claude", "--bg", "--resume", SID, "NOTE"]
    assert kwargs["cwd"] == "/srv/w/wake-probe"
    assert got == "de8b1457"


@pytest.mark.parametrize("flag", ["--name", "--settings", "--mcp-config",
                                  "--append-system-prompt", "--model",
                                  "--permission-mode", "--fork-session"])
def test_no_flag_that_forks_the_session_is_ever_passed(monkeypatch, flag):
    """The class sweep: every option the job saves, each one a fork."""
    seen = capture(monkeypatch, _Ran(0, BANNER, WOKE))
    spawn.resume_background(SID, cwd="/w", seed="NOTE")
    [(argv, _)] = seen
    assert flag not in argv


def test_a_resume_that_started_a_copy_is_reported_as_one(monkeypatch):
    capture(monkeypatch, _Ran(0, "backgrounded · b3f54a1c · wake-probe\n",
                              "note: ... started a copy as b3f54a1c."))
    assert spawn.resume_background(SID, cwd="/w", seed="N") == "b3f54a1c"


@pytest.mark.parametrize("stderr", ["Invalid API key · Please run /login",
                                    "OAuth token has expired",
                                    "API Error: 401 authentication_error"])
def test_a_refused_login_is_named_oauth_expired(monkeypatch, stderr):
    capture(monkeypatch, _Ran(1, "", stderr))
    with pytest.raises(spawn.SpawnError) as caught:
        spawn.resume_background(SID, cwd="/w", seed="N")
    assert caught.value.reason == "oauth_expired"


def test_any_other_refusal_is_resume_failed(monkeypatch):
    capture(monkeypatch, _Ran(1, "", f"No conversation found with session ID: {SID}"))
    with pytest.raises(spawn.SpawnError) as caught:
        spawn.resume_background(SID, cwd="/w", seed="N")
    assert caught.value.reason == "resume_failed"


def test_a_banner_with_no_id_is_resume_failed(monkeypatch):
    capture(monkeypatch, _Ran(0, "something else entirely", ""))
    with pytest.raises(spawn.SpawnError) as caught:
        spawn.resume_background(SID, cwd="/w", seed="N")
    assert caught.value.reason == "resume_failed"


# -- the CLI still behaves the way it was measured -----------------------------


def cli_binary() -> Path | None:
    override = os.environ.get("DECK_CLI_BINARY")
    if override:
        return Path(override) if Path(override).is_file() else None
    root = Path.home() / ".local" / "share" / "claude" / "versions"
    if not root.is_dir():
        return None
    builds = sorted(p for p in root.iterdir() if p.is_file())
    return builds[-1] if builds else None


#: Read out of the installed bundle. If Anthropic rewords or removes any of
#: these, the measurement above is about a CLI that no longer exists.
RESUME_CLAIMS = (
    "continues that session in the background under the same ID",
    "keeps its own saved options",
    "Without flags, the same command continues",
    "with its saved options",
)


def test_the_installed_cli_still_resumes_the_way_it_was_measured():
    binary = cli_binary()
    if binary is None:
        pytest.skip("no Claude Code bundle installed to measure against")
    data = binary.read_bytes()
    for claim in RESUME_CLAIMS:
        assert claim.encode() in data, f"{claim!r} is gone from {binary}"


# -- door 1: a Surface delivery that finds no socket ---------------------------


class FakeWaker:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def ensure_awake(self, name, *, reason):
        self.calls.append((name, reason))
        return wake.WakeResult(state="woken", session_id=SID, detail="")


def wait_for(predicate, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_a_message_that_finds_no_socket_wakes_the_desk(monkeypatch):
    fake = FakeWaker()
    monkeypatch.setattr(app_mod, "_waker", fake)
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)

    delivered = app_mod._client_surface._deliver("wake-probe", "hello")

    assert delivered is False, "a wake is not a delivery; the ack comes later"
    assert wait_for(lambda: fake.calls == [("wake-probe", "owner_message")])


def test_a_message_that_went_down_a_socket_wakes_nothing(monkeypatch):
    fake = FakeWaker()
    monkeypatch.setattr(app_mod, "_waker", fake)
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: True)

    assert app_mod._client_surface._deliver("atlas", "hello") is True
    time.sleep(0.05)
    assert fake.calls == []


def test_a_broadcast_wakes_nobody(monkeypatch):
    fake = FakeWaker()
    monkeypatch.setattr(app_mod, "_waker", fake)
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)
    app_mod._client_surface._deliver("*", "all hands")
    time.sleep(0.05)
    assert fake.calls == []


# -- door 2: a routine firing at a sleeping desk -------------------------------


def a_routine(agent="wake-probe"):
    return routines_mod.Routine(id="r1", agent=agent, prompt="morning report",
                                trigger={"cron": "* * * * *"}, next_run_at=0.0)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    from server import office
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(app_mod, "ROSTER_PATH", tmp_path / "roster.json")
    return tmp_path


def test_a_routine_for_a_sleeping_desk_wakes_it(queue, monkeypatch):
    fake = FakeWaker()
    monkeypatch.setattr(app_mod, "_waker", fake)
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)

    ok, detail = app_mod._deliver_routine(a_routine())

    assert ok is True
    assert detail == "woken"
    assert fake.calls == [("wake-probe", "routine")]


def test_a_routine_that_could_not_wake_its_desk_still_reads_queued(queue, monkeypatch):
    class Refusing(FakeWaker):
        def ensure_awake(self, name, *, reason):
            self.calls.append((name, reason))
            return wake.WakeResult(state="refused", session_id="",
                                   detail="oauth_expired: log in again")

    monkeypatch.setattr(app_mod, "_waker", Refusing())
    monkeypatch.setattr(app_mod, "_try_inject", lambda name, text: False)
    ok, detail = app_mod._deliver_routine(a_routine())
    assert ok is True
    assert detail.startswith("queued")
    assert "oauth_expired" in detail


# -- the fact the app shows as ASLEEP ------------------------------------------


def test_the_surface_is_handed_the_real_asleep_probe():
    """SERIAL with A2: `Surface(asleep=...)` is A2's parameter. Until it lands
    there is nothing to hand the probe to, and this says so instead of passing."""
    if "asleep" not in inspect.signature(api.Surface).parameters:
        pytest.skip("Surface has no `asleep` parameter yet (A2 not merged)")
    assert app_mod._client_surface._asleep is wake.is_asleep
