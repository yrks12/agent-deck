"""The agent's computer: a Linux container the owner can watch and take over.

`server/browser.py` and `server/screen.py` build argv for a Linux box -- Xvfb,
Chromium, `ffmpeg -f x11grab`. **None of that exists on macOS**, and the one
place it could run instead is the owner's own desktop, where an agent driving
a browser would fight him for his keyboard and share his Chrome profile. His
agents' computers are Linux. This module gives each desk one.

**One container per desk, not one shared.** The claim `docs/the-browser.md`
makes is that signing the Acme desk into Stripe signs the Globex desk into
nothing -- and inside a shared container that claim is a directory permission
in a box where both agents have a shell, which is no claim at all. Per desk it
is a kernel namespace. The cost is close to nothing: one image, N containers,
and the Xvfb and the Chromium were per desk either way.

**The container is named after the desk, and the name is the registry.**
`deck-desk-acme`. A daemon that restarted, or a Mac that rebooted, finds the
desk's computer by asking Docker for it -- there is no sidecar JSON file to
fall out of step with reality. Same reasoning as the display lock file.

**It does not come back by itself.** `--restart=no`, deliberately. A desk that
was fired, or a Mac that rebooted at 3 a.m., must not bring up a Chromium
still signed into whatever the owner signed it into during a take-over, on a
desk nobody is watching. The deck starts the computer when the desk is seated.

**Home is a bind mount under the bus dir.** `~/.claude/agent-bus/browser/
computers/<desk>` becomes `/home/agent` in the container, so the cookies, the
profile and anything the agent saved survive `docker rm`, are visible to the
owner without `docker cp`, and are not eaten by `docker volume prune`. Under
the bus dir for `browser.py`'s reason: a `git checkout` in a repo would delete
the session that is the entire point of persistence, and `git add -A` would
commit the tokens in it. `create_argv` refuses a home under a `.git`.

**How the deck reaches it, and why that is not a second door.** Only through
`docker exec` on the Docker socket -- a root-owned unix socket, not a port.
The container publishes **nothing** (`BANNED_RUN_FLAGS` sweeps for publishing
at all, not for a port number), so Chromium's DevTools port and the X display
live in the container's own network namespace and are unreachable from the
Mac's loopback, let alone the LAN. Measured: `docker port` is empty and
`nc 127.0.0.1 9222` is refused while a container is up. The only HTTP surface
is `/v1`, which is `api._authorise` and the bearer token, exactly as strong as
everything else a client can reach.

**What it is not defended against, honestly.** Measured on this Mac: a
default-bridge container resolves `host.docker.internal` and can GET the
daemon's unauthenticated `/api/state`. The two aliases are pointed at the
container's own loopback here; the raw gateway address still works and Docker
Desktop gives no container-side flag that closes it. See
docs/the-agents-computer.md -- it is written down rather than papered over.

**Chromium keeps its own sandbox, and `--security-opt seccomp=unconfined` is
what lets it.** Docker's default seccomp profile blocks `clone(CLONE_NEWUSER)`
without CAP_SYS_ADMIN, so Chromium cannot build its namespace sandbox and
refuses to start at all -- "No usable sandbox!" -- unless you add
`--no-sandbox`, which is in `browser.BANNED_CHROME_FLAGS` and would hand every
page the agent visits the agent's privileges. Trading Docker's blanket filter
for Chromium's per-renderer one is the right way round: measured, every
renderer then runs `Seccomp: 2` in a user namespace of its own. The browser
process itself is unfiltered either way, and is uid 1000 with no capabilities.

Stdlib only, and every builder here is PURE -- `docker` is only ever run by
the three thin functions at the bottom, and never through a shell.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import browser, office, screen
from .paths import BUS_DIR

#: Built from docker/desk-computer/Dockerfile. Tagged, never `:latest` -- an
#: untagged image resolves to whatever was pulled last, and the security
#: argument above is about a specific image's contents.
IMAGE = "agent-deck/desk-computer:2"

#: `deck-desk-<desk>`. Prefixed so `docker ps` tells the owner which
#: containers on his Mac are ours before he deletes one.
NAME_PREFIX = "deck-desk-"

#: The unprivileged user baked into the image. Stated on the run and on every
#: exec, never inherited: an image rebuilt with `USER root` would otherwise
#: turn every frame grab into a root command over the owner's bind mount.
DESK_UID = 1000
DESK_GID = 1000

DESK_HOME = "/home/agent"
DESK_PROFILE = f"{DESK_HOME}/chrome-profile"

#: Chromium's ProcessSingleton files. Each is a symlink whose *target* is the
#: lock: `SingletonLock -> 911ef588919a-9`, that being `<hostname>-<pid>`.
#:
#: They are the reason a signed-in desk would not come back. `stop` is
#: `docker rm --force`, so Chromium is killed mid-run and never unlinks them,
#: and `start` creates a container with a NEW hostname -- so the survivor names
#: a machine that does not exist and Chromium refuses the profile rather than
#: risk corrupting it. MEASURED on the box, on the real sequence (sign in,
#: leave the browser open, stop the desk, start it):
#:
#:     ERROR:chrome/browser/process_singleton_posix.cc:365] The profile appears
#:     to be in use by another Chromium process (10) on another computer
#:     (ba3f31d33f29). Chromium has locked the profile so that it doesn't get
#:     corrupted.
#:
#: Clearing them is safe by construction rather than by judgement: they are
#: cleared while the container does not exist, so there is no process on any
#: host that could be holding one.
SINGLETONS: tuple[str, ...] = (
    "SingletonLock", "SingletonCookie", "SingletonSocket")

#: One display per container, so the number never has to be allocated -- each
#: desk's X server is alone in its own network and IPC namespace.
#: `browser.reserve_display` exists for the shared-box case and is not used
#: here; that is the one thing containers make simpler.
DISPLAY = ":99"

#: 1600x1000, up from 1280x800 (2026-10-01): a bigger browser for the owner
#: and for the agent's own screenshots. MEASURED on the box, 20 s of non-stop scrolling through the
#: screen stream: 31% of a core and 1.6 MB/s, against 20% and 1.0 MB/s at
#: the old size; idle, both send nothing. See tests/test_desk_desktop.py.
SIZE = (1600, 1000)

#: The desktop: jwm's taskbar along the bottom (docker/desk-computer/jwmrc),
#: and the frame jwm draws round each window. Stated here because Chromium
#: is sized from them.
TASKBAR = 28
FRAME_BORDER = 3
FRAME_TITLE = 22

#: Debian's `/usr/bin/chromium` is a shell wrapper that appends flags of its
#: own (measured: `--enable-remote-extensions`, `--load-extension=`,
#: `--disable-dev-shm-usage`). A boundary asserted on our argv is not a
#: boundary if a wrapper edits it, so the real binary is named.
CHROMIUM = "/usr/lib/chromium/chromium"

#: /dev/shm defaults to 64 MB in Docker and Chromium's renderers fall over on
#: a real page below about 256 MB.
SHM_SIZE = "512m"
#: One desk's whole computer, swap included (`--memory-swap` equal to it
#: means none). MEASURED 2026-10-01: at 2g each, 13 desks on a 7.9 GB box
#: filled swap and a no-op `docker exec` took 5-10 s. Past 1g Chromium
#: loses a renderer ("Aw, Snap") instead of the box losing every desk.
#: MEASURED 2026-10-01 later: 18 kills in 24 h at 1g, busy desks peaking at
#: 630-980 MiB and killed renderers holding 400-590 MiB. With the flags in
#: `MEMORY_FLAGS` bounding the renderers, 1.25 GiB is the headroom, and
#: `browser_reaper.MAX_LIVE` (3) x this fits beside ~3.8 GB of claude
#: sessions on the 7.9 GB box. `DECK_DESK_MEMORY` overrides it.
MEMORY = "1280m"
MEMORY_MIN = 256 * 1024 ** 2
_MEMORY_RE = re.compile(r"\A([1-9][0-9]{0,5})([mg])\Z")
PIDS_LIMIT = 512

#: Chromium's own footprint, bounded. Two renderer processes at most (tabs
#: share them), a V8 heap a page cannot grow past 512 MiB (the page crashes,
#: not the container), shared memory in /tmp so it is reclaimable page cache
#: rather than unswappable shmem (one kill held 235 MiB of it), and no
#: extension, sync or default-app process waking in the background.
MEMORY_FLAGS: tuple[str, ...] = (
    "--renderer-process-limit=2",
    "--js-flags=--max-old-space-size=512",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-sync",
)


def _size(value: str) -> int:
    match = _MEMORY_RE.match(value)
    return int(match.group(1)) * 1024 ** (2 if match.group(2) == "m" else 3) \
        if match else 0


def memory_limit(env=None) -> str:
    """The desk container's memory cap, `DECK_DESK_MEMORY` or `MEMORY`. PURE
    given `env`. Garbage, or under 256 MiB, falls back rather than failing."""
    env = os.environ if env is None else env
    value = str(env.get("DECK_DESK_MEMORY") or "").strip().lower()
    return value if _size(value) >= MEMORY_MIN else MEMORY


def memory_limit_bytes(env=None) -> int:
    return _size(memory_limit(env))

#: Never emit one of these. Each is the whole feature failing open rather than
#: failing -- the same list-and-sweep as `browser.BANNED_CHROME_FLAGS`.
BANNED_RUN_FLAGS: frozenset[str] = frozenset({
    "-p", "--publish", "-P", "--publish-all",
    "--network=host", "--net=host", "--privileged",
    "--pid=host", "--ipc=host", "--userns=host",
    "--cap-add", "--security-opt=apparmor=unconfined",
})

#: What `xdotool key` will accept. Keysyms and chords only: `xdotool key
#: --file /etc/passwd` is a file read wearing a keystroke, and `-` may not
#: start the string for the same reason.
_KEYSYM_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_+]{0,39}\Z")

#: Milliseconds between synthesised keystrokes. Instant typing loses
#: characters in fields that debounce on input.
TYPE_DELAY_MS = 40

#: How long a `docker` call may take before we call it wedged. A frame grab is
#: a fraction of a second; anything near this is a container in trouble and
#: the panel needs to say so rather than hold the request open.
DOCKER_TIMEOUT = 20.0

#: The shell the terminal surface hands a typed line to. Named, like
#: `CHROMIUM` is: `sh` on Debian is dash, and a line the owner typed against
#: the bash he has everywhere else would fail on `[[` for no reason he could
#: see.
SHELL = "bash"

#: How much of a file, and of a command's output, crosses the wire. Borrowed
#: from `office.RECORD_MAX` rather than chosen again, so this machine has one
#: answer to "how much is too much" and one visible mark when it cuts.
READ_MAX = office.RECORD_MAX

#: A typed line, not a script. Past this it is a file the agent should write
#: and run, and a megabyte of argv is a way to wedge `docker exec`.
COMMAND_MAX = 8192

#: Linux's own ceiling. Longer than this is not a path, it is a payload.
PATH_MAX = 4096

#: 25 MiB. Past this a download is refused, not streamed: an 80 GB file
#: pushed through `docker exec` would hold one of the daemon's threads for as
#: long as the transfer takes, for every desk asking at once. Decided from
#: `stat_path`, before `cat` is ever built -- see `download_file`.
DOWNLOAD_MAX = 25 * 1024 * 1024


class BadPath(ValueError):
    """A path that is not a path. The **client's** fault, so it is a 400.

    A subclass rather than a bare `ValueError` because `server/api.py` tells
    the two 400s apart -- "you sent no command" and "you sent no path" are
    different things for a client to fix -- and neither is `SandboxError`,
    which is the machine's fault.
    """


class SandboxError(Exception):
    """Refusal to start or drive a desk's computer. `reason` is a stable slug.

    Mirrors `browser.BrowserError` and `spawn.SpawnError`: a caller branches
    on the slug rather than parsing English, and `server/api.py` maps it
    straight onto a documented `reason` in `docs/client-api.md`.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class Computer:
    """One desk's machine. `home` is the Mac-side path of its `/home/agent`."""

    desk: str
    name: str
    home: str
    display: str = DISPLAY


# ── names and places ───────────────────────────────────────────────────────


def container_name(desk: str) -> str:
    """`deck-desk-<desk>`. PURE.

    The desk name is `browser._check_desk`'s, imported rather than restated so
    there is one answer to "what is a legal desk name". Refused, not
    sanitised: `--name ../../etc` is a name Docker would take, and trimming it
    to its last segment hands one desk another desk's computer.
    """
    return f"{NAME_PREFIX}{browser._check_desk(desk)}"


def home_for(desk: str, root: Path = BUS_DIR / "browser" / "computers") -> Path:
    """Where `desk`'s `/home/agent` lives on the Mac. PURE -- creates nothing."""
    return (Path(root) / browser._check_desk(desk)).resolve()


def _check_path(path: str) -> str:
    """A path **inside the container**, checked, never rewritten. PURE.

    This value is attacker-adjacent: an agent's own prose reaches it, and
    prose contains `;`, backticks, `$(...)`, `|` and newlines. It is **not**
    escaped and **not** stripped, because every one of those is a legal byte
    in a Linux filename and there is no shell on this path to interpret them
    -- the path leaves as one element of an argv, and `_sweep`-style quoting
    would only corrupt the name of a file that really is called `a;b`.

    What *is* refused is the class an argv cannot defend against by itself:

    * **Not absolute.** `find -name x` is an option wearing a path, and
      `--workdir` would take `--rm` as a value. Requiring a leading `/` makes
      a leading dash impossible rather than merely unlikely.
    * **A NUL byte**, which would truncate the argument inside `execve`, so
      what the check saw and what ran would be different strings.
    * **Longer than `PATH_MAX`**, which is not a path.

    There is deliberately no confinement to `/home/agent`. The container is
    the boundary -- the terminal on this same surface is a shell in there --
    so pretending a path check is a jail would be a claim the design does not
    make. See docs/the-agents-computer.md.
    """
    value = str(path or "")
    if not value.startswith("/"):
        raise BadPath(
            f"path must be absolute inside the container, got {value!r}")
    if "\0" in value:
        raise BadPath("path contains a NUL byte")
    if len(value) > PATH_MAX:
        raise BadPath(f"path is {len(value)} characters, over {PATH_MAX}")
    return value


# ── the container ──────────────────────────────────────────────────────────


def create_argv(desk: str, *, home: Path, image: str = IMAGE,
                size: tuple[int, int] = SIZE, env=None) -> list[str]:
    """`docker run` for one desk's computer. PURE -- runs nothing.

    Read the flags rather than the docstring; this is the boundary.

    * **No `--publish`, ever.** The container holds an unauthenticated
      DevTools port and an X display with no authority. Unpublished, both are
      in a network namespace nothing on this Mac can dial.
    * **`--user`, `--cap-drop ALL`, `--security-opt no-new-privileges`.** The
      browser runs as uid 1000 with no capabilities and no route to gain any.
    * **`--security-opt seccomp=unconfined`** is what *enables* Chromium's own
      sandbox; see the module docstring. It is the one widening here and it
      buys a tighter filter on the process that actually runs hostile
      JavaScript.
    * **`--add-host ...:127.0.0.1`** blackholes the two names an agent would
      reach for to find the Mac.
    * **`--init`** so Xvfb's children are reaped; without it a container
      accumulates zombies for as long as the desk lives.

    The container's main process is Xvfb itself, not a supervisor: when the
    display dies the computer is down, which is the honest state to report,
    rather than an empty box that still says "running".
    """
    name = container_name(desk)
    path = Path(home).resolve()

    if browser.in_git_repo(path) or browser.in_git_repo(path.parent):
        raise browser.BrowserError(
            "home_in_repo",
            f"{path} is inside a git repo: a branch switch would delete the "
            f"desk's browser session, and `git add -A` would commit its cookies",
        )

    argv = [
        "docker", "run", "--detach",
        "--name", name,
        "--user", f"{DESK_UID}:{DESK_GID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # Not hardening removed -- hardening moved. See the module docstring.
        "--security-opt", "seccomp=unconfined",
        "--restart", "no",
        "--init",
        "--shm-size", SHM_SIZE,
        "--memory", memory_limit(env),
        "--memory-swap", memory_limit(env),
        "--pids-limit", str(PIDS_LIMIT),
        "--add-host", "host.docker.internal:127.0.0.1",
        "--add-host", "gateway.docker.internal:127.0.0.1",
        "--volume", f"{path}:{DESK_HOME}",
        "--env", f"HOME={DESK_HOME}",
        "--env", f"DISPLAY={DISPLAY}",
        # The terminal's prompt says whose computer this is.
        "--env", f"DECK_DESK={browser._check_desk(desk)}",
        image,
        *browser.xvfb_argv(DISPLAY, size),
    ]
    _sweep(argv)
    return argv


def exec_argv(desk: str, command: list[str], *,
              display: str = DISPLAY,
              workdir: str | None = None) -> list[str]:
    """Run `command` inside `desk`'s computer. PURE -- runs nothing.

    The uid and the display are stated on every exec. `docker exec` otherwise
    takes the image's user, so a rebuilt image is a silent privilege change;
    and ffmpeg and xdotool both act on whatever display they are handed, so an
    inherited `DISPLAY=:0` from the shell that launched the daemon would grab
    -- and type into -- the owner's own screen.

    This function is also the portability seam. Nothing above it knows about
    Docker: swap these six words for `ssh box` and every other argv in this
    module is byte-for-byte what runs on the box.

    `workdir` is Docker's own flag and not a `cd` spliced into a shell line,
    which is the one place a directory *name* would become a *command*. It is
    the reason this parameter lives here rather than a second exec builder
    existing beside this one.
    """
    return [
        "docker", "exec",
        "--user", f"{DESK_UID}:{DESK_GID}",
        "--env", f"DISPLAY={browser._check_display(display)}",
        "--env", f"HOME={DESK_HOME}",
        *(["--workdir", _check_path(workdir)] if workdir else []),
        container_name(desk),
        *[str(part) for part in command],
    ]


# ── what the owner sees ────────────────────────────────────────────────────


def frame_argv(desk: str) -> list[str]:
    """One JPEG of the desk's screen, on stdout. PURE.

    `screen.snapshot_argv` unchanged, placed inside the container. Not
    retyped: a second copy of the x11grab line is a second answer to "which
    display do we grab", and one of the two eventually reads `:0`.

    `pipe:1` rather than a file because the frame has to cross the container
    boundary anyway -- writing it inside and `docker cp`-ing it out is two
    copies and a temp file to clean up on a path that runs once a second.
    """
    return exec_argv(desk, screen.snapshot_argv(DISPLAY, "pipe:1"))


def desktop_layout(size: tuple[int, int] = SIZE) -> dict:
    """Where each window goes, as (x, y, width, height) frames. PURE.

    The browser fills the screen above the taskbar. No terminal window: the
    owner's Terminal view is the PTY (`server/terminal_stream.py`), and Both
    is the app drawing the screen and that terminal side by side. OWNER
    2026-10-01, of a desktop that tiled an xterm beside Chromium: "why I see
    terminal with the browser" -- Screen and Both looked the same, and the
    browser (and every screenshot the agent takes of it) was two thirds wide.
    """
    width, height = size
    usable = height - TASKBAR
    return {"browser": (0, 0, width, usable),
            "taskbar": (0, usable, width, TASKBAR)}


def client_size(width: int, height: int) -> tuple[int, int]:
    """A frame's inside: what a program asks for so its framed window fits."""
    return (width - 2 * FRAME_BORDER, height - FRAME_TITLE - 2 * FRAME_BORDER)


#: The desktop, started inside the container: the window manager and its
#: taskbar (docker/desk-computer/jwmrc), and the two tmux sessions -- `deck`,
#: the shell the app's Terminal attaches to, and `agent`, the read-only mirror
#: of the agent's commands. Idempotent: run twice, it starts nothing twice.
#: No terminal window opens by itself; the taskbar's Terminal and Agent
#: buttons open one on demand. Chromium is started by `browser_argv`.
#:
#: Here rather than as a script in the image, so the deck decides the desktop
#: and a desk on an older image gets the same one (image :2 carried a
#: `deck-desktop` that opened the xterm).
DESKTOP_SCRIPT = """set -u
export SHELL=/bin/bash
for _ in $(seq 50); do xdotool getdisplaygeometry >/dev/null 2>&1 && break; sleep 0.1; done
mkdir -p "$HOME/.deck" && touch "$HOME/.deck/agent.log"
if ! pgrep -u "$(id -u)" -x jwm >/dev/null; then
  setsid jwm >/dev/null 2>&1 < /dev/null &
fi
tmux has-session -t deck 2>/dev/null || tmux new-session -d -s deck
tmux has-session -t agent 2>/dev/null \\
  || tmux new-session -d -s agent "tail -n 400 -F $HOME/.deck/agent.log"
"""


def desktop_argv(desk: str, size: tuple[int, int] = SIZE) -> list[str]:
    """Start the window manager and the tmux sessions. PURE.

    Detached: nothing on the host holds it. `size` is kept for the callers;
    the desktop has nothing to place but the browser, which places itself.
    """
    argv = exec_argv(desk, ["bash", "-c", DESKTOP_SCRIPT])
    argv.insert(2, "--detach")
    return argv


def browser_argv(desk: str, *, url: str = "about:blank",
                 size: tuple[int, int] = SIZE) -> list[str]:
    """Start the desk's Chromium on its display. PURE.

    `browser.chrome_argv` unchanged, including its banned-flag sweep, so
    `--no-sandbox` cannot appear because someone found it easier than fixing
    seccomp. The profile path is the container's, under the bind-mounted home,
    so the cookies a take-over creates are still there tomorrow.
    """
    x, y, w, h = desktop_layout(size)["browser"]
    inner = browser.chrome_argv(
        display=DISPLAY, cdp_port=browser.CDP_PORT_BASE,
        profile_dir=DESK_PROFILE, url=url, size=client_size(w, h),
        position=(x, y), binary=CHROMIUM,
    )
    inner[-1:-1] = MEMORY_FLAGS  # before the url, which stays last
    return exec_argv(desk, inner)


# ── what the owner does: the take-over ─────────────────────────────────────


def _check_point(kind: str, x: int, y: int,
                 size: tuple[int, int]) -> tuple[int, int]:
    """One display-space coordinate, bounds-checked. PURE, shared.

    Every gesture below scales the same way: the client scaled a JPEG to fit
    a phone, so a coordinate is checked against the screen it was scaled
    from. Off-screen is the client's arithmetic bug and clamping it would
    hide that by acting somewhere plausible instead -- so every caller of
    this helper is a `ValueError`, never a clamp.
    """
    width, height = size
    try:
        px, py = int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{kind} needs integer coordinates, got {x!r},{y!r}") from exc
    if not (0 <= px < width and 0 <= py < height):
        raise ValueError(
            f"{kind} ({px},{py}) is outside the {width}x{height} screen")
    return px, py


def _check_button(button: int) -> str:
    """`1|2|3` -- left, middle, right. Same numbers `xdotool` already uses,
    so this is validation, not a translation table."""
    try:
        code = int(button)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"button must be 1, 2 or 3, got {button!r}") from exc
    if code not in (1, 2, 3):
        raise ValueError(f"button must be 1, 2 or 3, got {button!r}")
    return str(code)


def click_argv(desk: str, x: int, y: int, *, button: int = 1, count: int = 1,
               size: tuple[int, int] = SIZE) -> list[str]:
    """Move the pointer and click, in display coordinates. PURE.

    **No `mousemove --sync`, on any gesture in this file.** Debian 12's
    xdotool implements it as "wait until the pointer has moved", and the
    panel's hover has usually put the pointer exactly where he then clicks --
    so it never moves, and xdotool sat out its own timeout first. MEASURED on
    the box: every such click took 15.5 s to land. The wait bought nothing:
    the warp and the press go down one X connection, which the server applies
    in order. See tests/test_screen_click_is_not_late.py.

    `button=1, count=1` -- the only shape the client sent before drag/scroll
    existed -- stays the short `click 1` it always was: a second flag pair on
    the one gesture every client already sends would be a silent argv change
    for no client that asked for one. Anything else (a right-click, a
    double-click, a middle click) is the `--repeat N --delay 120 B` form,
    because `click 3 3` is not "click three times with button three" to
    `xdotool` -- repeat and button need their own flags once there is more
    than one of either.
    """
    px, py = _check_point("click", x, y, size)
    try:
        n = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"count must be 1, 2 or 3, got {count!r}") from exc
    if n not in (1, 2, 3):
        raise ValueError(f"count must be 1, 2 or 3, got {count!r}")
    b = _check_button(button)
    if b == "1" and n == 1:
        return exec_argv(desk, ["xdotool", "mousemove",
                                str(px), str(py), "click", "1"])
    return exec_argv(desk, ["xdotool", "mousemove", str(px), str(py),
                            "click", "--repeat", str(n), "--delay", "120", b])


def move_argv(desk: str, x: int, y: int,
              *, size: tuple[int, int] = SIZE) -> list[str]:
    """Move the pointer without clicking, in display coordinates. PURE.

    A hover -- a menu that opens on mouseover, a tooltip -- needs the pointer
    somewhere without a click landing there, which `click_argv` cannot do
    without also pressing a button nobody asked for.
    """
    px, py = _check_point("move", x, y, size)
    return exec_argv(desk, ["xdotool", "mousemove", str(px), str(py)])


#: `dy`/`dx` sign -> the `xdotool` wheel button it means.
_SCROLL_BUTTON = {"dy-": "5", "dy+": "4", "dx-": "6", "dx+": "7"}


def scroll_argv(desk: str, x: int, y: int, *, dy: int | None = None,
                dx: int | None = None,
                size: tuple[int, int] = SIZE) -> list[str]:
    """Move the pointer then click the wheel button that many times. PURE.

    A mouse wheel is a button to X11, not an axis -- there is no "scroll
    event", only `click 4`/`5` (vertical) and `6`/`7` (horizontal) fired
    `--repeat` times. Exactly one of `dy`/`dx` is required because a diagonal
    scroll is two wheel gestures, not one, and accepting both silently would
    have the client guess which axis actually happened. No `--delay`, unlike
    a real click: a repeated wheel click has no down/up state to space out.
    """
    px, py = _check_point("scroll", x, y, size)
    if (dy is None) == (dx is None):
        raise ValueError("scroll needs exactly one of dy or dx")
    axis, raw = ("dy", dy) if dy is not None else ("dx", dx)
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{axis} must be an integer, got {raw!r}") from exc
    if amount == 0 or not (-10 <= amount <= 10):
        raise ValueError(f"{axis} must be 1..10 or -1..-10, got {amount!r}")
    sign = "-" if amount < 0 else "+"
    button = _SCROLL_BUTTON[f"{axis}{sign}"]
    return exec_argv(desk, ["xdotool", "mousemove", str(px), str(py),
                            "click", "--repeat", str(abs(amount)), button])


def drag_argv(desk: str, x: int, y: int, to_x: int, to_y: int, *,
             button: int = 1, size: tuple[int, int] = SIZE) -> list[str]:
    """Press, move while held, release -- in that order. PURE.

    Both ends are bounds-checked, not just the start: a drag that begins
    on-screen and ends off it is still a coordinate the client's arithmetic
    got wrong, and `mouseup` at an unchecked point would drop whatever is
    being dragged wherever `xdotool` happens to clamp it.
    """
    px, py = _check_point("drag", x, y, size)
    qx, qy = _check_point("drag", to_x, to_y, size)
    b = _check_button(button)
    return exec_argv(desk, [
        "xdotool", "mousemove", str(px), str(py),
        "mousedown", b,
        "mousemove", str(qx), str(qy),
        "mouseup", b,
    ])


def type_argv(desk: str, text: str) -> list[str]:
    """Type `text` into whatever has focus. PURE.

    The owner types passwords through this during a take-over. It is argv, run
    with no shell, and the text is the last element after `--`, so a leading
    `-` is text and a backtick is a character rather than a command
    substitution in the container.
    """
    value = str(text)
    if not value:
        raise ValueError("nothing to type")
    if len(value) > 4096:
        raise ValueError(f"refusing to type {len(value)} characters")
    return exec_argv(desk, ["xdotool", "type", "--delay", str(TYPE_DELAY_MS),
                            "--", value])


def type_secret(desk: str, value: str) -> None:
    """Type a secret into whatever has focus, WITHOUT it touching argv.

    `type_argv` puts the text on the `docker exec` command line, where it sits
    in `ps` for every process on the box -- fine for a username, not for a
    password (server/vault.py, promise 1). Here xdotool reads it from stdin
    (`--file -`), so the value only ever travels down the pipe.
    """
    text = str(value or "")
    if not text:
        raise ValueError("nothing to type")
    if len(text) > 4096:
        raise ValueError(f"refusing to type {len(text)} characters")
    argv = exec_argv(desk, ["xdotool", "type", "--delay", str(TYPE_DELAY_MS),
                            "--file", "-"])
    argv.insert(2, "--interactive")
    _sweep(argv)
    try:
        proc = subprocess.run(argv, input=text.encode(), capture_output=True,
                              timeout=DOCKER_TIMEOUT)
    except FileNotFoundError as exc:
        raise SandboxError("docker_unavailable", "docker is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("computer_not_responding",
                           f"docker did not answer in {DOCKER_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise SandboxError("input_refused", "the computer refused the typing")


def key_argv(desk: str, key: str) -> list[str]:
    """Press one keysym or chord -- `Return`, `Tab`, `ctrl+l`. PURE.

    Validated against a pattern rather than escaped, because the thing being
    protected is not the shell (there is none) but xdotool's own option
    parser: `xdotool key --file /etc/passwd` reads a file, and `--window`
    aims the keystroke at a window nobody chose.
    """
    value = str(key or "")
    if not _KEYSYM_RE.match(value):
        raise ValueError(
            f"not a keysym: {key!r} -- letters, digits, `_` and `+`, no "
            f"leading dash")
    return exec_argv(desk, ["xdotool", "key", "--clearmodifiers", value])


# ── the terminal and the file tree ─────────────────────────────────────────
#
# The other two thirds of the desk's computer. All four builders below go
# through `exec_argv`, so the uid, the display and the container name are
# stated once and the `ssh box` swap above still moves everything.


def command_argv(desk: str, command: str, *,
                 cwd: str | None = None) -> list[str]:
    """A shell line, run inside `desk`'s computer. PURE -- runs nothing.

    A shell *is* wanted here: `ls | wc -l` is the feature, and the line the
    owner typed is the whole argument, passed as a single element after
    `-lc` so it is never re-split by anything on the way down.

    `cwd` is the part that is not the feature. It goes to `docker exec
    --workdir` as its own argv element rather than being spliced in as
    `cd <cwd> && ...`, because that splice is the one place a directory name
    would be read as a command.
    """
    line = str(command or "")
    if not line.strip():
        raise ValueError("nothing to run")
    if len(line) > COMMAND_MAX:
        raise ValueError(
            f"refusing to run {len(line)} characters; write a script instead")
    return exec_argv(desk, [SHELL, "-lc", line], workdir=cwd or None)


def stat_argv(desk: str, path: str) -> list[str]:
    """What `path` *is*: type, size, mtime. PURE.

    Asked before every listing and every read, so "there is no such path", "it
    is a directory" and "it is a file" are three different answers instead of
    one parsed out of `find`'s English on stderr.

    Long-form flags on purpose: `-c` in an argv that also carries a
    caller-supplied path reads, to anyone auditing it, like a shell.
    `--dereference` so a symlink to a directory is a directory, which is what
    someone clicking it in a file tree means.
    """
    return exec_argv(desk, ["stat", "--dereference", "--format=%F\t%s\t%Y",
                            "--", _check_path(path)])


def list_argv(desk: str, path: str) -> list[str]:
    """One directory, one level, inside `desk`'s computer. PURE.

    NUL-separated records, because a filename may contain a newline and a
    line-separated listing would show one file as two. The name is last and
    the three fixed fields are tab-separated, so a filename containing a tab
    survives a `split("\\t", 3)`.

    `%Y` rather than `%y`: the dereferenced type, so a symlink into a project
    is a directory you can open rather than a "file" that will not read.
    """
    return exec_argv(desk, ["find", _check_path(path),
                            "-mindepth", "1", "-maxdepth", "1",
                            "-printf", "%Y\t%s\t%T@\t%f\\0"])


def read_argv(desk: str, path: str) -> list[str]:
    """The first `READ_MAX` bytes of a file, plus one. PURE.

    The plus one is how truncation is *known* rather than guessed: reading
    exactly the ceiling cannot tell a 64 000-byte file from a 2 MB one, and a
    client shown a whole file marked "truncated" stops believing the mark.

    `--` because a file may legitimately be called `-n`.
    """
    return exec_argv(desk, ["head", "-c", str(READ_MAX + 1), "--",
                            _check_path(path)])


def download_argv(desk: str, path: str) -> list[str]:
    """The whole file, byte for byte, on stdout. PURE.

    `read_file` caps at `READ_MAX` because it renders inline as a JSON
    string; this is for a client that wants to *save* the file, so there is
    no ceiling here at all -- the refusal happens earlier, in `download_file`,
    from a `stat` the caller already has. `--` for the same reason as
    `read_argv`: a file may legitimately be called `-n`.
    """
    return exec_argv(desk, ["cat", "--", _check_path(path)])


# ── running it ─────────────────────────────────────────────────────────────


def _sweep(argv: list[str]) -> None:
    """Belt and braces: a flag added by a future edit fails here, not in review."""
    for arg in argv:
        head = arg.split("=", 1)[0]
        if arg in BANNED_RUN_FLAGS or head in BANNED_RUN_FLAGS:
            raise ValueError(f"refusing to emit {arg!r}")
        if "docker.sock" in arg:
            raise ValueError("the Docker socket is root on this Mac")


def _run(argv: list[str], *, timeout: float = DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
    """One `docker` call. Never `shell=True`, never a string.

    A `FileNotFoundError` here means Docker Desktop is not running, which is a
    fact the panel can state ("the desk's computer is off") rather than a
    traceback, so it becomes a slug.
    """
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SandboxError("docker_unavailable",
                           "docker is not on PATH; is Docker Desktop running?") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("computer_not_responding",
                           f"docker did not answer in {timeout}s") from exc


def is_up(desk: str) -> bool:
    """Is this desk's computer running right now?

    Asked of Docker, not of a file we wrote. A sidecar that says "running"
    about a container the Mac reboot took away is how a panel shows a black
    rectangle and calls it an idle agent.
    """
    proc = _run(["docker", "inspect", "-f", "{{.State.Running}}",
                 container_name(desk)])
    return proc.returncode == 0 and proc.stdout.strip() == b"true"


def prepare_home(path: Path) -> Path:
    """Make `path` a home the desk can actually use, before any container sees
    it. Returns the path. Never raises on the parts that are allowed to fail.

    Two things, and both were measured broken on the Linux box rather than
    reasoned about (see tests/test_desk_browser_persists.py for the transcripts).

    **It is handed to the uid the container runs as.** `mkdir` gives the
    directory to whoever runs the daemon. Under systemd on the box that is
    root, the container is `--user 1000:1000`, and the desk could not write to
    its own home at all: `touch /home/agent/canary.txt` came back "Permission
    denied" and Chromium died 133 without ever creating a profile. So nothing
    persisted, because nothing was ever written. This is invisible on the Mac,
    where Docker Desktop's file sharing remaps ownership -- which is exactly
    how a live test asserting "the profile is still there" stayed green while
    the product was broken on the machine it ships to.

    The chown is best-effort on purpose. On the Mac the daemon is not root, so
    it is refused, and there it does not matter. A desk must not fail to start
    over a permission the platform does not need -- same fail-open rule as
    `spawn._vouch`.

    **The locks a destroyed container left behind are cleared.** See
    `SINGLETONS`. `unlink` rather than `exists()`-then-remove, because these
    are symlinks into a container that is gone: `exists()` follows the link and
    answers False for precisely the file that is the problem.

    What it deliberately does NOT touch is anything inside the profile. The
    cookies, the login data and the local storage ARE the sign-in this whole
    route exists to keep; a "fix" that got the browser open by discarding them
    would be worse than the fault.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(path, DESK_UID, DESK_GID)
    except OSError:
        pass  # not root: the Mac, where the mount is remapped anyway
    profile = path / Path(DESK_PROFILE).name
    for lock in SINGLETONS:
        try:
            (profile / lock).unlink()
        except OSError:
            pass  # not there, or a directory we did not make -- either is fine
    return path


def start(desk: str, *, home: Path | None = None,
          image: str = IMAGE, size: tuple[int, int] = SIZE) -> Computer:
    """Bring up `desk`'s computer. Idempotent: already up is success.

    The home is prepared before Docker is told anything -- see `prepare_home`.
    Docker would otherwise create it root-owned as a side effect of the bind
    mount, and the container's uid 1000 could not write to its own home.
    """
    name = container_name(desk)
    path = Path(home) if home is not None else home_for(desk)
    if is_up(desk):
        return Computer(desk=browser._check_desk(desk), name=name, home=str(path))

    prepare_home(path)
    # A stopped container of the same name would make `docker run` fail on the
    # name rather than on anything meaningful.
    _run(["docker", "rm", "--force", name])

    proc = _run(create_argv(desk, home=path, image=image, size=size), timeout=60.0)
    if proc.returncode != 0:
        raise SandboxError("computer_start_failed",
                           browser.safe(proc.stderr.decode("utf-8", "replace").strip()))
    # The desktop is a nicety on top of a working computer: an image from
    # before it existed has no jwm or tmux, and that desk still starts.
    try:
        _run(desktop_argv(desk, size))
    except SandboxError:
        pass
    return Computer(desk=browser._check_desk(desk), name=name, home=str(path))


def stop(desk: str) -> None:
    """Take the desk's computer away. Never raises; safe to call twice.

    The home directory is left behind on purpose. It is the desk's work and
    its signed-in sessions, and `stop` is called from teardown paths where
    deleting a bind mount would be the most expensive possible side effect of
    a restart. Deleting it is `docs/the-agents-computer.md`'s one-line
    instruction, done by a person.
    """
    try:
        _run(["docker", "rm", "--force", container_name(desk)])
    except (SandboxError, ValueError):
        pass


def frame(desk: str) -> bytes:
    """The desk's screen, right now, as JPEG bytes.

    Raises rather than returning empty: a zero-byte frame renders as a broken
    image and reads as "the browser is showing nothing", which is a much
    calmer claim than "the capture failed". Same rule as `ScreenCache.put`.
    """
    proc = _run(frame_argv(desk))
    if proc.returncode != 0 or not proc.stdout:
        if not is_up(desk):
            raise SandboxError("computer_not_running",
                               f"{container_name(desk)} is not up")
        raise SandboxError(
            "no_frame",
            browser.safe(proc.stderr.decode("utf-8", "replace").strip())
            or "ffmpeg produced no frame")
    if len(proc.stdout) > screen.MAX_FRAME_BYTES:
        raise SandboxError("no_frame",
                           f"frame is {len(proc.stdout)} bytes, over the cap")
    return proc.stdout


def send_input(desk: str, action: dict) -> None:
    """One take-over gesture: click, move, scroll, drag, type or key.

    The dict is the client's, so the shape is checked here and the slug it
    fails with is documented. `ValueError` is the client's fault (400);
    `SandboxError` is the machine's (409/503).
    """
    body = action or {}
    kind = str(body.get("action") or "")
    if kind == "click":
        argv = click_argv(desk, body.get("x"), body.get("y"),
                          button=body.get("button", 1),
                          count=body.get("count", 1))
    elif kind == "move":
        argv = move_argv(desk, body.get("x"), body.get("y"))
    elif kind == "scroll":
        argv = scroll_argv(desk, body.get("x"), body.get("y"),
                           dy=body.get("dy"), dx=body.get("dx"))
    elif kind == "drag":
        argv = drag_argv(desk, body.get("x"), body.get("y"),
                         body.get("to_x"), body.get("to_y"),
                         button=body.get("button", 1))
    elif kind == "type":
        argv = type_argv(desk, body.get("text") or "")
    elif kind == "key":
        argv = key_argv(desk, body.get("key") or "")
    else:
        raise ValueError(
            f"action must be click, move, scroll, drag, type or key, "
            f"got {kind!r}")

    proc = _run(argv)
    if proc.returncode != 0:
        if not is_up(desk):
            raise SandboxError("computer_not_running",
                               f"{container_name(desk)} is not up")
        raise SandboxError(
            "input_refused",
            browser.safe(proc.stderr.decode("utf-8", "replace").strip()))


# ── the terminal and the file tree, run ────────────────────────────────────


def _ran(desk: str, argv: list[str]) -> subprocess.CompletedProcess:
    """One exec into a desk's computer, on the one clock.

    A non-zero exit is ambiguous by itself: `ls /nope` exits 2 and so does
    `docker exec` against a container that is not there. So the *only* time
    Docker is asked a second question is when something failed, and the
    answer turns the ambiguous case into `computer_not_running` -- the one
    slug the panel can render as a sentence and a button.

    `DOCKER_TIMEOUT` is passed explicitly rather than defaulted, because a
    hung command is the failure this surface invites: `_run` turns the
    timeout into `computer_not_responding` and the request ends, instead of a
    worker thread being held by a `sleep 999` someone typed.
    """
    proc = _run(argv, timeout=DOCKER_TIMEOUT)
    if proc.returncode != 0 and not is_up(desk):
        raise SandboxError(
            "computer_not_running",
            f"that agent's computer is not running ({container_name(desk)})")
    return proc


def _cut(text: str, what: str) -> tuple[str, bool]:
    """`office.fit`'s ceiling and mark, said for a stream rather than a message.

    The mark is in the text, not only in a `truncated` field, for
    `office.CUT_MARK`'s reason: a client that never learns to read the field
    must still show the owner that something was taken away.
    """
    if len(text) <= READ_MAX:
        return text, False
    lost = len(text) - READ_MAX
    return (f"{text[:READ_MAX]}\n\n{office.CUT_MARK}: {lost} more characters "
            f"of {what} did not fit the {READ_MAX}-character ceiling.", True)


def _as_text(blob: bytes, path: str, *, truncated: bool) -> str:
    """Bytes from a file, or `not_text`. Never `errors="replace"`.

    A JPEG decoded leniently is a screenful of U+FFFD that reads like a
    corrupt text file, so the caller is told which kind of thing it has rather
    than shown a plausible lie. Two ways to be binary, both swept: a NUL byte,
    which decodes perfectly well and is still not text, and bytes that are not
    UTF-8 at all.

    The trim is the false positive that rule invites. `READ_MAX` is a byte
    count, so a cut lands mid-character in any file of three-byte characters,
    and a truncated UTF-8 file must not be reported as a JPEG.
    """
    if b"\0" in blob:
        raise SandboxError(
            "not_text", f"{path} contains NUL bytes; this route serves text")
    for drop in range(4 if truncated else 1):
        try:
            return blob[:len(blob) - drop].decode("utf-8")
        except UnicodeDecodeError:
            continue
    raise SandboxError(
        "not_text", f"{path} is not valid UTF-8; this route serves text")


def _entries(blob: bytes) -> list[dict]:
    """`list_argv`'s NUL-separated records, parsed. Dirs first, then by name.

    Sorted here rather than left to the client: `find` returns directory
    order, and a file tree that reorders itself on every refresh is unusable.
    """
    rows: list[dict] = []
    for record in blob.split(b"\0"):
        if not record:
            continue
        parts = record.decode("utf-8", "replace").split("\t", 3)
        if len(parts) != 4:
            continue
        kind, size, modified, name = parts
        try:
            rows.append({"name": name,
                         "kind": "dir" if kind == "d" else "file",
                         "size": int(size), "modified": float(modified)})
        except ValueError:
            continue
    rows.sort(key=lambda row: (row["kind"] != "dir", row["name"]))
    return rows


def run_command(desk: str, command: str, *, cwd: str | None = None) -> dict:
    """One shell line in the desk's computer. Never on the Mac.

    A non-zero `exit` is returned, not raised: `grep` finding nothing is a
    result the owner wants to see, and a route that turns every failing
    command into an error page is not a terminal.
    """
    proc = _ran(desk, command_argv(desk, command, cwd=cwd))
    out, cut_out = _cut(proc.stdout.decode("utf-8", "replace"), "output")
    err, cut_err = _cut(proc.stderr.decode("utf-8", "replace"), "errors")
    return {"exit": int(proc.returncode), "stdout": out, "stderr": err,
            "truncated": bool(cut_out or cut_err)}


def stat_path(desk: str, path: str) -> dict:
    """What is at `path`: `{"kind", "regular", "size", "modified"}`."""
    proc = _ran(desk, stat_argv(desk, path))
    if proc.returncode != 0:
        raise SandboxError("no_such_path", f"{path} is not there")
    parts = proc.stdout.decode("utf-8", "replace").strip().split("\t")
    if len(parts) != 3:
        raise SandboxError("no_such_path", f"cannot read {path}")
    word, size, modified = parts
    try:
        size_i, modified_f = int(size), float(modified)
    except ValueError:
        size_i, modified_f = 0, 0.0
    return {"kind": "dir" if word == "directory" else "file",
            "regular": "regular" in word, "size": size_i,
            "modified": modified_f}


def list_dir(desk: str, path: str) -> dict:
    """One directory inside the desk's computer, one level down.

    A missing directory is `no_such_path` rather than an empty list, because
    those render identically in a panel and are opposite facts about whether
    the agent did any work.
    """
    if stat_path(desk, path)["kind"] != "dir":
        raise SandboxError("not_a_directory", f"{path} is a file")
    proc = _ran(desk, list_argv(desk, path))
    entries = _entries(proc.stdout)
    if proc.returncode != 0 and not entries:
        raise SandboxError(
            "list_failed",
            browser.safe(proc.stderr.decode("utf-8", "replace").strip())
            or f"could not list {path}")
    return {"path": _check_path(path), "entries": entries}


def read_file(desk: str, path: str) -> dict:
    """A text file inside the desk's computer, capped at `READ_MAX` bytes."""
    if not stat_path(desk, path)["regular"]:
        raise SandboxError("not_a_file", f"{path} is not a regular file")
    proc = _ran(desk, read_argv(desk, path))
    if proc.returncode != 0:
        raise SandboxError(
            "read_failed",
            browser.safe(proc.stderr.decode("utf-8", "replace").strip())
            or f"could not read {path}")

    blob = proc.stdout
    truncated = len(blob) > READ_MAX
    text = _as_text(blob[:READ_MAX], path, truncated=truncated)
    if truncated:
        text += (f"\n\n{office.CUT_MARK}: this is the first {READ_MAX} bytes "
                 f"of {path}; the rest was not read.")
    return {"path": _check_path(path), "text": text, "truncated": truncated}


def download_file(desk: str, path: str) -> dict:
    """The whole file inside the desk's computer, refused before it is read
    if it would be too big to be one response.

    The size comes from `stat_path`, asked and checked **before** `cat` is
    ever built: a `stat` is a few bytes back over `docker exec` no matter how
    large the file is, so this is how an 80 GB file becomes a `too_large`
    refusal instead of an 80 GB read held open on one of the daemon's
    threads. `read_file`'s truncate-and-mark behaviour has no equivalent
    here on purpose -- a client asking to download a file wants the bytes it
    asked for or a clean refusal, never a silently short file.
    """
    info = stat_path(desk, path)
    if not info["regular"]:
        raise SandboxError("not_a_file", f"{path} is not a regular file")
    if info["size"] > DOWNLOAD_MAX:
        raise SandboxError(
            "too_large",
            f"{path} is {info['size']} bytes, over the "
            f"{DOWNLOAD_MAX}-byte download ceiling")
    proc = _ran(desk, download_argv(desk, path))
    if proc.returncode != 0:
        raise SandboxError(
            "read_failed",
            browser.safe(proc.stderr.decode("utf-8", "replace").strip())
            or f"could not read {path}")
    return {"path": _check_path(path), "blob": proc.stdout}
