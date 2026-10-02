import json
"""Shared fixtures. No network, no real sessions, no tokens (except -m live)."""

import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ── The bus the suite is allowed to write to ────────────────────────────────
#
# This runs at import, before pytest has collected a single test and therefore
# before anything under `server/` is imported. That timing is the whole trick:
# every bus path in this codebase is a module-level constant computed from
# `paths.CLAUDE_HOME`, which reads `CLAUDE_CONFIG_DIR` once at import. Set it
# here and `app.BUS_FILE`, `app.ASKS_PATH`, `asking.DEFAULT_PATH`,
# `roster.DEFAULT_PATH`, `office.MESSAGES_FILE`, `vault.DEFAULT_PATH` and the
# rest resolve into a directory nothing but this process knows about. Set it in
# a fixture instead and it is already too late.
#
# WHY, rather than watching the real bus and blaming whoever is nearby:
#
# The two guards below used to census `~/.claude/agent-bus/` before and after
# every test. They caught a real defect -- 18 approval records a run written
# into the ledger `bin/deck-acceptance` grades from -- but the owner's bus has
# other writers. The deck daemon ticks at 1 Hz and `cc-bus.js` is registered
# machine-wide, so every live session on this Mac appends there while the suite
# runs. MEASURED, with pytest stopped: one 180-second window saw the live
# system add an `approval` record, a `permission_request` record and a new id
# in `messages.jsonl`. Whichever test straddled a write took the blame, which
# is why the suite reported two errors a run and different tests each time. A
# check that names an innocent test is not a smaller problem than the leak it
# watches for; it is the same problem, because it teaches everyone to skip the
# line and the next real leak goes through underneath it.
#
# Redirecting fixes attribution at the root instead of guessing at it. The
# guarded bus has exactly one writer -- this process -- so anything that
# appears in it was written by the test that was running, full stop. The guards
# stay, unchanged in what they mean, and get stronger: with no daemon in the
# directory there is nothing left to exclude, so `events.jsonl` is compared
# whole rather than by seven forgeable record kinds, and the eleven files that
# had to be waived are covered.
#
# It also makes the failure mode of forgetting harmless. A test that patches
# the wrong attribute writes into a temp directory instead of into the board
# the owner answers on his phone -- and the guard still fails it, by name, so
# the typo is still found.
#
# `sessions/` and `projects/` are symlinked back to the real ones. They are
# read, never written, and `tests/test_comms_sweep.py` runs the transcript
# extractor over every transcript on this machine. Redirecting those two would
# have turned that into a silent skip, which is coverage lost to a change that
# was only ever about the bus.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="deck-test-claude-home-")).resolve()
(_TEST_HOME / "agent-bus").mkdir()
for _read_only in ("sessions", "projects"):
    _real = Path(os.path.expanduser("~")) / ".claude" / _read_only
    if _real.is_dir():
        (_TEST_HOME / _read_only).symlink_to(_real)
os.environ["CLAUDE_CONFIG_DIR"] = str(_TEST_HOME)
# A desk on a second account carries `DECK_BUS_DIR`, which `server.paths` reads
# before CLAUDE_CONFIG_DIR. Left set, a suite run from that desk would write
# into the live bus the redirect above exists to keep it out of.
os.environ.pop("DECK_BUS_DIR", None)


# ── ...and the phone the suite is NOT allowed to reach ──────────────────────
#
# The redirect above covers the bus, the roster and every ledger. It has never
# covered the PHONE, and MEASURED on this Mac before this guard existed:
#
#     notify.script_path() -> ~/Projects/comunicate_with_me/bin/wa-send.js
#     exists -> True
#
# So any test that reached `notify.send` without stubbing it spawned the real
# bridge and sent Sam a real WhatsApp. Nothing had, by luck rather than by
# design -- every existing caller happens to inject its own sender -- but the
# inbound reply path ends at `_say_to_phone`, so testing it at all meant one
# unlucky line away from paging him on every run of the suite.
#
# Closed at the SOURCE rather than by patching `notify.send`, because there are
# two ways out of this module and a patch on the function would have to be
# remembered in every test that ever touches the path:
#
#   * the subprocess -- `DECK_WA_SEND` is pointed at a path that cannot exist,
#     so `send` returns "no such script" without spawning anything;
#   * the tunnel -- `DECK_WA_URL` is what the Linux box uses to reach the Mac's
#     bridge over WireGuard, and it skips the script entirely. Cleared, or a
#     suite run on the box would page him where a run on the Mac could not.
#
# Same timing as the bus redirect: at import, before `server.notify` is loaded.
# `tests/test_phone_replies_to_a_desk.py` asserts both halves, so a change that
# quietly undoes this is a failing test rather than a message on his lock
# screen. A test that WANTS a real send must set these itself and say why.
os.environ["DECK_WA_SEND"] = str(_TEST_HOME / "no-bridge-in-tests.js")
for _phone_var in ("DECK_WA_URL", "DECK_WA_TOKEN"):
    os.environ.pop(_phone_var, None)


def guarded_bus_dir() -> Path:
    """The bus the guards census -- which must be the one the product writes.

    Read through `server.paths` rather than recomputed, so the two halves
    cannot drift: if a change stopped the redirect from reaching the product,
    the guards would follow it back to the owner's live bus and start blaming
    bystanders again instead of quietly watching an empty directory.
    """
    from server import paths

    return paths.BUS_DIR


def pytest_unconfigure(config):
    """Take the throwaway home away again."""
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def never_the_real_claude_json(tmp_path_factory, monkeypatch):
    """No test may reach `~/.claude.json`. Autouse, so it is not opt-in.

    This exists because it already happened. Wiring `pretrust` into the
    interview door made `tests/test_interview.py` -- which knows nothing about
    trust and monkeypatches nothing -- write two pytest tmp paths into the
    owner's live 96-project config, and rewrite it 0600 in the process. A test
    that edits the file every Claude Code session on this machine shares is a
    worse bug than any it could catch, and "remember to patch it" is not a
    control. The default is redirected for every test in the suite; the ones
    that assert on the write point `pretrust.DEFAULT_CONFIG` at their own file.
    """
    from server import pretrust

    monkeypatch.setattr(
        pretrust, "DEFAULT_CONFIG",
        tmp_path_factory.mktemp("claude-config") / "dot-claude.json")


@pytest.fixture
def short_tmp():
    """A temp dir short enough for AF_UNIX.

    Socket paths are capped at ~104 bytes and pytest's tmp_path is already
    longer than that, so anything binding a socket needs its own short root --
    the same limit Claude Code's own resolver guards against.
    """
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="deck"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def fake_socket(short_tmp):
    """A UNIX socket that records every byte written to it.

    Yields (sock_dir, pid, received) where `received` is a list that fills with
    bytes objects as clients write. Mirrors how Claude Code binds
    <sock_dir>/<pid>.sock.
    """
    sock_dir = short_tmp / "cc-socks"
    sock_dir.mkdir(mode=0o700)
    pid = 424242
    path = sock_dir / f"{pid}.sock"

    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    stop = threading.Event()

    def accept_loop():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(0.5)
                buf = b""
                try:
                    while True:
                        part = conn.recv(4096)
                        if not part:
                            break
                        buf += part
                except (socket.timeout, OSError):
                    pass
                received.append(buf)

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    yield sock_dir, pid, received
    stop.set()
    thread.join(timeout=2)
    server.close()


@pytest.fixture
def approve_server():
    """A stand-in for the daemon's `POST /api/approve`, always answering allow.

    The approval hook is the one piece of this system that runs inside every
    tool call, so its contract is tested the way Claude Code exercises it: a
    real subprocess talking real HTTP to a real socket. Binds port 0 so parallel
    runs never collide. Yields the server; `.url` is its origin.

    Imports are local to keep this fixture a pure append -- nothing above it
    moves.
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            body = _json.dumps({"decision": "allow", "rule_id": "a"}).encode()
            self.send_response(200 if self.path == "/api/approve" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Silence: pytest output is not an access log."""

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _never_write_the_real_ledger():
    """No test may append a record to the event ledger the product reads.

    Measured: `tests/test_approve_records.py` alone put 18 lines into
    ~/.claude/agent-bus/events.jsonl on every run, because it monkeypatched an
    attribute name that does not exist (`BUS_EVENTS`; the code reads `BUS_FILE`)
    with `raising=False`, which turns a typo into silence.

    That is not untidiness. `bin/deck-acceptance` reads that ledger to decide
    whether a real run happened, and takes the LAST matching record -- so a
    pytest run would overwrite the answer with fixture data and the grader would
    report a live agent's approval as `session_id: "sid-1"`. The instrument was
    being answered by whoever wrote last.

    It used to watch the OWNER'S ledger, and only the seven record *kinds* a
    test can forge, because `cc-bus.js` is registered machine-wide and every
    live session on this Mac appends lifecycle lines to that file while the
    suite runs -- a whole-file check flagged those and flaked. It now watches
    the ledger under `guarded_bus_dir()`, which has exactly one writer, so the
    exclusions are gone: ANY line that appears is a line a test wrote. That is
    both stricter and, for the first time, correctly attributed.

    Guards the CLASS, not the instance: any future test that reaches the bus
    the product resolves fails here, named, rather than quietly moving the
    score -- and lands in a temp directory rather than on the owner's board.
    """
    ledger = guarded_bus_dir() / "events.jsonl"

    def _lines() -> list[str]:
        try:
            return ledger.read_text(errors="replace").splitlines()
        except OSError:
            return []

    before = _lines()
    yield
    after = _lines()
    if after == before:
        return
    appeared = [line for line in after if line not in before] or after
    raise AssertionError(
        f"this test wrote {len(after) - len(before)} record(s) to the event "
        "ledger the product reads, which bin/deck-acceptance grades from: "
        f"{appeared[:3]}. Point the bus at tmp_path -- the attribute app.py "
        "actually reads is BUS_FILE. If you patched something else and it took "
        "(monkeypatch with raising=False hides a typo), this is what it cost.")


#: Each product-state file in the bus, and how to name the THINGS in it.
#:
#: These are here for the MESSAGE, not for the detection. The guard fails on
#: any path in the bus that appeared or changed; this table lets it say
#: `roster.json: added ['a-fixture-desk']` instead of `roster.json changed`,
#: which is the difference between a failure someone acts on and a failure
#: someone reruns. A file missing from this table is still guarded, by path.
BUS_IDENTITIES = {
    "asks.json": lambda d: {a.get("id") for a in d.get("asks", [])},
    "autoreview.json": lambda d: {r.get("id") for r in d.get("rules", [])},
    "roster.json": lambda d: {a.get("name") for a in d.get("agents", [])},
    "routines.json": lambda d: {r.get("id") for r in d.get("routines", [])},
    "handoffs.json": lambda d: {h.get("id") for h in d.get("handoffs", [])},
    "groups.json": lambda d: {g.get("name") for g in d.get("groups", [])},
    "agent_prefs.json": lambda d: set(d),
    "desk_aliases.json": lambda d: set(d),
    "manager.json": lambda d: set(d),
}

#: Same rule, one record per line: the ids on the owner's own message queue.
BUS_JSONL = {"messages.jsonl": "id"}

#: Guarded by `_never_write_the_real_ledger` above, which reports the ledger
#: lines a test appended. Excluded here only so one write is not reported
#: twice, in two different vocabularies.
LEDGER_FILE = "events.jsonl"

#: The only files still waived, and the one reason that survives redirection.
#:
#: Eleven files used to be excluded because the OWNER'S bus had other writers.
#: That reason is gone -- `edits.jsonl`, `office.json`, `briefed.json`,
#: `approve-settings.json` and the rest are guarded now, as are `vault.json`
#: and `browser/`, which were never on either list and were simply uncovered.
#:
#: These three are different. Entering `TestClient(app.app)` fires the daemon's
#: startup event, which starts a poller inside the pytest process; it writes
#: these caches on ITS clock, not on the test's. MEASURED: three identical runs
#: of `tests/test_approve_records.py` blamed one test, then two, then one --
#: the same wrong-actor failure the redirect fixed, reproduced in-process. A
#: per-test check cannot attribute a timer's write, so it does not pretend to.
#:
#: They are the only three that hold, and the reason is narrow: each is a
#: derived cache -- token totals, a byte offset -- that nothing grades and
#: nobody reads as truth. The write can no longer reach the owner's machine
#: either way; prevention covers what attribution cannot. The way to close this
#: for real is for the loop to take its paths as arguments, or for a test that
#: wants routes not to have to start a poller to get them.
BACKGROUND_POLLER_FILES = frozenset({
    "usage-cache.json", "opencode-go-usage-cache.json", "harvest-offsets.json",
})


@pytest.fixture(autouse=True)
def _never_write_the_real_bus():
    """No test may write PRODUCT STATE anywhere in the agent-bus.

    The guard above it covers one file, `events.jsonl`, and stopped that class
    there. It covered nothing else in the directory, and the directory is where
    everything lives. Measured on the owner's live bus: `asks.json` held 50
    pending approvals and **41 of them were pytest fixtures** -- agents named
    `sid-a` and `an agent`, cwds like `~/Projects/deck` (a folder that does not
    exist on this machine), the same three subjects (`git push origin main`,
    `rm build/out`, `git status`) repeated once per suite run, and its mtime
    matching a run against the real bus.

    That is not clutter. Three things it actually cost:

    * **`MAX_ASKS` is 50.** The fixtures did not sit beside his real questions,
      they EVICTED them. Nine genuine asks survived; every earlier one is gone.
    * One fixture row offers an `always` option over an empty tool, an empty
      subject and an empty cwd -- a rule matching everything, everywhere. One
      tap on a card he did not create and the machine is handed over.
    * A board showing 41 questions nobody asked is a board he stops reading,
      which is how the two real ones underneath it get missed.

    So the guard is the whole directory, and now it is the whole directory
    literally: every path under `guarded_bus_dir()`, compared by content. It
    used to compare only the IDENTITIES in nine named files, and waive eleven
    others, because it watched the OWNER'S bus and could not tell a test's write
    from the daemon's tick. Redirecting the suite's bus removed that ambiguity
    and with it the exclusions -- a file nobody thought to list, `vault.json`
    say, is covered by default instead of silently uncovered. `BUS_IDENTITIES`
    survives to make the message say WHAT appeared, not whether anything did.

    Fails BY NAME, like its sibling: a test that reaches the bus the product
    resolves says which file it wrote and exactly what it added, instead of
    quietly editing the state every live agent on this Mac is governed by.
    """
    bus = guarded_bus_dir()

    def _identities(name: str) -> set | None:
        """The named rows in one file, or None if it has no naming rule."""
        if name in BUS_IDENTITIES:
            try:
                return {x for x in BUS_IDENTITIES[name](
                    json.loads((bus / name).read_text())) if x is not None}
            except (OSError, ValueError, AttributeError, TypeError):
                return set()  # unreadable or absent: nothing to add TO
        if name in BUS_JSONL:
            found = set()
            try:
                for line in (bus / name).read_text(errors="replace").splitlines():
                    try:
                        found.add(json.loads(line).get(BUS_JSONL[name]))
                    except Exception:
                        continue
            except OSError:
                return set()
            return {x for x in found if x is not None}
        return None

    def _census() -> tuple[dict, dict]:
        """(every path in the bus -> its bytes, every named file -> its rows).

        A directory maps to None: creating one is a write too. A workspace
        allocated under the live bus is product state just as surely as a row
        in a JSON file.
        """
        contents: dict[str, bytes | None] = {}
        try:
            for path in bus.rglob("*"):
                rel = str(path.relative_to(bus))
                if rel == LEDGER_FILE:
                    continue  # its sibling reports this one, in ledger terms
                if rel in BACKGROUND_POLLER_FILES:
                    continue  # a timer wrote it; no test can be blamed
                try:
                    contents[rel] = None if path.is_dir() else path.read_bytes()
                except OSError:
                    contents[rel] = b"<unreadable>"
        except OSError:
            pass
        named = {name: rows for name in (*BUS_IDENTITIES, *BUS_JSONL)
                 if (rows := _identities(name)) is not None}
        return contents, named

    before, before_rows = _census()
    yield
    after, after_rows = _census()

    touched = sorted(rel for rel in after
                     if rel not in before or after[rel] != before[rel])
    if not touched:
        return

    detail = []
    for rel in touched:
        rows = sorted(after_rows.get(rel, set()) - before_rows.get(rel, set()))
        detail.append(f"{rel}: added {rows}" if rows else f"{rel}: written")

    raise AssertionError(
        "this test wrote product state into the agent-bus the product "
        f"resolves: {'; '.join(detail)}. On the owner's machine those are the "
        "files every agent is governed by -- asks.json is the approval board "
        "he reads on his phone, and it is capped at 50 rows, so a fixture "
        "written there evicts a real question. Point the module constant the "
        "code actually reads at tmp_path (app.ASKS_PATH, app.AUTOREVIEW_PATH, "
        "app.ROSTER_PATH, app.ROUTINES_PATH, office.BUS_DIR, "
        "office.MESSAGES_FILE), or pass the path into the Surface. The bus is "
        "already redirected to a temp directory for you, so nothing escaped "
        "this machine -- but a test writing where it did not mean to is a test "
        "that is not exercising the path it claims to.")



#: What every `TestClient` in this suite presents to `/api`. Not a secret and
#: not a default anywhere in the product -- it exists only so the suite is a
#: caller with a credential rather than a caller without one.
PYTEST_DECK_TOKEN = "pytest-deck-token-not-a-real-one"


@pytest.fixture(autouse=True)
def the_suite_is_an_authorised_caller(tmp_path_factory, monkeypatch):
    """`/api` requires the deck's token. This makes the suite present one.

    Read this before you decide it is a bypass, because that is the obvious
    reading and it is wrong in a way worth spelling out.

    It does NOT switch the guard off. `server/app.py`'s middleware runs in
    full, `server/deckauth.matches` runs in full, and what the client sends is
    a real bearer header down the real code path. What it removes is the need
    to add that header by hand in seventeen test files -- `test_approve_*`,
    `test_roster_routes`, `test_pages`, `test_say_endpoint` and the rest all
    exercise `/api` for reasons that have nothing to do with authorisation, and
    editing every one of them would have buried the change that matters.

    The door itself is tested with this taken away. `tests/test_api_auth.py`
    drops the header from its own client and pins BOTH directions on every
    `/api` route -- refused with no credential, serving real data with one --
    so a regression that stopped gating a route, or one that started refusing
    everybody, still fails there.

    It also protects the owner's real token. `~/.claude/agent-bus/deck-token.txt`
    is what `bin/deck`, `bin/cdash` and every hired session's hooks read; a test
    that minted a fresh one into it would silently strand all of them until the
    daemon next restarted. `TOKEN_PATH` is redirected here for every test in the
    suite, in the same spirit as the two bus guards above.
    """
    from fastapi.testclient import TestClient

    from server import deckauth

    monkeypatch.setattr(
        deckauth, "TOKEN_PATH",
        tmp_path_factory.mktemp("deck-token") / "deck-token.txt")
    monkeypatch.setenv(deckauth.TOKEN_ENV, PYTEST_DECK_TOKEN)

    original_init = TestClient.__init__

    def _with_credential(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # `setdefault`, not assignment: a test that states its own
        # Authorization header -- including the empty one that means "no
        # credential" -- keeps it.
        if "authorization" not in self.headers:
            self.headers["authorization"] = f"Bearer {PYTEST_DECK_TOKEN}"

    monkeypatch.setattr(TestClient, "__init__", _with_credential)


# ── his screen ───────────────────────────────────────────────────────────────
#
# Found live, mid-work: "i need my screen now make these tests when i say i
# dont need the screen". A test suite seized his Mac while he was typing.
#
# Guarded at the CALL, not at the caller. Every route to his screen in this
# codebase ends at `subprocess.run(["osascript", ...])` -- `server/spawn.py`
# opens a Terminal window with `do script ... activate`, `server/focus.py`
# raises an existing one -- so a door written next month inherits this instead
# of having to remember a convention. That is the same reason `_vouch` lives
# inside `spawn` and the channel choice lives inside `spawn.start`.
#
# The escape is narrow and already exists: `live` and `ui` are deselected by
# `pytest.ini` and announce that they spend real resources. Those are the ones
# he opts into, with YOS_SCREEN_IS_FREE=1.
#
# Note this necessarily patches the stdlib `subprocess.run` for the duration of
# a test: `spawn.subprocess` and `focus.subprocess` are the same module object.
# That is safe because this file runs only under pytest -- the product is
# untouched, and `tests/test_the_screen_stays_his.py` asserts both halves.

class ScreenTaken(RuntimeError):
    """A test tried to open or raise a window on the owner's Mac."""


#: argv[0] basenames that put something in front of him.
_SCREEN_TAKERS = {"osascript", "osacompile"}

#: Binaries that can START A REAL AGENT. A different harm from the screen
#: takers -- money and a live session rather than focus -- and the half his Mac
#: hid. On the Mac every start door takes the Terminal channel and the
#: osascript entry above catches everything; on the Linux box the same door
#: takes the headless channel and runs `claude --bg` directly, past every
#: double in the suite. Measured on the box: 40 tests reached this, and only
#: PATH stopped them.
_ENGINE_BINARIES = {"claude", "codex", "opencode"}

#: First arguments that only READ. These do not start a session, cost nothing,
#: and the suite depends on some of them: `tests/test_spawn.py` asks the real
#: CLI which flags it advertises, which is the measurement that keeps
#: `build_argv` honest, and the collector tick reads `claude agents --json`.
#: Refusing those would delete real signal to buy nothing -- the harm is
#: starting a session, not naming the binary.
_INFO_ONLY = {"--help", "-h", "--version", "-v", "agents", "logs", "mcp",
              "doctor", "config", "plugin", "update"}


def _takes_the_screen(argv) -> str:
    """The command's name if it would reach his screen, else ""."""
    if isinstance(argv, (str, bytes, Path)):
        head = argv
    elif isinstance(argv, (list, tuple)) and argv:
        head = argv[0]
    else:
        return ""
    name = Path(os.fsdecode(head)).name if isinstance(
        head, (str, bytes, Path)) else ""
    if name in _SCREEN_TAKERS:
        return name
    if name in _ENGINE_BINARIES:
        rest = [str(a) for a in argv[1:]] if isinstance(
            argv, (list, tuple)) else []
        # A bare invocation starts an interactive session; so does anything
        # whose first argument is not one of the read-only forms (`-p`, a bare
        # prompt, `--bg`). Default-deny: an argument shape nobody listed is
        # treated as starting something.
        if not rest or rest[0] not in _INFO_ONLY:
            return name
    # `open -a Foo` activates an application; bare `open` of a file does not.
    if name == "open" and isinstance(argv, (list, tuple)) and "-a" in argv:
        return "open -a"
    return ""


@pytest.fixture(autouse=True)
def the_screen_stays_his(request, monkeypatch):
    """Refuse, loudly and by name, when an unmarked test reaches his screen."""
    if request.node.get_closest_marker("live") or \
            request.node.get_closest_marker("ui"):
        return
    if os.environ.get("YOS_SCREEN_IS_FREE") == "1":
        return

    import subprocess as _subprocess
    real_run = _subprocess.run

    def guarded(argv=None, *args, **kwargs):
        taker = _takes_the_screen(argv)
        if taker:
            harm = ("starts a real agent session, which costs real money "
                    "and puts a live process on the roster"
                    if taker in _ENGINE_BINARIES else
                    "takes the owner's screen -- it opens or raises a window "
                    "in front of whatever he is doing")
            raise ScreenTaken(
                f"{request.node.name} tried to run {taker!r}, which {harm}. "
                f"Stub the call at the seam the code actually uses -- on Linux "
                f"the start doors take the HEADLESS channel, so a double on "
                f"spawn_terminal alone does not intercept them -- or mark the "
                f"test `live`/`ui` and run it with YOS_SCREEN_IS_FREE=1. "
                f"Command was: {argv!r}")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(_subprocess, "run", guarded)


@pytest.fixture(autouse=True)
def _the_tick_pages_into_a_temp_ledger(tmp_path_factory, monkeypatch):
    """The daemon's tick must not write the paging ledger into the bus.

    This is the fix for an error that looked like flakiness for two full runs.
    Entering `TestClient` starts the collector loop, the loop pages his phone
    about pending questions, and `app.PAGED_PATH` is `paths.BUS_DIR /
    "paged.json"` -- product state. Whether the ledger guard fired depended
    only on whether a tick landed inside some test's window, so the error moved
    between tests and vanished on re-run. Measured: reverting the redirect in
    `tests/test_approve_records.py` reproduces it exactly, with the guard
    naming `paged.json: written`.

    Autouse, and in conftest, because it is a CLASS problem rather than one
    fixture's oversight: 18 test modules enter `TestClient` and start that
    loop, and before this exactly TWO redirected this path. Fixing the one file
    that happened to show the error would have left sixteen others able to
    produce it again, differently, next month.

    A test that cares about the ledger's contents monkeypatches it in its own
    body and wins, because that happens after this.
    """
    from server import app as app_mod

    monkeypatch.setattr(
        app_mod, "PAGED_PATH",
        tmp_path_factory.mktemp("paged") / "paged.json", raising=False)


@pytest.fixture
def osascript_is_on_path(monkeypatch):
    """Say "this machine has a Terminal.app" instead of asking the host.

    The second half of a pin that until now only had one half. A test about the
    Mac door already states its channel -- see the comment block in
    `tests/test_spawn_gates.py` and `tests/test_pretrust_wiring.py` explaining
    why `spawn.choose_channel` is pinned rather than probed. But `choose_channel`
    is not the only ambient read on that path: `spawn.spawn_terminal` asks
    `shutil.which("osascript")` again, on its own account, and refuses with
    `osascript_missing` when the answer is None.

    On the owner's Mac that second read is invisibly true, so nobody had to
    think about it. MEASURED on the Linux box, same commit: seventeen tests that
    HAD correctly pinned the channel died anyway with `SpawnError: osascript is
    not on PATH: this machine cannot open a Terminal window` -- a refusal that is
    exactly right for the product and exactly wrong for a test which has already
    declared which install it is describing. A pinned channel with an unpinned
    PATH is still asking the host machine a question.

    Narrow on purpose. Only `osascript` is answered; every other name goes
    through to the real `shutil.which`, because `approval.install()` runs on this
    same path and resolves `node` through it -- a blanket stub would hand the
    approver a fake interpreter and the test would be grading its own double.

    It does NOT touch `the_screen_stays_his`. That guard fires on the CALL, and
    the call is still `subprocess.run(["osascript", ...])`, so a test that takes
    this fixture without also stubbing `subprocess.run` is refused by name
    exactly as before -- `tests/test_the_screen_stays_his.py` pins that.
    """
    real_which = shutil.which

    def answers_like_a_mac(name, *args, **kwargs):
        if name == "osascript":
            return "/usr/bin/osascript"
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", answers_like_a_mac)
    return "/usr/bin/osascript"


@pytest.fixture(autouse=True)
def _every_test_starts_with_a_full_receipt_allowance(monkeypatch):
    """A rate limiter shared by the whole process must not leak between tests.

    `app._receipt_ceiling` is one `deskpage.Ceiling` created at import, which is
    exactly right in production: `_say_to_phone` is the one path to his phone
    with no natural gap, and a process-wide cap is what stops a loop reaching
    285 sends in a minute. In a suite it is shared state -- the tests run in
    one process, each one spends from the same allowance, and by the time
    `test_phone_wiring.py` runs there is none left.

    MEASURED, and this is why it is in conftest rather than in that one file:

        tests/test_phone_wiring.py alone   -> 10 passed
        the full suite                     ->  7 failed, 1537 passed

    Same code, same assertions. The seven were not testing anything different;
    they were running after the allowance was gone. A file-local fixture would
    have made those seven green and left every other module that reaches
    `_say_to_phone` able to fail the same way, in a different order, next
    month -- which is the shape this repo has already been bitten by twice.

    The good signal it protects is the one that matters to him: after he
    answers something, he is TOLD where it went. A suite in which that
    assertion quietly depends on run order cannot say whether the product
    still does it.
    """
    from server import app as app_mod
    from server import deskpage

    monkeypatch.setattr(app_mod, "_receipt_ceiling", deskpage.Ceiling())


@pytest.fixture(autouse=True)
def _no_live_openai_key(monkeypatch):
    """A test must never mint a real OpenAI client secret from the dev Mac's
    env (C3); tests that want a key set one after this runs."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _the_login_vault_stays_in_the_test(tmp_path_factory, monkeypatch):
    """Answering a browser handoff `done` shares the sign-in with every desk
    (server/login_vault.py). In a test that must neither write the real vault
    nor dial a real desk's browser; tests that want either set their own."""
    from server import login_vault

    monkeypatch.setattr(login_vault, "VAULT_PATH",
                        tmp_path_factory.mktemp("login-vault") / "cookies.json")
    monkeypatch.setattr(login_vault, "running_desks", lambda: [])

    def no_desk(desk):
        raise login_vault.sandbox.SandboxError("docker_unavailable", "tests")
    monkeypatch.setattr(login_vault, "driver_for", no_desk)


@pytest.fixture(autouse=True)
def _the_store_stays_off_the_network(tmp_path_factory, monkeypatch):
    """The deck's mounted Connectors & Skills store (server/connectors.py)
    fetches GitHub and the MCP registry, and reloads real desks. A route sweep
    that calls `/v1/store/catalog` must do neither: it gets a store with no
    network, a temp cache and no reloads. Tests that want a store build one."""
    from server import connectors

    store = connectors.default_store()
    root = tmp_path_factory.mktemp("store")
    monkeypatch.setattr(store, "root", root)
    monkeypatch.setattr(store, "fetch", lambda *a, **k: (503, b"offline in tests"))
    monkeypatch.setattr(store, "probe", lambda url: "unknown")
    monkeypatch.setattr(store, "post", lambda *a, **k: (503, {}, b"offline in tests"))

    def no_cli(args, cwd, timeout):
        raise FileNotFoundError("claude is not run by tests")
    monkeypatch.setattr(store, "runner", no_cli)
    monkeypatch.setattr(store, "reloader", lambda desk, note: {
        "desk": desk, "action": "none", "detail": "tests"})
    monkeypatch.setattr(store, "background", False)
    monkeypatch.setattr(connectors, "INSTALLED_PATH", root / "installed.json")


@pytest.fixture(autouse=True)
def _never_bind_the_real_socket_dir(monkeypatch):
    """No test may bind an AF_UNIX socket in a real `cc-socks` directory.

    MEASURED 2026-09-30: `/tmp/cc-socks/agentdeck.sock` -- where the owner's
    WhatsApp replies arrive -- was held by another agent's pytest run, not by
    his deck. Any test that ran the app's startup bound the real path (and the
    listener unlinked his live socket to do it). So:

    * `DECK_PHONE_SOCKET` points the deck's socket at a short tmp dir, for every
      test (server/app.py `_deck_socket` reads it first);
    * any bind under a real socket directory fails the test loudly instead of
      quietly taking the path. Tests that need a Claude-style socket dir build
      their own under a tmp root (see `fake_socket`), which this allows.
    """
    import socket as _socket

    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="dps"))
    monkeypatch.setenv("DECK_PHONE_SOCKET", str(root / "agentdeck.sock"))

    real_dirs = []
    for base in (os.environ.get("CLAUDE_CODE_TMPDIR"), os.environ.get("XDG_RUNTIME_DIR"), "/tmp"):
        if base:
            real_dirs += [Path(base) / "cc-socks", Path(base) / f"cc-socks-{os.getuid()}"]
    real = {os.path.realpath(d) for d in real_dirs}

    original = _socket.socket.bind

    def guarded(self, address):
        if isinstance(address, (str, bytes)) and address:
            where = os.fsdecode(address)
            parent = os.path.realpath(os.path.dirname(where) or ".")
            assert parent not in real, (
                f"a test tried to bind {where}: that is a real cc-socks directory "
                "(the owner's live sessions and the deck's phone socket live there)")
        return original(self, address)

    monkeypatch.setattr(_socket.socket, "bind", guarded)
    yield
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _the_rules_stay_in_the_test(tmp_path_factory, monkeypatch):
    """Starting a desk records which rules it was briefed on
    (server/rules.py). In a test that record goes to a temp directory."""
    from server import rules

    monkeypatch.setattr(rules, "RULES_DIR", tmp_path_factory.mktemp("rules"))


@pytest.fixture(autouse=True)
def _team_memory_stays_in_the_test(tmp_path_factory, monkeypatch):
    """Team memory, desk-written skills and desk memory (server/learning.py)
    are what every desk is briefed on. In a test they live in a temp
    directory: the app's startup seed and every save_lesson land there, and a
    brief never lists a skill from the machine running the suite."""
    from server import learning

    root = tmp_path_factory.mktemp("learning")
    monkeypatch.setattr(learning, "TEAM_DIR", root / "team-memory")
    monkeypatch.setattr(learning, "SKILLS_DIR", root / "skills")
    monkeypatch.setattr(learning, "PROJECTS_DIR", root / "projects")


@pytest.fixture(autouse=True)
def _desk_browsers_stay_in_the_test(tmp_path_factory, monkeypatch):
    """The browser reaper (server/browser_reaper.py) marks desks as used,
    records states and memory, and WAKES a stopped desk's browser when its
    screen is opened. In a test its files live in a temp directory and a wake
    starts nothing: opening a screen in a test must never `docker run` a
    real container on the machine running the suite."""
    from server import browser_reaper

    root = tmp_path_factory.mktemp("reaper")
    monkeypatch.setattr(browser_reaper, "LAST_USE_DIR", root / "last-use")
    monkeypatch.setattr(browser_reaper, "VIEW_DIR", root / "viewing")
    monkeypatch.setattr(browser_reaper, "STATES_PATH", root / "states.json")
    monkeypatch.setattr(browser_reaper, "MEMORY_PATH", root / "memory.json")
    monkeypatch.setattr(browser_reaper, "ADMISSION_LOCK", root / "admission.lock",
                        raising=False)
    monkeypatch.setattr(browser_reaper, "_ensure", lambda desk: None)


@pytest.fixture(autouse=True)
def _desk_launches_ignore_the_hosts_priority_tools(monkeypatch):
    """Every `claude --bg` the deck runs is wrapped in `choom`, `nice` and
    `ionice` when the host is Linux and has them (server/spawn.py `lowered`).
    Which wrappers appear is a fact about the machine running the suite, not
    about the code: on a Mac none do, on a Linux CI runner all three do. A test
    asserting a launch argv must not pass on one and fail on the other.

    So the suite starts from "no tool present" everywhere. The wrapping itself
    is pinned by tests that hand `lowered` its tools explicitly, or patch
    `_choom`/`_nice`/`_ionice` themselves (tests/test_desk_cpu_priority.py,
    tests/test_oom_priority.py) -- a test's own patch runs after this one."""
    from server import spawn

    monkeypatch.setattr(spawn, "_choom", lambda: None)
    monkeypatch.setattr(spawn, "_nice", lambda: None)
    monkeypatch.setattr(spawn, "_ionice", lambda: None)


@pytest.fixture(autouse=True)
def _no_oauth_refresher_thread(monkeypatch):
    """The app's startup starts a daemon thread that wakes every 600s and
    refreshes OAuth tokens in the DEFAULT connectors store (server/app.py
    `_refresh_oauth_forever`). The suite runs one app for its whole length,
    so once it passed ten minutes the thread fired in the middle of whatever
    test was running, wrote `connectors/oauth.json` into the guarded bus and
    failed that test by name -- on CI, `test_skills.py`, which never touched
    OAuth. A test that wants the refresh calls `refresh_oauth_due()` on a
    store it built; the timer itself never runs here."""
    from server import app as app_mod

    monkeypatch.setattr(app_mod, "_refresh_oauth_forever", lambda: None)
