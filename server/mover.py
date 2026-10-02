"""Move a desk to another Claude account -- never mid-turn, never twice (S8).

docs/plans/2026-10-01-two-accounts.md. `Mover.move(desk, to)`, under the
waker's per-desk lock so a wake cannot resume the desk while it is between
accounts:

  1. gate: idle >= IDLE_SECONDS on the board, no subagent running, the CLI job
     not mid-turn -- else `not_idle`. A desk nobody is seated at passes: there
     is no turn to interrupt. An open ask/handoff does NOT hold it: it is in
     the deck's ledger under the desk's NAME, not in the account. At a turn
     boundary (`at_boundary`) the CLI's word that the turn is over is the gate.
  2. stop it in its account and wait until the CLI says it is gone. If it will
     not go, nothing else happens: resuming elsewhere would be two brains.
  3. make its transcript readable in the target account (shared by symlink in
     a `deckctl login` install; copied otherwise);
  4. vouch for its folder in the target account's `.claude.json`;
  5. resume it there: `claude --bg --resume <sid> <its own flags> <note>`;
  6. success: the roster names the new account, the bus says `account_move`;
  7. failure: resume it where it was (no flags -- its job is still there),
     leave the roster alone, answer `move_failed`.

MEASURED (probes P2/P2b, box, claude 2.1.286): that resume in a second config
dir continues the SAME session id with its memory when the dir has no job for
it, and the flags carry the desk's name. A resume that answers with a different
id started a copy: the copy is stopped and the move fails.

The prompt cache is cold on the first turn after a move (accepted in the plan).
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import accounts, acct_scoped, office, pretrust, roster, spawn, wake
from .paths import slug_for

IDLE_SECONDS = 20.0
STOP_WAIT_SECONDS = 15.0
#: The board states a desk can be moved from. DONE is a finished turn.
IDLE_STATES = ("IDLE", "DONE")

NOTE = ("[Agent Deck] This desk was moved to another Claude account ({to}). Same "
        "conversation, same memory. Nothing to do about it: carry on, or wait for the "
        "next message.")
BACK_NOTE = ("[Agent Deck] A move to another Claude account did not work, so this desk "
             "is back where it was. Nothing to do about it.")


class MoveError(Exception):
    """A refusal in the API's shape: an HTTP status, a slug, a sentence."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.status, self.reason, self.detail = status, reason, detail or reason


# -- the default impure edges ------------------------------------------------


def resume_into(session_id: str, *, cwd: str, flags: list[str], note: str,
                account: str) -> str:
    """Resume `session_id` in `account` WITH its flags (that account has no job
    for it, so nothing else would carry them). Returns the id the CLI printed."""
    acct = accounts.get(account) or accounts.main_account()
    argv = ["claude", "--bg", "--resume", session_id, *flags, note]
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30,
                             **accounts.env_kw(acct))
    except (OSError, subprocess.SubprocessError) as exc:
        raise spawn.SpawnError("resume_failed", str(exc)) from exc
    said = f"{out.stdout}\n{out.stderr}".strip()
    if out.returncode != 0:
        raise spawn.SpawnError("resume_failed", said[:300] or "claude --bg --resume failed")
    found = spawn._agent_id(out.stdout)
    if not found:
        raise spawn.SpawnError("resume_failed", f"no id in {said[:200]!r}")
    return found


def ensure_transcript(src: accounts.Account, dst: accounts.Account, cwd: str,
                      session_id: str) -> None:
    """The transcript (and its subagents) where `dst`'s CLI will look."""
    there = accounts.dirs(dst).projects / slug_for(cwd)
    if (there / f"{session_id}.jsonl").exists():
        return  # shared by symlink: nothing to do
    here = accounts.dirs(src).projects / slug_for(cwd)
    there.mkdir(parents=True, exist_ok=True)
    shutil.copy2(here / f"{session_id}.jsonl", there / f"{session_id}.jsonl")
    if (here / session_id).is_dir():
        shutil.copytree(here / session_id, there / session_id, dirs_exist_ok=True)


def vouch(acct: accounts.Account, cwd: str) -> None:
    config = accounts.dirs(acct).global_config
    pretrust.accept_bypass(config_path=config)
    if Path(cwd).is_dir():
        pretrust.pretrust(cwd, roster_path=roster.DEFAULT_PATH, config_path=config)


def save_account(name: str, to: str) -> None:
    desks = roster.load_roster(roster.DEFAULT_PATH)
    stored = "" if to == accounts.DEFAULT_ID else to
    roster.save_roster(roster.DEFAULT_PATH, [
        dataclasses.replace(d, account=stored) if d.name == name else d for d in desks])


def job_state(session_id: str) -> dict | None:
    where = spawn.locate_job(session_id=session_id)
    for ident, _entry, state in spawn._job_states():
        if ident == where and state.get("sessionId") == session_id:
            return state
    return None


def has_job(session_id: str, account: str) -> bool:
    """Does `account` already hold a CLI job for this session?"""
    return any(ident == account and state.get("sessionId") == session_id
               for ident, _entry, state in spawn._job_states())


def wake_into(job, account: str, seed: str) -> str:
    """Wake a desk moved while asleep, in its NEW account (S9e): the mover's
    steps, with the wake seed as the opening prompt. Flags only where that
    account has no job for the session (MEASURED: P2b, and the S8d fork)."""
    src = accounts.get(job.account) or accounts.main_account()
    dst = accounts.get(account) or accounts.main_account()
    try:
        ensure_transcript(src, dst, job.cwd, job.session_id)
    except OSError as exc:
        raise spawn.SpawnError("resume_failed", f"transcript: {exc}") from exc
    vouch(dst, job.cwd)
    flags = [] if has_job(job.session_id, dst.id) else list(
        (job_state(job.session_id) or {}).get("respawnFlags") or [])
    return resume_into(job.session_id, cwd=job.cwd, flags=flags, note=seed,
                       account=dst.id)


def tell_desk(name: str, text: str) -> None:
    """Queued like any message: the hook hands it over on the desk's next turn,
    or the wake seed carries it if it is asleep."""
    office.send(name, text, sender="deck")


def mid_turn(state: dict) -> bool:
    """Mid-turn by the job's `tempo`, not its `state`. MEASURED 2026-10-01: a
    finished turn read `state: working`, `tempo: idle` for 18 min (music-ops).
    `blocked` waits on a prompt. No tempo (older CLI): `state`."""
    tempo = state.get("tempo")
    if tempo:
        return tempo != "idle"
    return state.get("state") == "working"


# -- the mover -----------------------------------------------------------------


class Mover:
    def __init__(self, *, card_of: Callable[[str], dict | None],
                 desk_of=wake._desk_of, last_job=wake.last_job,
                 live=office.live_session_ids, running=wake.running,
                 job_state=job_state, jobs_for=spawn.jobs_for, stop=spawn.stop_job,
                 has_job=has_job,
                 resume_into=resume_into, resume_back=spawn.resume_background,
                 ensure_transcript=ensure_transcript, vouch=vouch,
                 save_account=save_account, record=wake._record,
                 scoped=acct_scoped.note_for, tell=tell_desk,
                 clock=time.time, sleep=time.sleep,
                 lock_for: Callable | None = None) -> None:
        self.card_of, self.desk_of = card_of, desk_of
        self.last_job, self.live, self.running = last_job, live, running
        self.job_state, self.jobs_for, self.stop = job_state, jobs_for, stop
        self.has_job = has_job
        self.resume_into, self.resume_back = resume_into, resume_back
        self.ensure_transcript, self.vouch = ensure_transcript, vouch
        self.save_account, self.record = save_account, record
        self.scoped, self.tell = scoped, tell
        self.clock, self.sleep = clock, sleep
        self.lock_for = lock_for or wake._default._lock_for

    def turn_over(self, name: str) -> bool:
        """Has the desk's current turn ended (the CLI's word, not the board's)?"""
        job = self.last_job(name)
        return job is not None and not mid_turn(self.job_state(job.session_id) or {})

    def _busy(self, name: str, job: wake.Job | None, at_boundary: bool = False) -> str:
        """Why `name` cannot be moved now, or ""."""
        state = (self.job_state(job.session_id) or {}) if job else {}
        if mid_turn(state):
            return ("it is waiting on a prompt" if state.get("tempo") == "blocked"
                    else "its session is in the middle of a turn")
        card = self.card_of(name)
        if card is None and not self.live(name):
            return ""  # asleep: no turn to interrupt
        if card is None:
            return "its session is not on the board yet"
        if int(card.get("agents_running") or 0):
            return "a subagent of it is still running"
        if card.get("attention"):
            return "it is waiting on a prompt"
        if at_boundary:
            return ""  # the board lags a turn end; the CLI just said it is over
        if card.get("state") not in IDLE_STATES:
            return f"it is {str(card.get('state') or 'busy').lower()}"
        idle = self.clock() - float(card.get("state_since") or 0)
        if idle < IDLE_SECONDS:
            return f"it has been idle {idle:.0f}s, under {IDLE_SECONDS:.0f}s"
        return ""

    def move(self, name: str, to: str, *, note: str | None = None,
             at_boundary: bool = False) -> dict:
        desk = self.desk_of(name)
        if desk is None:
            raise MoveError(404, "unknown_agent", f"no desk named {name!r}")
        target = accounts.get(to)
        if target is None:
            raise MoveError(404, "no_account", f"no Claude account {to!r}; see GET /v1/accounts")
        if desk.engine != "claude":
            raise MoveError(409, "engine_not_supported", f"{desk.engine} desks have no account")
        source = accounts.for_desk(desk)
        done = {"ok": True, "moved": False, "desk": name, "from": source.id,
                "to": target.id, "session_id": ""}
        if source.id == target.id:
            return done
        with self.lock_for(name):
            job = self.last_job(name)
            why = self._busy(name, job, at_boundary)
            if why:
                raise MoveError(409, "not_idle", why)
            if job is None:  # never ran: the next start is in the new account
                self.save_account(name, target.id)
                self._told(name, source.id, target.id, "")
                return {**done, "moved": True}
            sid, cwd = job.session_id, job.cwd or desk.cwd
            if self.card_of(name) is None and not self.live(name):
                # Asleep: change its account and leave it asleep (S9e). Its
                # next wake resumes it there. MEASURED 2026-10-01: resuming 17
                # asleep desks at once left the 8 GB box 1.6 GB.
                self.save_account(name, target.id)
                self._told(name, source.id, target.id, sid)
                self._left_behind(name, job, source, target, cwd)
                return {**done, "moved": True, "session_id": sid}
            flags = list((self.job_state(sid) or {}).get("respawnFlags") or [])
            for short in self.jobs_for(name):
                self.stop(short, account=job.account)
            waited = 0.0
            while self.running(sid):
                if waited >= STOP_WAIT_SECONDS:
                    raise MoveError(502, "move_failed",
                                    "the session did not stop, so it was left where it is")
                self.sleep(1.0)
                waited += 1.0
            try:
                self.ensure_transcript(accounts.get(job.account) or source, target, cwd, sid)
                self.vouch(target, cwd)
                # Flags only where there is no job for this session. MEASURED on
                # the box (wake-probe, work -> main): a resume WITH flags in an
                # account that still holds the job "started a copy" -- the
                # job's saved options are what carry the desk there.
                if self.has_job(sid, target.id):
                    flags = []
                got = self.resume_into(sid, cwd=cwd, flags=flags,
                                       note=note or NOTE.format(to=target.label),
                                       account=target.id)
                if got and not sid.startswith(got):
                    self.stop(got, account=target.id)
                    raise spawn.SpawnError("forked", f"the CLI started a copy as {got}")
            except (spawn.SpawnError, OSError) as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                try:
                    self.resume_back(sid, cwd=cwd, seed=BACK_NOTE, account=job.account)
                except spawn.SpawnError as back:
                    detail += f"; and it could not be resumed where it was: {back.detail}"
                raise MoveError(502, "move_failed", detail) from exc
            self.save_account(name, target.id)
            self._told(name, source.id, target.id, sid)
            self._left_behind(name, job, source, target, cwd)
            return {**done, "moved": True, "session_id": sid}

    def _left_behind(self, name: str, job, source, target, cwd: str) -> None:
        """Tell the desk which claude.ai artifacts and user MCP stayed in the
        old account (acct_scoped.py). Never fails a move that already happened."""
        try:
            text = self.scoped(accounts.get(job.account) or source, target, cwd,
                               job.session_id)
            if text:
                self.tell(name, text)
                self.record({"type": "account_scoped", "event": "account_scoped",
                             "desk": name, "from": source.id, "to": target.id,
                             "ts": self.clock()})
        except Exception:  # noqa: BLE001 - the move is done; this is advice
            pass

    def _told(self, name: str, src: str, dst: str, sid: str) -> None:
        self.record({"type": "account_move", "event": "account_move", "desk": name,
                     "from": src, "to": dst, "session_id": sid, "ts": self.clock()})
