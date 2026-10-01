"""Wake: a message to a sleeping desk brings the SAME session back (K3).

Claude Code's background daemon retires an idle desk after about an hour
(`[bg] bg retire 25adf776: idle-prompt, idle 61m`, measured on the box), and
until this module the deck never brought one back: the owner's message was
queued for a socket that was gone and waited for whoever restarted the desk by
hand. `ensure_awake` is the one call every delivery door makes when the socket
is not there. See `docs/wake.md`.

Four outcomes, closed:

    live       a session is seated (or was woken < MID_RESUME_SECONDS ago)
    woken      `claude --bg --resume <last session id>` -- same id, same memory,
               no replay, no "this desk was restarted" line
    restarted  nothing resumable: today's `spawn.start` (replay + notice)
    refused    cannot run a session at all -- `detail` opens with the slug
               (`oauth_expired`, `disk_full`, `not_a_desk`, `terminal_channel`,
               or whatever `spawn.start` refused with)

THE WAITING MAIL RIDES IN THE SEED. MEASURED on the box: the office hook did
not hand the queued message to a woken session's first turn -- the hook finds
mail by the session's NAME in `office.json`, and the collector had not yet
published the resumed session when the hook ran. So the seed carries the
messages, framed exactly as the hook would frame them, and they are acked once
the CLI has taken the resume.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import office, roster, spawn
from .paths import BUS_FILE, CLAUDE_HOME, PROJECTS_DIR, SESSIONS_DIR, slug_for

#: The wake seed's first line. Pinned by tests/test_wake.py.
WAKE_NOTE = ("[Agent Deck] You were idle and have been woken because messages "
             "are waiting for you. They follow.")

REASONS = frozenset({"owner_message", "routine", "peer_message",
                     "decision_answer", "call"})

STATES = ("live", "woken", "restarted", "refused")

#: How long a desk the deck just woke counts as `live`. The collector publishes
#: the resumed session on its next tick; until then `office.live_session_ids`
#: says nobody is there, and a second message in that window must not wake it
#: a second time.
MID_RESUME_SECONDS = 30.0

#: The ceiling on the mail carried in one seed. Same number and same reason as
#: `office.HISTORY_MAX`: the seed is an argv entry and a turn the desk pays for.
#: What does not fit stays queued and reaches the desk on its next turn.
SEED_MAX = office.HISTORY_MAX

#: Below this much free space on the Claude home, a session cannot write its
#: own transcript; waking one would start a desk that dies mid-turn.
MIN_FREE_BYTES = 256 * 1024 * 1024

JOBS_DIR = spawn.JOBS_DIR


@dataclass(frozen=True)
class WakeResult:
    state: str  # one of STATES
    session_id: str
    detail: str = ""


@dataclass(frozen=True)
class Job:
    """One CLI background job: `~/.claude/jobs/<short>/state.json`."""

    short: str
    session_id: str
    cwd: str
    created_at: str


# ── where the last session comes from ────────────────────────────────────────

#: `{job dir name: (desk name, Job)}`. A job's name, session and cwd never
#: change after it is created, so each directory is parsed once; the listing is
#: re-read every call so a new job is seen at once.
_job_cache: dict[str, tuple[str, Job] | None] = {}
_job_cache_lock = threading.Lock()


def _read_job(entry: Path) -> tuple[str, Job] | None:
    try:
        state = json.loads((entry / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    name = str(state.get("name") or "")
    session_id = str(state.get("sessionId") or "")
    if not name or not session_id:
        return None
    return name, Job(short=str(state.get("daemonShort") or entry.name),
                     session_id=session_id, cwd=str(state.get("cwd") or ""),
                     created_at=str(state.get("createdAt") or ""))


def last_job(name: str, *, jobs_dir: Path | None = None,
             projects_dir: Path | None = None) -> Job | None:
    """The job at desk `name` that talked last, if its transcript still exists.

    The JOB REGISTRY, not the board: once the daemon retires a session it is
    gone from `office.json` and from `GET /v1/agents`, and the registry is the
    only record left of which session sat at the desk. A job with no transcript
    cannot be resumed -- the conversation IS the transcript -- so it is not
    offered. Never raises.

    LAST TO TALK, not last created. MEASURED on the box: a copy made and
    stopped within a minute was newer by creation than the desk's own session,
    which then ran for another hour -- and ordering by creation woke the copy.
    The transcript written last is the session that spoke last; creation time
    only breaks a tie.
    """
    jobs = Path(jobs_dir) if jobs_dir is not None else Path(JOBS_DIR)
    projects = Path(projects_dir) if projects_dir is not None else Path(PROJECTS_DIR)
    try:
        entries = [e for e in jobs.iterdir() if e.is_dir()]
    except OSError:
        return None
    mine: list[Job] = []
    with _job_cache_lock:
        for entry in entries:
            key = str(entry)
            if key not in _job_cache or _job_cache[key] is None:
                _job_cache[key] = _read_job(entry)
            found = _job_cache[key]
            if found is not None and found[0] == name and found[1].cwd:
                mine.append(found[1])
    best: tuple[float, str] | None = None
    chosen: Job | None = None
    for job in mine:
        transcript = projects / slug_for(job.cwd) / f"{job.session_id}.jsonl"
        try:
            key = (transcript.stat().st_mtime, job.created_at)
        except OSError:
            continue  # no transcript: nothing to resume
        if best is None or key > best:
            best, chosen = key, job
    return chosen


def running(session_id: str, *, sessions_dir: Path | None = None) -> bool:
    """Is the CLI itself still running `session_id`? Never raises.

    The CLI's own `~/.claude/sessions/<pid>.json`, not the board. MEASURED on
    the box: `--resume` on a session that is still running "started a copy as
    89bad41e" -- a second brain at the desk under a new id. The board only
    learns of a session on the collector's next tick, and knows nothing for the
    first seconds after the deck restarts, so its "nobody is there" is not
    enough to resume on.
    """
    folder = Path(sessions_dir) if sessions_dir is not None else Path(SESSIONS_DIR)
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return False
    for path in files:
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
            continue
        try:
            os.kill(int(entry.get("pid") or 0), 0)
        except (OSError, ValueError, TypeError):
            continue
        return True
    return False


def is_asleep(name: str) -> bool:
    """No live session at `name`, and one that can be resumed. PURE of side
    effects; this is the fact `GET /v1/agents` renders as `ASLEEP`."""
    if office.live_session_ids(name):
        return False
    return last_job(name) is not None


# ── the mail the wake carries ────────────────────────────────────────────────


def pending_for(name: str) -> list[dict]:
    """Unacked records addressed to exactly `name`, oldest first.

    A broadcast (`to == "*"`) is left to the hook, for the reason
    `app._try_inject` leaves it: carrying it here would hand it to one desk and
    mark it delivered for all of them.
    """
    try:
        lines = office.MESSAGES_FILE.read_text().splitlines()
    except OSError:
        return []
    acked: set[str] = set()
    queued: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("ack"):
            acked.add(str(rec["ack"]))
        elif rec.get("id") and rec.get("text") and rec.get("to") == name:
            queued.append(rec)
    return [r for r in queued if str(r["id"]) not in acked]


def wake_seed(pending: list[dict]) -> tuple[str, list[str]]:
    """(the seed, the ids it carries). The first message always goes in; the
    rest only while the seed stays under SEED_MAX."""
    parts: list[str] = []
    ids: list[str] = []
    used = len(WAKE_NOTE)
    for rec in pending:
        sender = str(rec.get("from") or "")
        framed = office.attribute(str(rec.get("text") or ""), sender, who=sender)
        if ids and used + len(framed) + 2 > SEED_MAX:
            break
        parts.append(framed)
        ids.append(str(rec["id"]))
        used += len(framed) + 2
    return "\n\n".join([WAKE_NOTE, *parts]), ids


# ── the default impure edges ─────────────────────────────────────────────────


def _desk_of(name: str) -> roster.Desk | None:
    try:
        desks = roster.load_roster(roster.DEFAULT_PATH)
    except Exception:
        return None
    return next((d for d in desks if d.name == name), None)


def restart_headless(desk: roster.Desk, *, roster_path=None) -> dict:
    """`spawn.start`, but never a Terminal window nobody asked for.

    On the Mac the start door opens a window; a message arriving for an
    unstarted desk must not put one in front of whatever he is doing. The box
    has no window to open and takes the headless channel.
    """
    channel = spawn.choose_channel()
    if channel.name == "terminal":
        raise spawn.SpawnError(
            "terminal_channel",
            "starting this desk would open a Terminal window; start it from "
            "the deck instead")
    return spawn.start(desk, roster_path=roster_path or roster.DEFAULT_PATH,
                       channel=channel)


def _free_bytes() -> int:
    try:
        return shutil.disk_usage(CLAUDE_HOME).free
    except OSError:
        return MIN_FREE_BYTES  # unknown is not full


def _record(event: dict) -> None:
    try:
        BUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with BUS_FILE.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass


# ── the waker ────────────────────────────────────────────────────────────────


class Waker:
    """`ensure_awake` with every impure edge injected.

    ONE WAKE PER DESK. A per-desk lock serialises callers, and a desk woken in
    the last MID_RESUME_SECONDS reads `live` -- so the owner's message, the
    routine that fires the same minute and a peer's `message_desk` produce one
    `claude --bg --resume`, not three.
    """

    def __init__(self, *,
                 live: Callable[[str], set] = office.live_session_ids,
                 running: Callable[[str], bool] = running,
                 last: Callable[[str], Job | None] = last_job,
                 desk_of: Callable[[str], roster.Desk | None] = _desk_of,
                 resume: Callable[..., str] = spawn.resume_background,
                 restart: Callable[[roster.Desk], dict] = restart_headless,
                 pending: Callable[[str], list[dict]] = pending_for,
                 ack: Callable[[str], None] = office.ack,
                 record: Callable[[dict], None] = _record,
                 free_bytes: Callable[[], int] = _free_bytes,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._live, self._last, self._desk_of = live, last, desk_of
        self._running = running
        self._resume, self._restart = resume, restart
        self._pending, self._ack, self._record = pending, ack, record
        self._free_bytes, self._clock = free_bytes, clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        #: `{desk: (clock at wake, session id)}` for the mid-resume window.
        self._recent: dict[str, tuple[float, str]] = {}

    def _lock_for(self, name: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def ensure_awake(self, name: str, *, reason: str) -> WakeResult:
        if reason not in REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
        with self._lock_for(name):
            result = self._wake(name)
            if result.state in ("woken", "restarted"):
                self._recent[name] = (self._clock(), result.session_id)
        # A name that is no desk was never a wake: a session id, a group
        # member that left. Recording it would fill the log with non-events.
        if result.state != "live" and not result.detail.startswith("not_a_desk"):
            self._record({"type": "wake", "event": "wake", "desk": name,
                          "state": result.state,
                          "session_id": result.session_id, "reason": reason,
                          "detail": result.detail, "ts": time.time()})
        return result

    def _wake(self, name: str) -> WakeResult:
        seated = sorted(self._live(name) or ())
        if seated:
            return WakeResult("live", seated[0])
        recent = self._recent.get(name)
        if recent and self._clock() - recent[0] < MID_RESUME_SECONDS:
            return WakeResult("live", recent[1], "mid_resume")

        desk = self._desk_of(name)
        if desk is None:
            return WakeResult("refused", "", f"not_a_desk: no desk named {name!r}")
        if self._free_bytes() < MIN_FREE_BYTES:
            return WakeResult("refused", "", "disk_full: too little free space "
                              "for a session to write its transcript")

        why_not = "no resumable session"
        job = self._last(name) if desk.engine == "claude" else None
        if job is not None:
            if self._running(job.session_id):
                return WakeResult("live", job.session_id, "running_off_board")
            seed, carried = wake_seed(self._pending(name))
            try:
                agent_id = self._resume(job.session_id, cwd=job.cwd, seed=seed)
            except spawn.SpawnError as exc:
                if exc.reason == "oauth_expired":
                    return WakeResult("refused", "", f"oauth_expired: {exc.detail}")
                why_not = f"{exc.reason}: {exc.detail}"
            else:
                for message_id in carried:
                    self._ack(message_id)
                if agent_id and not job.session_id.startswith(agent_id):
                    return WakeResult("woken", "",
                                      f"the CLI resumed a copy as {agent_id}")
                return WakeResult("woken", job.session_id)

        try:
            started = self._restart(desk)
        except spawn.SpawnError as exc:
            return WakeResult("refused", "", f"{exc.reason}: {exc.detail}")
        except Exception as exc:  # a start must never take the caller down
            return WakeResult("refused", "", f"restart_failed: {exc!r}")
        return WakeResult("restarted", "",
                          f"{why_not}; started {started.get('agent_id') or 'a new session'}")


_default = Waker()


def ensure_awake(name: str, *, reason: str) -> WakeResult:
    """K3's entry point, on the default waker."""
    return _default.ensure_awake(name, reason=reason)
