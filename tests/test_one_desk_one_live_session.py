"""One desk, one LIVE session -- and the message reaches that one.

THE MEASURED DEFECT, on the box, 2026-09-04.

`POST /v1/agents/{name}/start` seats a session at a desk without retiring the
one already there. Four processes ended up registered under the single desk
name `new-hire-a64fcd`:

    2326c35c  pid 2568438   new-hire-a64fcd
    e7a4b27f  pid 2568444   new-hire-a64fcd
    403893d3  pid 2568472   new-hire-a64fcd
    b839e9cd  pid 2570279   new-hire-a64fcd

All four were genuinely alive -- real pids, real sockets in `/tmp/cc-socks/`,
real entries in `claude agents --json`. The board was not lying about liveness.
It was ambiguous about IDENTITY, and `office.address_for` resolved the
ambiguity by taking the first match in file order. MEASURED on the box:

    office.address_for("new-hire-a64fcd") -> uds:/tmp/cc-socks/2568438.sock

which is the OLDEST of the four, because `publish` writes cards in the
collector's order and the collector sorts by `started_at` ascending. So every
message the owner sent that desk was answered by the session that had been
sitting there longest.

That is what made three correct fixes look broken. A session's brief is frozen
at spawn (`--append-system-prompt`), so a fix to the brief only reaches a
session started after it -- and the deck was deterministically addressing the
session started before it.

WHY KILLING THEM DID NOT WORK, also measured. `~/.claude/jobs/<id>/state.json`
holds `respawnFlags`, which carries that frozen `--append-system-prompt`, and
the CLI's own daemon respawns the job from it when the process dies. A
`kill -TERM` on pid 2568438 removed the process, the socket and the session
file within four seconds -- and the daemon had it back under pid 2665177,
same session id, same name, same stale brief, before the next tick. The only
lever that retires a background session for good is the CLI's own
`claude stop <job-id>`, which sets the job's state to `stopped` so the daemon
leaves it alone. MEASURED: `claude stop 2326c35c` -> `stopped 2326c35c`, and it
stayed gone.

WHY RETIRE AND NOT REFUSE. Both were on the table for `spawn.start`. Refusing
to start an occupied desk is the safer-sounding option and it is the wrong one
here, because of `respawnFlags`: the incumbent is immortal and carries a brief
that cannot be edited in place. If `start` refuses while a session is seated,
then a desk whose brief is wrong can never be given the right one through the
deck at all -- the only fix left is the manual kill that the daemon undoes.
Retire-then-seat is the only order in which "we fixed the brief" is a thing the
owner can actually receive.

EVERY ASSERTION NAMES THE GOOD SIGNAL. "No duplicates on the board" and "no
stale address" are both also true of an empty board and of a deck that has
stopped starting anything at all. So these tests assert that the LIVE session
is the one addressed, that the live session's address is exactly its own
socket, and that the incumbent was retired AND a new session was seated.
"""

import json

import pytest

from server import manager, office, roster, spawn

DESK = "new-hire-a64fcd"

#: The two sessions of the measured defect: the one that had been there longest
#: and answered everything, and the one just seated that should have.
STALE_SID = "2326c35c-6953-4875-8f5a-e87b0c1500f4"
FRESH_SID = "b839e9cd-5e3e-4c35-880c-9c324c145d2b"

#: The pid of the session that answered everything on the box.
STALE_PID = 2568438


def retired_socket(sock_dir, pid):
    """The path a RETIRED session used to answer on, and no longer does.

    MEASURED on the box: retiring a session removed the process, its
    `/tmp/cc-socks/<pid>.sock` and its `~/.claude/sessions/<pid>.json` within
    four seconds. So a retired desk is a board entry whose socket is gone,
    which is exactly what this returns -- a path inside the live socket
    directory that nothing has bound.
    """
    return sock_dir / f"{pid}.sock"


@pytest.fixture
def board(tmp_path, fake_socket, monkeypatch):
    """A board written one tick ago: a stale session first, the live one second.

    Both entries carry an address, because at the moment the tick wrote them
    both processes existed. Between then and now the older one died. That gap
    is the case `address_for` has to survive on its own -- a board is a file,
    and a file is always a little bit out of date.
    """
    sock_dir, live_pid, _ = fake_socket
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    stale = retired_socket(sock_dir, STALE_PID)
    live = sock_dir / f"{live_pid}.sock"
    office.OFFICE_FILE.write_text(json.dumps({
        "generated_at": 0.0,
        "sessions": {
            # Insertion order is the defect's order: oldest first.
            STALE_SID: {"name": DESK, "state": "IDLE", "started_at": 100.0,
                        "pid": STALE_PID, "address": f"uds:{stale}"},
            FRESH_SID: {"name": DESK, "state": "IDLE", "started_at": 900.0,
                        "pid": live_pid, "address": f"uds:{live}"},
        },
    }))
    return live


def test_the_desk_resolves_to_the_session_that_is_actually_answering(board):
    """THE test. `address_for` must yield the live socket, by name alone.

    Names the good signal: the address it returns is the one the running
    process is listening on. A message sent here is answered by the brain the
    deck just seated, which is the entire point -- "it did not return the stale
    one" would also be satisfied by returning nothing at all.
    """
    assert office.address_for(DESK) == f"uds:{board}", (
        "the desk still resolves to a session that is not answering; a message "
        "to it is silently taken by the wrong brain")


def test_a_session_that_stopped_answering_is_published_with_no_address(
        tmp_path, fake_socket, monkeypatch):
    """`publish` may only hand out an address that resolves to something.

    `address_of`'s own docstring already promises this -- "an address that
    resolved to nothing would be worse than none, because a junior would use
    it" -- and the code never checked. It built `uds:<sock_dir>/<pid>.sock` as
    a pure function of the pid.

    The good signal is the DISCRIMINATION: the answering session is published
    with exactly its own socket, and the one that is not answering is published
    with nothing to send to.
    """
    sock_dir, live_pid, _ = fake_socket
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    monkeypatch.setattr(office, "toplevel_for", lambda cwd: cwd)
    monkeypatch.setattr(office, "branch_for", lambda top: "")
    monkeypatch.setattr(manager, "sock_dir", lambda: sock_dir)
    live = sock_dir / f"{live_pid}.sock"
    stale = retired_socket(sock_dir, STALE_PID)

    office.publish([
        {"session_id": STALE_SID, "name": DESK, "cwd": str(tmp_path),
         "pid": STALE_PID, "state": "IDLE", "git_branch": ""},
        {"session_id": FRESH_SID, "name": DESK, "cwd": str(tmp_path),
         "pid": live_pid, "state": "IDLE", "git_branch": ""},
    ])
    published = json.loads(office.OFFICE_FILE.read_text())["sessions"]

    assert published[FRESH_SID]["address"] == f"uds:{live}", (
        "the answering session lost its address; nothing can reach it now")
    assert published[STALE_SID]["address"] == "", (
        "a session that is not answering was published as addressable: "
        f"{published[STALE_SID]['address']!r}")


# -- seating a desk that is already occupied ---------------------------------

ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}

#: The CLI's own job id for the incumbent -- `daemonShort` in
#: `~/.claude/jobs/<id>/state.json`, and the only string `claude stop` accepts.
#: MEASURED: `claude stop <full session id>` answers "No job matching".
INCUMBENT_JOB = "2326c35c"


@pytest.fixture
def occupied(tmp_path, fake_socket, monkeypatch):
    """Desk `acme` with a live session at it, and the CLI job behind it.

    The job registry is real in shape: the daemon respawns from `respawnFlags`,
    so the job -- not the process -- is what has to be retired.
    """
    sock_dir, live_pid, _ = fake_socket
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    # Retiring a session is now also a thing the owner is TOLD about, in the
    # desk's own thread -- see `spawn.announce_restart`. So this test writes to
    # the message log as well as to the board, and both have to be its own.
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    live = sock_dir / f"{live_pid}.sock"
    office.OFFICE_FILE.write_text(json.dumps({
        "generated_at": 0.0,
        "sessions": {STALE_SID: {"name": "acme", "state": "IDLE",
                                 "started_at": 100.0, "pid": live_pid,
                                 "address": f"uds:{live}"}},
    }))
    jobs = tmp_path / "jobs" / INCUMBENT_JOB
    jobs.mkdir(parents=True)
    (jobs / "state.json").write_text(json.dumps({
        "state": "idle", "name": "acme", "sessionId": STALE_SID,
        "daemonShort": INCUMBENT_JOB,
        "respawnFlags": ["--name", "acme", "--append-system-prompt",
                         "the brief from before the fix"],
    }))
    monkeypatch.setattr(spawn, "JOBS_DIR", tmp_path / "jobs")
    return tmp_path


@pytest.fixture
def levers(monkeypatch):
    """Double the two impure ends: the retirement verb and the launcher."""
    calls = {"stopped": [], "started": []}

    def fake_stop(job_id):
        calls["stopped"].append(job_id)
        return True

    def fake_background(desk, *, roster_path, seed=""):
        calls["started"].append(desk.name)
        return {"ok": True, "agent_id": "0b697cee",
                "pretrust": {"ok": True, "reason": "", "detail": "",
                             "muted": []}}

    monkeypatch.setattr(spawn, "stop_job", fake_stop)
    monkeypatch.setattr(spawn, "spawn_background", fake_background)
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: spawn.Channel(
        "background", "no_osascript", "no Terminal.app here"))
    return calls


def test_seating_a_desk_retires_the_session_already_there(occupied, levers):
    """BOTH halves, and in this order: the incumbent is retired by its CLI job
    id, and a session is still seated. Asserting only the retirement would pass
    for a `start` that refused to start anything."""
    spawn.start(roster.Desk(**ACME), roster_path=occupied / "roster.json")

    assert levers["stopped"] == [INCUMBENT_JOB], (
        "the incumbent was never retired, so the desk now has two live "
        f"sessions; stop calls were {levers['stopped']}")
    assert levers["started"] == ["acme"], (
        "nothing was seated at the desk after retiring it")


def test_an_empty_desk_is_seated_without_retiring_anybody(tmp_path, levers,
                                                          monkeypatch):
    """The other half. A retirement that fires on an empty desk would stop
    whatever the deck happened to find, and a first hire would kill a
    stranger."""
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    # ITS OWN message log, and this one is not housekeeping. `spawn.start`
    # reads the desk's conversation to decide whether it is re-seating anybody,
    # and the fixture above writes `acme` a restart line into the shared bus --
    # so without this, "an empty desk" would inherit the other test's
    # conversation and stop being empty.
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(spawn, "JOBS_DIR", tmp_path / "jobs")
    office.OFFICE_FILE.write_text(json.dumps({"generated_at": 0.0,
                                              "sessions": {}}))

    spawn.start(roster.Desk(**ACME), roster_path=tmp_path / "roster.json")

    assert levers["started"] == ["acme"]
    assert levers["stopped"] == [], (
        f"an empty desk retired something: {levers['stopped']}")


def test_every_door_that_seats_a_desk_goes_through_the_one_that_retires():
    """Aim the check where the fault IS: the CLASS of doors, not one of them.

    Four doors can seat a session -- `POST /v1/agents/{name}/start`,
    `POST /v1/agents/interview`, `POST /api/roster/{name}/start` and the
    harvester's `seat` callback. The lesson of `spawn.start` is that a rule
    written in a caller is a rule the next caller forgets, so the retirement
    lives in `spawn.start` and this pins that `start` is the only thing any of
    them calls. Parsing the source is what still fails when a fifth door is
    added next month.
    """
    import ast
    import inspect

    from server import api as api_mod
    from server import app as app_mod

    offenders = []
    for module in (api_mod, app_mod):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in {"spawn_terminal", "spawn_background"}
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "spawn"):
                offenders.append(f"{module.__name__}:{node.lineno} "
                                 f"calls spawn.{node.attr} directly")
    assert offenders == [], (
        "a door seats a session without passing the retirement in "
        f"spawn.start: {offenders}")


def test_the_retirement_verb_uses_the_job_id_the_cli_actually_accepts(occupied):
    """`claude stop` takes the job's `daemonShort`, never the session id.

    MEASURED on the box: `claude stop 2326c35c-6953-4875-8f5a-e87b0c1500f4`
    answers `No job matching '...'. Run 'claude agents' to list running
    sessions.` and the process is still there. A retirement that passes the
    wrong id reports success and retires nothing, which is the defect wearing
    a fix's clothes.
    """
    assert spawn.jobs_for("acme") == [INCUMBENT_JOB], (
        "the desk's live session was not traced back to a CLI job id, so "
        "nothing can be retired for good")
