"""After a deploy, every live desk runs the CURRENT deck tools -- by itself.

MEASURED 2026-10-02: Atlas, whose MCP processes predated `call_owner`, told
the owner "the calling tool isn't loaded... restart my desk". Same class as
`set_my_look`, `history`, `send_file` and the Mac control tools (PR #261,
whose reload this generalises).

Stale = a `deck`/`mac` stamp older than this code (`server/tool_stamp.py`),
a deck/mac/computer process started from other server code
(`server/code_stamp.py`), or a `--mcp-config` file older code wrote (rewritten here; a respawn re-reads
it). `Sweeper.tick` hands each LIVE stale Claude desk to the connectors'
reloader: it waits for the turn to end and runs `claude respawn` -- same job,
same conversation, never a fork. Once per tool set, retried after `retry`
seconds at most `max_tries` times. Asleep desks wake on fresh processes.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Callable

from . import atomic, code_stamp, computer_mcp, deck_mcp, mac_mcp, tool_stamp

#: How often the deck looks.
SWEEP_SECONDS = 60.0
#: The first look after the deck starts.
BOOT_SECONDS = 30.0
#: A reload waits up to 30 minutes for a turn to end; look again after that.
RETRY_SECONDS = 1800.0
#: A desk that is still stale after this many reloads is not helped by more.
MAX_TRIES = 3

NOTE = ("[Agent Deck] The deck was updated and your desk reloaded its tools "
        "between turns (same conversation). Every deck tool in your brief is "
        "loaded now{new}. Nothing to do: carry on.")


# ── the checks: each says why a desk is stale (a key), or None ───────────────


def deck_check(desk: str) -> str | None:
    return f"deck:{deck_mcp.TOOLS_SIG}" if deck_mcp.stale(desk) else None


def mac_check(desk: str) -> str | None:
    return f"mac:{mac_mcp.TOOLS_SIG_NAMES}" if mac_mcp.stale(desk) else None


def code_check(desk: str) -> str | None:
    """Any of the desk's own MCP processes started from other code. MEASURED
    2026-10-02: #319 changed `browser_reaper`, no tool schema, and no desk
    picked it up (`server/code_stamp.py`)."""
    servers = [s for s in code_stamp.SERVERS if s != computer_mcp.NAME]
    try:
        computer_mcp.server(desk)
        servers.append(computer_mcp.NAME)
    except ValueError:
        pass   # no container name: this desk runs no computer process
    if any(code_stamp.stale(s, desk) for s in servers):
        return f"code:{code_stamp.CODE_SIG}"
    return None


def config_drift(desk: str) -> str | None:
    """The desk's `--mcp-config` file, if older code wrote it, rewritten to
    what this code writes. A desk with no file was never spawned here."""
    try:
        path = deck_mcp.config_path(desk)
    except ValueError:
        return None
    if not path.exists():
        return None
    want = deck_mcp.config(desk)
    try:
        have = json.loads(path.read_text())
    except (OSError, ValueError):
        have = None
    if have == json.loads(want):
        return None
    try:
        atomic.write_text(path, want)
    except OSError:
        return None
    return "config:" + hashlib.sha256(want.encode()).hexdigest()[:16]


def default_checks() -> list[Callable[[str], str | None]]:
    return [deck_check, mac_check, code_check, config_drift]


def missing_tools(desk: str) -> list[str]:
    """The deck tools this code serves that the desk's process did not."""
    row = tool_stamp.read(deck_mcp.NAME, desk, root=deck_mcp.STAMP_ROOT) or {}
    had = set(row.get("names") or [])
    if not had:
        return []
    return sorted(t["name"] for t in deck_mcp.TOOLS if t["name"] not in had)


def note_for(desk: str) -> str:
    new = missing_tools(desk)
    return NOTE.format(new=f" (new: {', '.join(new)})" if new else "")


# ── the sweep ────────────────────────────────────────────────────────────────


def _live(name: str) -> bool:
    from . import connectors
    return connectors._live_job(name) is not None


def _reload(name: str, note: str) -> None:
    from . import connectors
    connectors.schedule_reload(name, note)


def _desks():
    from . import roster
    return roster.load_roster(roster.DEFAULT_PATH)


class Sweeper:
    """Every impure edge injected; `tick` is the whole procedure."""

    def __init__(self, *, desks=_desks, live=_live, checks=None, reload=_reload,
                 note=note_for, clock=time.time, retry: float = RETRY_SECONDS,
                 max_tries: int = MAX_TRIES) -> None:
        self.desks, self.live, self.reload, self.note = desks, live, reload, note
        self.checks = list(checks) if checks is not None else default_checks()
        self.clock, self.retry, self.max_tries = clock, retry, max_tries
        #: desk -> (why, last reload at, tries)
        self.done: dict[str, tuple[str, float, int]] = {}

    def _why(self, name: str) -> str | None:
        keys = [k for k in (check(name) for check in self.checks) if k]
        return "|".join(keys) or None

    def tick(self) -> list[str]:
        now, out = self.clock(), []
        for desk in self.desks():
            name = desk.name
            if desk.engine != "claude" or not self.live(name):
                continue
            why = self._why(name)
            if why is None:
                self.done.pop(name, None)
                continue
            last = self.done.get(name)
            tries = 0
            if last is not None and last[0] == why:
                if last[2] >= self.max_tries or now - last[1] < self.retry:
                    continue
                tries = last[2]
            self.reload(name, self.note(name))
            self.done[name] = (why, now, tries + 1)
            out.append(name)
        return out


def sweep_forever(sweeper: Sweeper | None = None, *, sleep=time.sleep,
                  log=print) -> None:
    """The deck's loop: once at start (a deploy restarts the deck), then
    every SWEEP_SECONDS. A failed tick is logged and tried again. The first
    look waits BOOT_SECONDS, for the desks' processes to reconnect."""
    sweeper = sweeper or Sweeper()
    sleep(BOOT_SECONDS)
    while True:
        try:
            for name in sweeper.tick():
                log(f"[agent-deck] tools: {name} runs older deck tools; "
                    "reloading it after its turn")
        except Exception as exc:  # noqa: BLE001 - a sweep never takes the deck down
            log(f"[agent-deck] tools: sweep failed: {exc!r}")
        sleep(SWEEP_SECONDS)
