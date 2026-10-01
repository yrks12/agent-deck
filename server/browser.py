"""The agent's browser: one Chrome, on a display a human can walk into.

An agent that cannot open a browser cannot log into anything, cannot fill a
form, and cannot publish. Everything else in the deck is a way of deciding
*what* a desk should do; this is the first module that lets one actually do it
on the open web.

**One Chrome, two drivers.** A single Chrome runs on a virtual X display. The
agent drives it over the DevTools Protocol; the owner sees the same display in the
panel and, when the agent stops at a login or a card field, uses it directly.
Because it is literally one browser, "take over, sign in, hand back" needs no
session transfer at all -- the cookie the human's sign-in created is already in
the profile the agent will use on its next run. Every alternative design
(a headless agent browser plus a separate human one, cookie export, storage
state files) has to move credentials between two places, and moving them is the
part that leaks.

Three things this module refuses to do, each one a real way the design fails:

1. **The DevTools port is never off loopback.** Anything that can speak CDP to
   that port can `Page.navigate` to `file:///Users/you/.ssh/id_ed25519`
   and read the response. That is not a browser bug; it is what the protocol
   is. `--remote-debugging-address=127.0.0.1` is asserted in the argv rather
   than left to Chrome's default, which is a thing that can change in an
   upgrade.
2. **The sandbox is never disabled.** `--no-sandbox` is the flag every "make
   Chrome run in Docker" answer tells you to add, and adding it means any page
   the agent visits gets the agent's own privileges. `BANNED_CHROME_FLAGS` is
   swept in tests/test_browser.py.
3. **The profile never lives in a repo.** A `git checkout` would delete the
   cookies that are the entire point of persistence, and a `git add -A` would
   commit the session tokens inside them. Profiles live under the bus dir,
   which no branch switch touches, and `start()` refuses a root with a `.git`
   above it.

**Secrets.** A browser started for a desk gets `vault.env_for(desk)` and
nothing else from the ambient environment -- an unrelated key in the shell that
launched the daemon must not be readable from a process that runs untrusted
JavaScript. Every URL or excerpt that leaves this module goes through `safe()`,
which is the vault redactor composed with the shape rules in `handoff.redact`.

**Stdlib only, on purpose.** The argv builders and `subprocess` need nothing.
Actually *driving* Chrome needs a WebSocket client, which the stdlib does not
have -- that lives behind the `BrowserDriver` protocol and ships in
`server/browser_cdp.py`, whose dependency is installed in a separate venv on
the box and never in the deck's. See docs/the-browser.md.
"""

from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from . import handoff, vault
from .paths import BUS_DIR

# Everything this module writes lives here: profiles, display locks, captures.
# Under the bus dir on purpose -- the deck's own state directory, which no
# branch switch touches and no `git add -A` reaches.
DEFAULT_ROOT: Path = BUS_DIR / "browser"

CHROME_BINARY = "google-chrome"

# Asserted in the argv rather than trusted as a default. An open CDP port is
# remote code execution on the machine it runs on.
CDP_BIND = "127.0.0.1"

# Chosen to sit above anything a dev server grabs, and below the ephemeral
# range so a reservation is not fighting the kernel for the number.
CDP_PORT_BASE = 9222
MIN_CDP_PORT = 1024
MAX_CDP_PORT = 65535

# `:99` upward is the conventional headless range and is what the proven
# capture path in yair_os uses.
DISPLAY_BASE = 99
DISPLAY_MAX = 199

DEFAULT_SIZE = (1280, 800)
DEFAULT_FPS = 12
MAX_FPS = 120

# X's own claim on a display number. Ours are not the only ones.
X_LOCK_DIR = Path("/tmp")

# Never emit one of these. Each hands a page the agent visits the privileges of
# the agent itself, which makes the entire approval layer decorative -- the
# same reasoning as spawn.BANNED_FLAGS, one layer down.
BANNED_CHROME_FLAGS: frozenset[str] = frozenset({
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-web-security",
    "--remote-allow-origins",
    "--allow-running-insecure-content",
    "--disable-features=IsolateOrigins,site-per-process",
})

# A desk name becomes a directory name. It arrives from roster.json, which a
# human edits, so it is validated rather than sanitised: quietly turning
# "../../etc" into "etc" would hand Chrome a --user-data-dir nobody chose.
_DESK_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_DISPLAY_RE = re.compile(r"\A:\d{1,4}(\.\d{1,2})?\Z")

# The only schemes an origin may be granted for. `file:` is exactly the scheme
# an escaped agent would want a standing rule on, and it has no origin worth
# writing one against.
WEB_SCHEMES = ("http", "https")


class BrowserError(Exception):
    """Refusal to start or drive a browser. `reason` is a stable slug.

    Mirrors `spawn.SpawnError`: the caller gets something it can branch on
    without parsing English.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class BrowserSession:
    """One Chrome, on one display, owned by one desk.

    `pid` is Chrome's. Xvfb's pid is written into the display lock file rather
    than carried here, so that `stop()` can tear down a session it was handed
    by a different process -- which is what happens when the daemon restarts.
    """

    desk: str                # which desk owns it
    display: str             # ":99"
    cdp_port: int
    profile_dir: str         # persistent, per desk
    pid: int | None
    started_at: float


class BrowserDriver(Protocol):
    """What the rest of the deck is allowed to ask a browser to do.

    Deliberately narrow. Every method here is one an agent needs and a human
    could watch happen; there is no `evaluate(arbitrary_js)` door, because a
    driver that can run arbitrary JavaScript in a signed-in browser is a driver
    that can exfiltrate the session it was given.

    The only implementation is `browser_cdp.CDPDriver`, which needs a WebSocket
    client the stdlib does not have. Keeping it behind a protocol is what lets
    the deck stay stdlib-only and the default suite stay offline.
    """

    def goto(self, url: str) -> None: ...

    def page_state(self) -> dict:
        """The structural facts `browser_takeover.needs_human` reads."""

    def click(self, selector: str) -> None: ...

    def type_text(self, selector: str, text: str) -> None: ...

    def screenshot(self, dest: Path) -> str: ...

    def close(self) -> None: ...


# ── paths ──────────────────────────────────────────────────────────────────
#
# Layout under `root`:
#   <root>/profiles/<desk>/     Chrome's --user-data-dir, persistent
#   <root>/displays/<n>.lock    our claim on display :<n>, holds Xvfb's pid
#   <root>/screens/<desk>.mp4   x11grab capture for the panel
#
# `root` is recoverable from `profile_dir`, which is why `stop()` can release a
# display without being handed the root again.


def _check_desk(desk: str) -> str:
    name = str(desk or "")
    if not _DESK_RE.match(name):
        raise ValueError(
            "desk must be a plain name (letters, digits, dot, dash, "
            f"underscore; no separators), got {name!r}"
        )
    return name


def profile_for(desk: str, root: Path) -> Path:
    """Where `desk`'s Chrome profile lives. PURE -- creates nothing.

    Per desk and isolated: signing the Acme desk into Stripe must not sign the
    Globex desk into anything. That is the one thing the reference product
    (one VM, one browser session, one credential set for every agent) gets
    wrong, and it is a one-line mistake to repeat.

    A desk name that is not a plain name is refused, not sanitised. The name
    decides what `--user-data-dir` points at, and Chrome will happily take over
    whatever directory it is given.
    """
    name = _check_desk(desk)
    return (Path(root) / "profiles" / name).resolve()


def root_of(sess: BrowserSession) -> Path:
    """The root a session's profile was allocated under. PURE."""
    return Path(sess.profile_dir).resolve().parent.parent


def display_lock(root: Path, display: str) -> Path:
    """Our claim on a display number. PURE."""
    number = _check_display(display).lstrip(":").split(".")[0]
    return Path(root) / "displays" / f"{number}.lock"


def in_git_repo(path: Path) -> bool:
    """True if `path` or any ancestor holds a `.git`.

    A browser profile inside a repo is a profile that a branch switch deletes
    and an `add -A` commits. Checked at `start()` so the failure is a clear
    refusal now rather than a mysteriously signed-out agent later.
    """
    here = Path(path).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return True
    return False


# ── the pure argv builders ─────────────────────────────────────────────────


def _check_display(display: str) -> str:
    value = str(display or "")
    if not _DISPLAY_RE.match(value):
        raise ValueError(f"display must look like ':99', got {display!r}")
    return value


def xvfb_argv(display: str, size: tuple[int, int]) -> list[str]:
    """The virtual X server the browser and the human both look at. PURE.

    `-nolisten tcp` is the X-level twin of the loopback CDP bind. Without it
    the display is a network service, and anything that can reach it can read
    every pixel of a signed-in browser and inject keystrokes into it.
    """
    display = _check_display(display)
    width, height = _check_size(size)
    return ["Xvfb", display, "-screen", "0", f"{width}x{height}x24",
            "-nolisten", "tcp"]


def _check_size(size: tuple[int, int]) -> tuple[int, int]:
    try:
        width, height = (int(size[0]), int(size[1]))
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"size must be (width, height), got {size!r}") from exc
    if not (320 <= width <= 7680 and 240 <= height <= 4320):
        raise ValueError(f"size out of range: {size!r}")
    return width, height


def chrome_argv(*, display: str, cdp_port: int, profile_dir: str | Path,
                url: str, size: tuple[int, int] = DEFAULT_SIZE,
                binary: str = CHROME_BINARY) -> list[str]:
    """The command line Chrome is started with. PURE.

    This function is the security boundary of the whole feature, so read the
    flags rather than trusting the docstring:

    * `--remote-debugging-address=127.0.0.1` -- stated, not inherited. The
      DevTools protocol has no authentication of any kind; reachability *is*
      the access control.
    * no `--no-sandbox`, and no relative `--user-data-dir` (Chrome would
      resolve it against its own cwd, which nothing here controls).
    * `--disable-background-networking` and `--no-first-run` so a fresh profile
      does not phone home or open a welcome tab over the page the agent wants.
    """
    display = _check_display(display)
    width, height = _check_size(size)

    port = int(cdp_port)
    if not (MIN_CDP_PORT <= port <= MAX_CDP_PORT):
        raise ValueError(
            f"cdp_port must be an unprivileged port {MIN_CDP_PORT}-"
            f"{MAX_CDP_PORT}, got {cdp_port!r}"
        )

    profile = str(profile_dir)
    if not os.path.isabs(profile):
        raise ValueError(f"profile_dir must be absolute, got {profile!r}")

    target = str(url or "about:blank")

    argv = [
        binary,
        f"--display={display}",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={CDP_BIND}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--password-store=basic",
        f"--window-size={width},{height}",
        "--window-position=0,0",
        target,
    ]

    # Belt and braces: a flag added above by a future edit is caught here
    # rather than in review. The test sweep is the same list.
    for arg in argv:
        head = arg.split("=", 1)[0]
        if head in BANNED_CHROME_FLAGS or arg in BANNED_CHROME_FLAGS:
            raise ValueError(f"refusing to emit {arg!r}")
    return argv


def capture_argv(*, display: str, out_path: str | Path, fps: int = DEFAULT_FPS,
                 size: tuple[int, int] = DEFAULT_SIZE) -> list[str]:
    """ffmpeg x11grab: what the panel shows as "<Agent Name>'s screen". PURE.

    The approach is a proven capture path: x11grab on the display the work is
    happening on, H.264, yuv420p so it plays in a browser. `-preset veryfast`
    because this runs alongside a Chrome on the same box and dropping frames is
    better than starving the thing being recorded.
    """
    display = _check_display(display)
    width, height = _check_size(size)
    rate = int(fps)
    if not (1 <= rate <= MAX_FPS):
        raise ValueError(f"fps must be 1-{MAX_FPS}, got {fps!r}")
    return [
        "ffmpeg", "-y",
        "-f", "x11grab",
        "-framerate", str(rate),
        "-video_size", f"{width}x{height}",
        "-i", display,
        "-codec:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]


# ── URLs on their way out ──────────────────────────────────────────────────


def origin_of(url: str) -> str:
    """Scheme and host (and port), and nothing else. PURE.

    The origin is the only part of a URL that a durable rule may be written
    against. A full URL carries a session id, a one-time token, an order
    number -- and a rule is permanent, stored in a file every desk reads, and
    is not the vault. `https://ads.google.com` is a scope;
    `https://ads.google.com/x?session=...` is a leak with a scope attached.

    Refuses anything that is not http(s): `file:`, `data:`, `javascript:` and
    `chrome:` have no origin anybody should be granting standing access to.
    """
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in WEB_SCHEMES:
        raise ValueError(f"not a web URL: {url!r}")
    host = parts.hostname
    if not host:
        raise ValueError(f"no host in URL: {url!r}")
    # `parts.netloc` would carry user:password@; hostname and port never do.
    origin = f"{parts.scheme.lower()}://{host.lower()}"
    if parts.port:
        origin = f"{origin}:{parts.port}"
    return origin


# `vault.MARKER_FOR("STRIPE_LIVE_KEY")` -> "[redacted STRIPE_LIVE_KEY]".
_MARKER_RE = re.compile(r"\[redacted [A-Za-z_][A-Za-z0-9_]*\]")


def redactor_for(vault_path: Path = vault.DEFAULT_PATH) -> Callable[[str], str]:
    """Vault values first, then shape rules -- and the markers are protected.

    Two complementary defences and both are needed. `vault.redactor` removes
    exactly the values the vault was told about, whatever shape they are, and
    leaves a marker that *names* the key so the owner knows which one leaked.
    `handoff.redact` removes the shapes -- a bearer token, a 6-digit code, a
    `user:pass@` URL -- including secrets the vault never saw, which on the
    open web is most of them.

    Running them naively in sequence corrupts the first one's output: a URL
    ending `?key=[redacted STRIPE_LIVE_KEY]` still matches the query-parameter
    rule, which eats the marker down to `[redacted] STRIPE_LIVE_KEY]` and
    throws away the only useful part of it. So the shape rules are applied to
    the text *between* markers and never to a marker itself. Nothing is left
    unscrubbed by this: a marker is text we wrote, and the value it replaced is
    already gone.
    """
    scrub_known = vault.redactor(Path(vault_path))

    def scrub(text: str) -> str:
        known = scrub_known(str(text or ""))
        out: list[str] = []
        cursor = 0
        for match in _MARKER_RE.finditer(known):
            out.append(handoff.redact(known[cursor:match.start()]))
            out.append(match.group(0))
            cursor = match.end()
        out.append(handoff.redact(known[cursor:]))
        return "".join(out)

    return scrub


def safe(text: str, *, vault_path: Path = vault.DEFAULT_PATH) -> str:
    """Everything that leaves this module goes through here.

    A URL is the browser surface's easiest leak: session tokens and one-time
    codes live in query strings, and the URL is what goes on the board, into
    the handoff and onto a phone.
    """
    return redactor_for(vault_path)(text)


# ── display allocation ─────────────────────────────────────────────────────


def reserve_display(root: Path, *, x_lock_dir: Path = X_LOCK_DIR) -> str:
    """Claim a free display number for a new browser. Returns ":99" etc.

    Two desks starting at once must not both get `:99` -- Globex's Chrome would
    draw over Acme's, and the owner would take over the wrong browser. The claim is
    an `O_EXCL` lock file, so it holds between processes and survives a daemon
    restart, not just between two calls in one interpreter.

    X's own `/tmp/.X<n>-lock` is checked too: our lock files are not the only
    claim on a display number, and stepping on a real X server is worse than
    stepping on our own.
    """
    root = Path(root)
    (root / "displays").mkdir(parents=True, exist_ok=True)
    for number in range(DISPLAY_BASE, DISPLAY_MAX + 1):
        if (Path(x_lock_dir) / f".X{number}-lock").exists():
            continue
        lock = root / "displays" / f"{number}.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:  # pragma: no cover - unwritable state dir
            raise BrowserError("display_lock_failed", str(exc)) from exc
        os.close(fd)
        return f":{number}"
    raise BrowserError(
        "no_free_display",
        f"every display :{DISPLAY_BASE}-:{DISPLAY_MAX} is claimed",
    )


def release_display(root: Path, display: str) -> None:
    """Give a display number back. Safe to call twice.

    A display leaked on every stop is a box that runs out of screens after
    a hundred restarts, and the failure then looks like "the browser stopped
    working" rather than "a lock file was not removed".
    """
    display_lock(root, display).unlink(missing_ok=True)


def _lock_pid(root: Path, display: str) -> int | None:
    try:
        raw = display_lock(root, display).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ── the environment ────────────────────────────────────────────────────────


def chrome_env(desk: str, *, display: str, profile_dir: str | Path,
               vault_path: Path = vault.DEFAULT_PATH) -> dict[str, str]:
    """What the browser process is handed, and deliberately nothing more.

    A desk's granted secrets, plus the four variables a browser cannot start
    without. Not `os.environ`: whatever key happens to be exported in the shell
    that launched the daemon has no business inside a process that renders
    pages from the open web and runs their JavaScript.

    `HOME` points into the isolated profile so that Chrome's stray dotfiles
    (`~/.pki`, crash dumps, the GPU cache) land there rather than in the real
    home directory, where the next desk's browser would read them.
    """
    profile = str(Path(profile_dir))
    env = {
        "DISPLAY": _check_display(display),
        "HOME": profile,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "en_GB.UTF-8"),
    }
    env.update(vault.env_for(Path(vault_path), _check_desk(desk)))
    return env


# ── start and stop ─────────────────────────────────────────────────────────


def is_running(sess: BrowserSession) -> bool:
    """Is this session's Chrome still there?

    `signal 0` is the "does this pid exist and may I touch it" probe. A session
    that never started (`pid=None`) is not running, which is the honest answer
    and not an error.
    """
    if not sess.pid:
        return False
    try:
        os.kill(int(sess.pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Still running, still not ours.
        return True
    except OSError as exc:  # pragma: no cover
        return exc.errno != errno.ESRCH
    return True


def start(desk: str, *, root: Path = DEFAULT_ROOT, url: str = "about:blank",
          size: tuple[int, int] = DEFAULT_SIZE,
          vault_path: Path = vault.DEFAULT_PATH,
          binary: str = CHROME_BINARY,
          settle: float = 0.6) -> BrowserSession:
    """Bring up Xvfb and a Chrome on it for `desk`.

    Every refusal happens before anything is spawned, so a bad call leaves no
    half-started X server behind. The order is: validate the name, refuse a
    profile root inside a repo, then reserve a display, then spawn.

    Not exercised by the default suite beyond those guards -- starting a real
    Chrome belongs in `-m live` on the box (tests/test_browser_live.py).
    """
    name = _check_desk(desk)
    root = Path(root)
    profile = profile_for(name, root)

    if in_git_repo(root) or in_git_repo(profile.parent):
        raise BrowserError(
            "profile_in_repo",
            f"{root} is inside a git repo: a branch switch would delete the "
            f"browser session, and `git add -A` would commit its cookies",
        )

    profile.mkdir(parents=True, exist_ok=True)
    (root / "screens").mkdir(parents=True, exist_ok=True)

    display = reserve_display(root)
    try:
        xvfb = subprocess.Popen(
            xvfb_argv(display, size),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        display_lock(root, display).write_text(str(xvfb.pid), encoding="utf-8")
        time.sleep(settle)  # let X bind the socket before Chrome connects

        port = _free_port_from(CDP_PORT_BASE)
        chrome = subprocess.Popen(
            chrome_argv(display=display, cdp_port=port,
                        profile_dir=str(profile), url=url, size=size,
                        binary=binary),
            env=chrome_env(name, display=display, profile_dir=profile,
                           vault_path=vault_path),
            cwd=str(profile),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        release_display(root, display)
        raise BrowserError("browser_start_failed", str(exc)) from exc

    return BrowserSession(
        desk=name, display=display, cdp_port=port,
        profile_dir=str(profile), pid=chrome.pid, started_at=time.time(),
    )


def _free_port_from(base: int) -> int:
    """First port at or above `base` that nothing is listening on.

    Bound and closed immediately rather than held: CDP ports are per-Chrome and
    two browsers on one port is a confusing failure (the second one silently
    does not publish a target). A small race remains and is acceptable -- the
    loser fails loudly at start, not silently later.
    """
    import socket

    for port in range(base, min(base + 200, MAX_CDP_PORT)):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((CDP_BIND, port))
            except OSError:
                continue
        return port
    raise BrowserError("no_free_cdp_port", f"nothing free above {base}")


def stop(sess: BrowserSession) -> None:
    """Tear down Chrome, then Xvfb, then the display claim.

    Never raises. This runs from teardown and error paths, and a `stop` that
    throws is a `stop` that leaks the thing it was called to clean up. A
    session that never started is a no-op.

    Xvfb is found through the display lock rather than a field on the session,
    so a daemon that restarted can still clean up a browser it did not start.
    """
    root = root_of(sess)

    for pid in (sess.pid, _lock_pid(root, sess.display)):
        if not pid:
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(int(pid), sig)
            except (ProcessLookupError, PermissionError, OSError, ValueError):
                break
            if sig is signal.SIGTERM:
                time.sleep(0.2)
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    break

    try:
        release_display(root, sess.display)
    except (OSError, ValueError):  # pragma: no cover - best effort by design
        pass
