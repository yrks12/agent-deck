"""Idle desk browsers are stopped, woken on demand, and capped.

MEASURED on the box, 2026-10-01: 13 desk Chromium containers (240-800 MB
each, ~5.8 GB together) on a 7.9 GB, 4-core box. Swap was full, kswapd ate a
core, load hit 48, a no-op `docker exec true` took 5-10 s and one screen frame
took 15-24 s. Most of those desks were ASLEEP: nobody was using the browsers
that were taking the box down.

Every sweep (the daemon's /v1 background loop) this module asks:

  * is the desk ASLEEP / OFFLINE and nobody has touched its browser for
    two minutes?  -> stop it;
  * is it not WORKING and its browser unused for the idle window (15 min by
    default, `DECK_BROWSER_IDLE_MINUTES`)?  -> stop it;
  * are more than the cap running (3, `DECK_BROWSER_MAX_LIVE`, the daemon's
    value: see `write_states`)?  -> stop the
    least recently used idle one first. Low memory takes one more.
  * is a running desk past `TRIM_AT` of its container's memory?  -> close
    its background tabs, so a tab goes rather than the kernel's OOM killer
    taking the browser (18 such kills on 2026-10-01).

**One door in.** MEASURED 2026-10-01 19:24: nine browsers started in ~20 s
under a cap of five. Every `ensure` checked "under the cap" before any of the
others had started. `admit` is now the only way a container comes up: a file
lock (the computer tools run in each desk's own process, so a thread lock
would not do) around count -> evict the LRU idle one -> start. If nothing
idle can go, the start is refused (`browsers_full`), never squeezed in.

**A glance is not a request.** The burst was the owner clicking through nine
desks in the app: each desk page polls its screen, and every poll of a
stopped desk woke it. `ask` wakes one only once he has stayed on its screen
for `WAKE_DWELL` seconds.

A WORKING desk's browser is never stopped, and neither is one used in the last
two minutes -- the owner's screen view touches the desk on every frame.

**Logins survive.** The container's home is a bind mount (`sandbox.home_for`),
and `retire` asks Chromium to exit cleanly (SIGTERM to the browser process)
before the container is removed, so the cookie store is flushed to disk. The
next computer tool or screen view starts it again through
`desk_computer.ensure`, which also seeds the vault's logins into a freshly
launched browser.

"Last used" is a file per desk, not memory: the computer tools run in the
desk's own MCP process and the reaper in the daemon, and the disk is the only
thing they share. The desk states go the other way for the same reason, so
`make_room` -- called by `ensure` before starting a new container -- knows
who is WORKING.

Stdlib only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from pathlib import Path

from . import browser, sandbox
from .paths import BUS_DIR

IDLE_MINUTES = 15
MAX_LIVE = 3
#: Past this share of its container's memory limit, background tabs close.
TRIM_AT = 0.85
#: Polls of a stopped desk's screen for this long before its browser wakes;
#: a gap longer than `WAKE_GAP` means he left and the wait starts over.
WAKE_DWELL = 5.0
WAKE_GAP = 10.0
#: A browser used this recently is somebody's, whatever the desk's state says.
GRACE = 120.0
#: Under this fraction of RAM available, the box is in trouble.
LOW_MEMORY = 0.10
#: `make_room` trusts the daemon's states only this long.
STATES_MAX_AGE = 300.0

#: A container on no roster is swept once nobody has used it this long.
ORPHAN_IDLE = 600.0
#: A desk he looked at this recently is the one he is watching.
VIEW_WINDOW = 30.0
#: The chief of staff and the viewed desk queue this long for a slot, polling
#: every `PRIORITY_POLL`, before the least recently used one is taken anyway.
PRIORITY_WAIT = 30.0
PRIORITY_POLL = 1.0

ASLEEP_STATES = frozenset({"ASLEEP", "OFFLINE", "DEAD"})
PREFIX = "deck-desk-"

LAST_USE_DIR = BUS_DIR / "browser" / "last-use"
VIEW_DIR = BUS_DIR / "browser" / "viewing"
STATES_PATH = BUS_DIR / "browser" / "desk-states.json"
MEMORY_PATH = BUS_DIR / "memory.json"
ADMISSION_LOCK = BUS_DIR / "browser" / "admission.lock"
#: Read inside the container: with a private cgroup namespace these are its own.
CGROUP_ARGV = ["cat", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"]
_TARGET_ID = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")
MEMINFO = Path("/proc/meminfo")

#: SIGTERM to the browser process only (not its renderers), then wait up to
#: five seconds for it to go. A constant: nothing from a desk reaches it.
GRACEFUL = (
    "for p in /proc/[0-9]*; do "
    "[ \"$(cat $p/comm 2>/dev/null)\" = chromium ] || continue; "
    "grep -qa -- --type= $p/cmdline 2>/dev/null && continue; "
    "kill -TERM ${p#/proc/} 2>/dev/null; done; "
    "for i in 1 2 3 4 5 6 7 8 9 10; do alive=; "
    "for p in /proc/[0-9]*; do "
    "[ \"$(cat $p/comm 2>/dev/null)\" = chromium ] && alive=1; done; "
    "[ -z \"$alive\" ] && exit 0; sleep 0.5; done; exit 1"
)


# ── configuration ───────────────────────────────────────────────────────────


def _positive(value, default: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def settings(env=None) -> tuple[int, int]:
    """(idle seconds, max live browsers). PURE given `env`."""
    env = os.environ if env is None else env
    minutes = _positive(env.get("DECK_BROWSER_IDLE_MINUTES"), IDLE_MINUTES)
    return minutes * 60, _positive(env.get("DECK_BROWSER_MAX_LIVE"), MAX_LIVE)


# ── the decision ────────────────────────────────────────────────────────────


def plan(states: dict, running: list, last_use: dict, *, now: float,
         idle_seconds: float, max_live: int, low_memory: bool = False,
         grace: float = GRACE) -> list[str]:
    """Which running desk browsers to stop, in order. PURE.

    `states` is name -> card state; a running container whose desk is not in
    it is not ours to judge. `last_use` is name -> epoch seconds.
    """
    def used(desk: str) -> float:
        return float(last_use.get(desk) or 0.0)

    def quiet(desk: str, seconds: float) -> bool:
        return now - used(desk) >= seconds

    stop: list[str] = []
    for desk in sorted(running):
        state = states.get(desk)
        if state is None or state == "WORKING":
            continue
        if state in ASLEEP_STATES:
            if quiet(desk, grace):
                stop.append(desk)
        elif quiet(desk, max(idle_seconds, grace)):
            stop.append(desk)

    remaining = [d for d in running if d not in stop]
    excess = max(len(remaining) - max_live, 0) + (1 if low_memory else 0)
    if excess > 0:
        victims = sorted((d for d in remaining
                          if states.get(d) not in (None, "WORKING")
                          and quiet(d, grace)), key=used)
        stop.extend(victims[:excess])
    return stop


# ── last use ────────────────────────────────────────────────────────────────


def _stamp_file(path: Path, stamp: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


def touch(desk: str, *, root: Path | None = None, now: float | None = None,
          view: bool = False) -> None:
    """Mark `desk`'s browser as used now. Never raises on a disk problem.
    `view`: the owner is looking at it (the screen), not a tool driving it."""
    name = browser._check_desk(desk)
    stamp = time.time() if now is None else float(now)
    _stamp_file(Path(root or LAST_USE_DIR) / name, stamp)
    if view:
        _stamp_file(Path(VIEW_DIR) / name, stamp)


def viewing(desk: str, *, now: float, window: float = VIEW_WINDOW) -> bool:
    """Is the owner on `desk`'s screen right now?"""
    try:
        seen = (Path(VIEW_DIR) / browser._check_desk(desk)).stat().st_mtime
    except (OSError, ValueError):
        return False
    return now - seen <= window


def chief_name() -> str:
    """The chief of staff's desk name; Atlas if the roster cannot say."""
    try:
        from . import roster
        return roster.chief(roster.load_roster(roster.DEFAULT_PATH)) or "atlas"
    except Exception:  # noqa: BLE001 - a bad roster must not refuse Atlas
        return "atlas"


def last_uses(desks, *, root: Path | None = None) -> dict:
    out = {}
    base = Path(root or LAST_USE_DIR)
    for desk in desks:
        try:
            out[desk] = (base / browser._check_desk(desk)).stat().st_mtime
        except (OSError, ValueError):
            pass
    return out


# ── states, for the other process ───────────────────────────────────────────


def write_states(states: dict, path: Path | None = None, *,
                 now: float | None = None,
                 limits: tuple[int, int] | None = None) -> None:
    """`limits` is the daemon's (idle seconds, cap). MEASURED 2026-10-02:
    desks run under the claude daemon, not the deck's unit, so the
    environment a desk's computer tools read is the one it was spawned with.
    Raising the cap in agentdeck.env changed the sweep and no desk's
    `make_room`. Written here, every process obeys the daemon's value."""
    path = Path(path or STATES_PATH)
    body = {"at": time.time() if now is None else now, "states": dict(states)}
    if limits is not None:
        body["idle_seconds"], body["max_live"] = int(limits[0]), int(limits[1])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, path)
    except OSError:
        pass


def read_states(path: Path | None = None, *, now: float | None = None,
                max_age: float = STATES_MAX_AGE) -> dict | None:
    """The daemon's last word on who is WORKING, or None if it is stale."""
    try:
        body = json.loads(Path(path or STATES_PATH).read_text())
        at = float(body["at"])
        states = body["states"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (time.time() if now is None else now) - at > max_age:
        return None
    return {str(k): str(v) for k, v in states.items()} if isinstance(states, dict) else None


def published_settings(path: Path | None = None, *, now: float | None = None,
                       env=None, max_age: float = STATES_MAX_AGE
                       ) -> tuple[int, int]:
    """(idle seconds, cap): the daemon's, if it published them recently,
    else this process's environment."""
    idle, cap = settings(env)
    try:
        body = json.loads(Path(path or STATES_PATH).read_text())
        if (time.time() if now is None else now) - float(body["at"]) > max_age:
            return idle, cap
        return (_positive(body.get("idle_seconds"), idle),
                _positive(body.get("max_live"), cap))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return idle, cap


# ── memory ──────────────────────────────────────────────────────────────────


def available_fraction(path: Path | None = None) -> float | None:
    """MemAvailable / MemTotal from /proc/meminfo; None off Linux."""
    try:
        fields = {}
        for line in Path(path or MEMINFO).read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key.strip()] = float(rest.split()[0])
        return fields["MemAvailable"] / fields["MemTotal"]
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return None


def record_memory(fraction: float | None, path: Path | None = None, *,
                  now: float | None = None) -> None:
    """Remember since when RAM has been low, for `bin/deckdoctor`."""
    path = Path(path or MEMORY_PATH)
    stamp = time.time() if now is None else now
    since = low_memory_since(path)
    low = fraction is not None and fraction < LOW_MEMORY
    body = {"at": stamp, "available_fraction": fraction,
            "low_since": (since if since is not None else stamp) if low else None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, path)
    except OSError:
        pass


def low_memory_since(path: Path | None = None) -> float | None:
    try:
        value = json.loads(Path(path or MEMORY_PATH).read_text()).get("low_since")
        return None if value is None else float(value)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


# ── docker ──────────────────────────────────────────────────────────────────


def running_desks() -> list[str]:
    proc = sandbox._run(["docker", "ps", "--filter", f"name=^{PREFIX}",
                         "--format", "{{.Names}}"])
    if proc.returncode != 0:
        return []
    names = proc.stdout.decode("utf-8", "replace").split()
    return sorted(n[len(PREFIX):] for n in names if n.startswith(PREFIX))


def graceful_argv(desk: str) -> list[str]:
    """Ask the desk's Chromium to exit cleanly. PURE."""
    return sandbox.exec_argv(desk, ["bash", "-c", GRACEFUL])


def retire(desk: str) -> None:
    """Stop `desk`'s browser container, profile flushed first. Never raises."""
    try:
        sandbox._run(graceful_argv(desk), timeout=15.0)
    except (sandbox.SandboxError, ValueError):
        pass
    sandbox.stop(desk)


#: name -> when the reaper first saw it running without a last-use file.
_first_seen: dict = {}


def _uses(running, now: float) -> dict:
    uses = last_uses(running)
    for desk in running:
        if desk not in uses:
            uses[desk] = _first_seen.setdefault(desk, now)
    for desk in list(_first_seen):
        if desk not in running:
            del _first_seen[desk]
    return uses


def sweep(states: dict, *, now: float | None = None, env=None) -> list[str]:
    """One pass: record states and memory, stop what `plan` says. Returns
    the desks stopped."""
    now = time.time() if now is None else now
    idle, cap = settings(env)
    write_states(states, STATES_PATH, now=now, limits=(idle, cap))
    fraction = available_fraction()
    record_memory(fraction, MEMORY_PATH, now=now)
    running = running_desks()
    uses = _uses(running, now)
    orphans = [d for d in running if d not in states]
    stop = [d for d in orphans if now - uses.get(d, now) >= ORPHAN_IDLE]
    ours = [d for d in running if d in states]
    stop += plan(states, ours, uses, now=now, idle_seconds=idle, max_live=cap,
                 low_memory=fraction is not None and fraction < LOW_MEMORY)
    for desk in stop:
        retire(desk)
    for desk in (d for d in running if d not in stop):
        if (pressure(desk) or 0.0) >= TRIM_AT:
            trim(desk)
    return stop


def make_room(desk: str, *, now: float | None = None, env=None,
              sleep=time.sleep) -> list[str]:
    """Before `desk`'s container starts: if that would pass the cap, stop the
    least recently used idle browsers first, and refuse if none can go.
    Without fresh states nobody is known to be idle, so that refuses too.

    A container on no roster holds no slot (the reaper sweeps it when it has
    sat unused). The chief of staff and the desk he is watching are never
    refused: see `_make_room_for_priority`.
    Call it only inside `admit`."""
    now = time.time() if now is None else now
    idle, cap = published_settings(STATES_PATH, now=now, env=env)
    running = running_desks()
    states = read_states(STATES_PATH, now=now) or {}
    counted = [d for d in running if d in states] if states else running
    if desk in running or len(counted) < cap:
        return []
    if desk == chief_name() or viewing(desk, now=now):
        return _make_room_for_priority(desk, counted, cap, now=now, sleep=sleep)
    stop = plan(states, counted, last_uses(counted), now=now,
                idle_seconds=idle, max_live=max(cap - 1, 0))
    for victim in stop:
        retire(victim)
    if len(counted) - len(stop) >= cap:
        raise sandbox.SandboxError(
            "browsers_full", f"{len(counted) - len(stop)} desk browsers are "
            f"live (cap {cap}) and none is idle enough to stop")
    return stop


def _make_room_for_priority(desk: str, counted: list, cap: int, *,
                            now: float, sleep) -> list[str]:
    """One slot for the chief of staff or the desk he is watching, never a
    refusal: the least recently used browser that is not WORKING goes; if
    every slot is mid-action, wait briefly for one to finish; after that the
    least recently used goes whatever it is doing. Another desk he is
    watching is never the victim."""
    clock = now
    deadline = now + PRIORITY_WAIT
    watched = {d for d in counted if viewing(d, now=now)}
    while True:
        states = read_states(STATES_PATH, now=clock) or {}
        uses = last_uses(counted)
        spare = sorted((d for d in counted if d != desk
                        and d not in watched),
                       key=lambda d: uses.get(d, 0.0))
        idle = [d for d in spare if states.get(d) != "WORKING"]
        if idle or clock >= deadline:
            break
        sleep(PRIORITY_POLL)
        clock += PRIORITY_POLL
    victims = (idle or spare)[:max(len(counted) - cap + 1, 1)]
    for victim in victims:
        retire(victim)
    return victims


def admit(desk: str, start) -> None:
    """The one door a desk's container comes up through: under a lock every
    process shares, count, evict, start. Raises `browsers_full` at the cap."""
    path = Path(ADMISSION_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not sandbox.is_up(desk):
                make_room(desk)
                start(desk)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ── memory inside one desk ──────────────────────────────────────────────────


def pressure(desk: str) -> float | None:
    """memory.current / memory.max of `desk`'s container; None if unknown."""
    try:
        proc = sandbox._run(sandbox.exec_argv(desk, CGROUP_ARGV), timeout=10.0)
        used, limit = (int(v) for v in proc.stdout.decode().split()[:2])
        return used / limit if proc.returncode == 0 and limit else None
    except (sandbox.SandboxError, ValueError, OSError):
        return None


def trim(desk: str) -> int:
    """Close every page but the front one (the one the desk drives: the
    DevTools list is most recently used first). Returns how many."""
    from .desk_computer import ContainerCDP  # imports this module
    driver, closed = ContainerCDP(desk), 0
    pages = [t for t in driver.targets() if t.get("type") == "page"]
    for target in pages[1:]:
        if not _TARGET_ID.match(str(target.get("id") or "")):
            continue
        try:  # answers in text, not JSON, so the read fails after the close
            driver._get(f"/json/close/{target['id']}")
        except browser.BrowserError:
            pass
        closed += 1
    if closed:
        print(f"[agent-deck] reaper: {desk} near its memory limit, closed "
              f"{closed} background tab(s)", flush=True)
    return closed


# ── waking ──────────────────────────────────────────────────────────────────


def _ensure(desk: str) -> None:
    from . import desk_computer  # imports this module; late on purpose
    desk_computer.ensure(desk)


_waking: set = set()
_threads: dict = {}
_lock = threading.Lock()
#: desk -> (first ask of this stay, last ask).
_asked: dict = {}


def ask(desk: str, *, now: float | None = None) -> bool:
    """A screen poll found `desk` stopped. Wake it once he has stayed for
    `WAKE_DWELL`; until then, and while it starts, answer True ("waking")."""
    now = time.time() if now is None else now
    with _lock:
        first, last = _asked.get(desk, (now, now))
        if now - last > WAKE_GAP:
            first = now
        _asked[desk] = (first, now)
    touch_view(desk, now)
    if now - first >= WAKE_DWELL:
        with _lock:
            _asked.pop(desk, None)  # a refused wake waits a whole dwell again
        wake(desk)
    return True


def touch_view(desk: str, now: float) -> None:
    """He is on this desk's screen, though its browser is stopped."""
    try:
        _stamp_file(Path(VIEW_DIR) / browser._check_desk(desk), now)
    except ValueError:
        pass


def is_waking(desk: str) -> bool:
    with _lock:
        return desk in _waking


def wake(desk: str) -> bool:
    """Start `desk`'s computer and browser in the background. True if this
    call started it, False if it was already on its way."""
    with _lock:
        if desk in _waking:
            return False
        _waking.add(desk)

    def run() -> None:
        try:
            touch(desk)
            _ensure(desk)
        except Exception as exc:  # a failed wake is reported by the next poll
            print(f"[agent-deck] waking {desk}'s browser failed: {exc!r}",
                  flush=True)
        finally:
            with _lock:
                _waking.discard(desk)

    thread = threading.Thread(target=run, name=f"wake-{desk}", daemon=True)
    _threads[desk] = thread
    thread.start()
    return True
