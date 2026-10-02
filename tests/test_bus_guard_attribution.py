"""The guard must blame the actor that actually wrote, and only that actor.

Two guards in `conftest.py` -- `_never_write_the_real_ledger` and
`_never_write_the_real_bus` -- snapshot `~/.claude/agent-bus/` before and after
every test and fail on anything that appeared. They caught a real defect: a test
that monkeypatched `BUS_EVENTS` (a name nothing reads; the code reads
`BUS_FILE`) put 18 approval records into the ledger `bin/deck-acceptance` grades
from, so pytest was overwriting the answer to "did a real run happen".

But the owner's bus has other writers. The deck daemon runs on a 1 Hz tick and
`cc-bus.js` is registered machine-wide, so every live Claude session on this Mac
appends to the same directory while the suite runs. MEASURED, with no pytest
running at all: in one 180-second window the live system added an `approval`
record, a `permission_request` record and a new id in `messages.jsonl` -- three
of the exact things the guards count. Whichever test happened to straddle that
write took the blame, which is why the suite reported two errors per run and
different tests each time.

A guard that names an innocent test is not a smaller problem than the leak it
watches for. It is the same problem: it trains everyone to skip the line, and
the next real leak goes through underneath it. So these tests pin BOTH sides,
and neither is allowed to hold alone:

  * the guarded bus is somewhere no other process on this machine can write, so
    a concurrent daemon write can never be attributed to a test; and
  * a test that leaks -- the original `BUS_EVENTS` typo, verbatim -- still
    fails, by name, with the message that says which attribute to patch.

Both are proved by running a real inner pytest session against this repo's real
`tests/conftest.py`, because what is under test IS the conftest. The inner run
gets a throwaway `HOME`, so the "live daemon" this file simulates never touches
the owner's bus.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: A record kind `_never_write_the_real_ledger` counts as forgeable, in the
#: shape `hooks/cc-bus.js` writes it. Used both as the leak a test can make and
#: as the write the daemon makes on its own tick.
APPROVAL = {"ts": 1.0, "event": "approval", "session_id": "s",
            "tool": "Bash", "decision": "ask", "rule_id": None}


def _run_inner(probe: Path, home: Path, env_extra: dict | None = None,
               timeout: int = 180) -> subprocess.CompletedProcess:
    """Run one probe file through pytest, with this repo's real conftest.

    The probe lives in `tests/` so `tests/conftest.py` applies to it exactly as
    it applies to the suite, and is named so the outer run never collects it
    (pytest collects a file given explicitly on the command line whatever its
    name, but only `test_*.py` when it walks the tree).
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env["PYTHONPATH"] = str(REPO)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         str(probe)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def probe_file():
    """Write a probe into `tests/`, and take it away again.

    Yields a writer: `probe_file(source) -> Path`.
    """
    written: list[Path] = []

    def write(source: str) -> Path:
        path = TESTS / f"_attribution_probe_{len(written)}.py"
        path.write_text(source)
        written.append(path)
        return path

    yield write
    for path in written:
        path.unlink(missing_ok=True)


@pytest.fixture
def fake_home(tmp_path):
    """A HOME the inner run believes is the owner's, with a bus under it.

    `.resolve()` because macOS hands out `/var/folders/...` for temp dirs and
    resolves it to `/private/var/folders/...`; a path comparison across the two
    spellings fails for a reason that has nothing to do with what is tested.
    """
    home = (tmp_path / "home").resolve()
    (home / ".claude" / "agent-bus").mkdir(parents=True)
    return home


# ── The bad signal: a daemon write must never be blamed on a test ────────────

INNOCENT_PROBE = '''
"""A test that touches no bus at all, and waits while something else does."""
import os
import time
from pathlib import Path


def test_an_innocent_test_that_writes_nothing_anywhere():
    Path(os.environ["PROBE_READY"]).write_text("ready")
    done = Path(os.environ["PROBE_DONE"])
    deadline = time.time() + 60
    while not done.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert done.exists(), "the harness never signalled; the probe is broken"
'''


def test_a_live_daemon_write_during_a_test_is_not_blamed_on_that_test(
        probe_file, fake_home, tmp_path):
    """The flake, reproduced deterministically and then forbidden.

    The inner test writes nothing. While it is running -- inside its own
    setup/teardown window, not between tests -- this test plays the deck daemon
    and appends to the bus under the inner run's HOME: an approval record, a
    message id, and a new desk on the roster. All three are things the guards
    count, and all three are things the live system genuinely does on its own
    tick (measured: it did the first two in a 180s window with pytest stopped).

    The inner run must still be green. If it is not, the guard is attributing
    another process's write to a test, and every error it prints is noise.
    """
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    probe = probe_file(INNOCENT_PROBE)
    bus = fake_home / ".claude" / "agent-bus"

    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         str(probe)],
        cwd=str(REPO), env={**os.environ, "HOME": str(fake_home),
                            "PYTHONPATH": str(REPO),
                            "PROBE_READY": str(ready), "PROBE_DONE": str(done)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 120
        while not ready.exists() and time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        assert ready.exists(), (
            "the inner run never reached the probe:\n" + proc.communicate()[0])

        # ── the daemon tick, mid-test ──
        with (bus / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(APPROVAL) + "\n")
        with (bus / "messages.jsonl").open("a") as fh:
            fh.write(json.dumps({"id": "live-msg-1", "to": "boss"}) + "\n")
        (bus / "roster.json").write_text(json.dumps(
            {"agents": [{"name": "a-desk-hired-mid-run"}]}))

        done.write_text("go")
        out = proc.communicate(timeout=120)[0]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, (
        "a test that wrote nothing was failed because another process wrote to "
        "the bus while it ran. That is the guard blaming the wrong actor:\n"
        + out)
    assert "1 passed" in out, out


# ── The good signal: the original defect must still be caught ────────────────

LEAK_PROBE = '''
"""The defect that made the guard necessary, reproduced verbatim.

`app._bus_append` reads the module global `BUS_FILE`. This patches `BUS_EVENTS`
-- a name nothing reads -- with `raising=False`, which turns the typo into
silence, and then writes an approval record for real.
"""


def test_patches_an_attribute_nothing_reads_and_then_writes(monkeypatch, tmp_path):
    from server import app

    monkeypatch.setattr(app, "BUS_EVENTS", tmp_path / "events.jsonl",
                        raising=False)
    app._bus_append({"ts": 1.0, "event": "approval", "session_id": "sid-1",
                     "tool": "Bash", "decision": "allow", "rule_id": "r"})
'''


def test_a_test_that_patches_the_wrong_attribute_and_writes_still_fails(
        probe_file, fake_home):
    """The protection, not the flake. This must hold before and after any fix.

    A guard that stops crying wolf by stopping looking is worse than the flake.
    So the `BUS_EVENTS` typo is put back exactly as it was written the first
    time, and the inner run must go red -- and say which attribute to patch,
    because a failure nobody can act on is a failure nobody acts on.
    """
    probe = probe_file(LEAK_PROBE)
    result = _run_inner(probe, fake_home)
    out = result.stdout + result.stderr

    assert result.returncode != 0, (
        "a test wrote an approval record to the bus the product actually reads "
        "and the suite reported success. The guard is gone:\n" + out)
    assert "BUS_FILE" in out, (
        "the guard fired but did not name the attribute to patch:\n" + out)


LEAK_PROBE_PRODUCT_STATE = '''
"""The same class, one directory wider: product state, not the ledger."""

import json


def test_hires_a_desk_without_patching_the_roster_path(monkeypatch, tmp_path):
    from server import roster

    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "roster.json",
                        raising=False)
    roster.save_roster(roster.DEFAULT_PATH,
                       [roster.Desk(name="a-fixture-desk", cwd="/tmp/nope",
                                    engine="claude", mission="none")])
'''


def test_a_test_that_writes_product_state_to_the_bus_still_fails(
        probe_file, fake_home):
    """`asks.json` is capped at 50 rows, so a fixture written there EVICTS a
    real question the owner has not answered yet. The roster is the same class
    of file. A test that adds a row to either must be named, whichever guard
    catches it."""
    probe = probe_file(LEAK_PROBE_PRODUCT_STATE)
    result = _run_inner(probe, fake_home)
    out = result.stdout + result.stderr

    assert result.returncode != 0, (
        "a test added a desk to the roster the live deck reads and the suite "
        "reported success:\n" + out)
    assert "a-fixture-desk" in out, (
        "the guard fired but did not say what appeared:\n" + out)


# ── The structural half: nothing a test imports points at the owner's bus ────

#: Every module that resolves a path in the bus at import time. Named rather
#: than discovered, so adding a module without adding it here is a review
#: question instead of a silent hole.
BUS_PATH_MODULES = (
    "server.app", "server.asking", "server.autoreview", "server.browser",
    "server.compaction", "server.handoff", "server.manager", "server.office",
    "server.paths", "server.roster", "server.routines", "server.sandbox",
    "server.vault", "server.sources.usage", "server.sources.opencode_usage",
)


def test_no_module_default_resolves_into_the_owners_live_bus():
    """Prevention, stated as an invariant.

    Every one of these paths is a module-level constant the product reads when
    a test forgets to patch it -- `app.BUS_FILE`, `asking.DEFAULT_PATH`,
    `roster.DEFAULT_PATH`, `office.MESSAGES_FILE`, and the rest. While the
    suite runs, not one of them may point into `~/.claude/agent-bus/`.

    That is what makes the guards honest: the bus they watch has exactly one
    writer, this process, so anything that appears in it was written by the
    test that was running. It is also what makes a forgotten patch harmless
    instead of destructive -- the write lands in a temp directory, and the
    guard still names it.

    Walks attributes rather than checking a list of names, so a new constant
    added to any of these modules is covered the day it is written.
    """
    import importlib

    live_bus = (Path(os.path.expanduser("~")) / ".claude" / "agent-bus")
    leaks = []
    for name in BUS_PATH_MODULES:
        module = importlib.import_module(name)
        for attr in dir(module):
            value = getattr(module, attr, None)
            if not isinstance(value, Path):
                continue
            if value == live_bus or live_bus in value.parents:
                leaks.append(f"{name}.{attr} = {value}")

    assert not leaks, (
        "these module defaults point into the owner's LIVE bus while the suite "
        "runs, so any test that forgets to patch one writes to the board every "
        "agent on this Mac is governed by: " + "; ".join(sorted(leaks)))


def test_the_guards_watch_the_same_bus_the_product_writes_to():
    """The two halves must not drift apart.

    Redirecting the product's paths without moving the guards would leave the
    guards staring at a directory no test can reach -- green forever, watching
    nothing. Redirecting the guards without the product would blame tests for
    daemon writes again. So the bus the guards census IS the bus the product
    resolves.
    """
    from tests import conftest
    from server import paths

    assert conftest.guarded_bus_dir() == paths.BUS_DIR
