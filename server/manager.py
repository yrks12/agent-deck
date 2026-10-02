"""The manager: who it is, and the one way to speak to it.

Two halves that belong together.

**The crown.** One session at a time is the manager. The deck stores that choice
next to its other bus state so a restart does not lose it. Nothing about the org
chart is stored -- that is derived from traffic every tick. All that persists is
who you are talking to.

**The write path.** Claude Code binds a UNIX socket per session and documents the
injection frame in its own startup log:

    { echo '{"type":"auth","token":"'"$CLAUDE_CODE_MESSAGING_TOKEN"'"}'
      echo '{"type":"user","message":{"role":"user","content":"hello"}}'
    } | socat - UNIX-CONNECT:<sock>

Newline-delimited JSON, one message per line. The socket is mode 0600 inside a
0700 directory, so only the owning uid can write to it -- which the daemon is.
Only the crowned pid is ever written to.

The auth line has to be the *first* line: the CLI authenticates the opening line
of a connection and ignores an auth frame anywhere after it. Auth is optional on
macOS today and required on Windows, and nothing is written back either way, so
a rejection is invisible to us -- which is exactly why we send the line rather
than rely on the setting that currently lets us skip it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import opencode_sock
from .paths import BUS_DIR, SESSIONS_DIR as _CLAUDE_SESSIONS
from . import atomic

MANAGER_FILE = BUS_DIR / "manager.json"

MAX_TEXT = 2000
CONNECT_TIMEOUT = 2.0

# Where Claude Code puts its sockets, in the order its own resolver tries.
_SOCK_DIR_CANDIDATES = (
    os.environ.get("CLAUDE_CODE_TMPDIR"),
    os.environ.get("XDG_RUNTIME_DIR"),
    "/tmp",
)


class InjectError(Exception):
    """Refusal to write. `reason` is a stable machine-readable slug."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


# ── the crown ──────────────────────────────────────────────────────────────


def read_crown() -> dict:
    """Who is currently the manager. {} when nobody is, or on any bad read."""
    try:
        data = json.loads(MANAGER_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def crown(session_id: str, pid: int, name: str) -> dict:
    """Make one session the manager, replacing whoever held it."""
    record = {
        "session_id": str(session_id),
        "pid": int(pid),
        "name": str(name),
        "since": time.time(),
    }
    try:
        MANAGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(MANAGER_FILE, json.dumps(record))
    except OSError:
        return {}
    return record


def uncrown() -> dict:
    """Nobody is the manager. Safe to call when nobody was."""
    try:
        MANAGER_FILE.unlink()
    except OSError:
        pass
    return {}


# ── the write path ─────────────────────────────────────────────────────────


def sock_dir() -> Path | None:
    """First candidate directory that actually holds sockets."""
    for base in _SOCK_DIR_CANDIDATES:
        if not base:
            continue
        candidate = Path(base) / "cc-socks"
        if candidate.is_dir():
            return candidate
        alt = Path(base) / f"cc-socks-{os.getuid()}"
        if alt.is_dir():
            return alt
    return None


def socket_path(pid: int, base: Path | None = None) -> Path | None:
    base = base or sock_dir()
    if base is None:
        return None
    path = base / f"{pid}.sock"
    # Containment: a crafted pid must not walk out of the socket directory.
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise InjectError("bad_pid", f"{path} escapes {base}")
    return path


def frame(text: str) -> bytes:
    """The exact bytes the CLI documents: one compact JSON object + newline."""
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def auth_frame(token: str) -> bytes:
    """The line the CLI's own startup log advertises, ahead of the user frame."""
    payload = {"type": "auth", "token": token}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


# Each live session publishes its peer token beside its socket, in
# <SESSIONS_DIR>/<pid>.<sha256 of the socket path>.key, mode 0600. Measured
# against Claude Code 2.1.252 -- see the spec's "Injection auth" section. The
# CLAUDE_CODE_MESSAGING_TOKEN a child process inherits is a *different* secret
# (the child token); an outside peer such as this daemon must send peerToken.
#: Main's. It was hardcoded to ~/.claude/sessions while every other reader
#: honoured CLAUDE_CONFIG_DIR; on a stock install the two are the same path.
SESSIONS_DIR = _CLAUDE_SESSIONS
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def key_file(pid: int, path: Path, sessions_dir: Path | None = None) -> Path | None:
    """The key file the CLI would have published for this pid on this socket.

    Falls back to a glob when the exact name is absent -- only when it resolves
    to exactly one file, since two candidates mean we cannot tell which socket
    the token belongs to and a wrong token is worse than none.
    """
    bases = [sessions_dir] if sessions_dir else [SESSIONS_DIR]
    if not sessions_dir:
        # A desk under a second account publishes its key in THAT account's
        # sessions dir. Pids are unique on the machine, so this cannot cross.
        from . import accounts
        bases += [folder for _ident, folder in accounts.other_dirs("sessions")]
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    for base in bases:
        exact = base / f"{pid}.{digest}.key"
        if exact.exists():
            return exact
    found: list[Path] = []
    for base in bases:
        try:
            found += sorted(base.glob(f"{pid}.*.key"))
        except OSError:
            continue
    return found[0] if len(found) == 1 else None


def messaging_token(pid: int, path: Path | None = None,
                    sessions_dir: Path | None = None) -> str | None:
    """The session's peer token, or None when nothing trustworthy is on disk.

    None is a normal answer: the file is mode 0600 and only exists while the
    session lives. Callers must keep working without it, because auth is still
    optional on macOS.
    """
    path = path or socket_path(pid)
    if path is None:
        return None
    found = key_file(pid, path, sessions_dir)
    if found is None:
        return None
    try:
        token = json.loads(found.read_text(encoding="utf-8")).get("peerToken")
    except (OSError, ValueError, AttributeError):
        return None
    # Shape-check rather than trust the file: never put a malformed secret on
    # the wire, where a bad auth frame is what gets the connection dropped.
    return token if isinstance(token, str) and _TOKEN_RE.match(token) else None


def inject(pid, text: str, *, sock_dir: Path | None = None,
           token: str | None = None) -> dict:
    """Write one user message into a session. Raises InjectError on refusal.

    Tries Claude Code's socket first; if the session is OpenCode wrapped with
    opencode-sock, fall back to the OpenCode socket.

    The auth line goes first when a token is known, because the CLI only
    honours one as the opening line of the connection. With no token the bytes
    are exactly what they have always been.
    """
    text = (text or "").strip()
    if not text:
        raise InjectError("empty", "nothing to send")
    if len(text) > MAX_TEXT:
        raise InjectError("too_long", f"{len(text)} > {MAX_TEXT}")

    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        raise InjectError("bad_pid", f"not a pid: {pid!r}")

    blob = frame(text)

    # Claude Code socket.
    path = socket_path(pid_int, sock_dir)
    if path is not None and path.exists():
        secret = token if token is not None else messaging_token(pid_int, path)
        payload = auth_frame(secret) + blob if secret else blob
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(CONNECT_TIMEOUT)
                client.connect(str(path))
                client.sendall(payload)
            return {"ok": True, "bytes": len(payload), "transport": "cc-sock",
                    "authed": bool(secret)}
        except OSError as exc:
            raise InjectError("unreachable", str(exc))

    # OpenCode socket proxy.
    try:
        return opencode_sock.inject(pid_int, text)
    except opencode_sock.InjectError:
        pass

    raise InjectError("no_socket", f"no socket for pid {pid_int}")


#: What goes down the socket when the message itself is over `MAX_TEXT`. It
#: exists to START A TURN: the office hook (`hooks/cc-office.js`, on
#: UserPromptSubmit) attaches every queued message to that turn, full length.
NUDGE = ("[Agent Deck] A message for you is waiting that is too long for this "
         "line. It is attached to this turn -- read it and act on it now.")


def inject_or_nudge(pid, text: str) -> bool:
    """`inject`, but a message too long for the socket still starts a turn.

    MEASURED 2026-09-30: an owner brief of 1,332 characters framed to 2,498,
    `inject` raised `too_long`, and the idle desk sat on it until he typed
    "hello" -- whose turn the hook then attached the brief to. So on
    `too_long` this sends `NUDGE` instead.

    True: the text itself went down (the caller may ack its record).
    False: only the nudge went; the caller must leave the record QUEUED,
    because the hook is what hands the desk the text on the nudged turn.
    Any other refusal raises `InjectError`, as `inject` does.
    """
    try:
        inject(pid, text)
        return True
    except InjectError as exc:
        if exc.reason != "too_long":
            raise
    inject(pid, NUDGE)
    return False


# ── speaking the reply ─────────────────────────────────────────────────────

SPEECH_LIMIT = 1200
def build_speak_cmd(env=None, home: Path | None = None,
                    which=shutil.which) -> list[str]:
    """argv that speaks `{text}`, or [] for "this machine has no voice".

    `DECK_SPEAK_CMD` (shell-quoted; `{text}` marks where the words go, else they
    are appended) wins. Otherwise: an mlx-tts install at ~/Applications/mlx-tts
    if this home has one, then macOS's own `say`, then silence."""
    env = os.environ if env is None else env
    raw = (env.get("DECK_SPEAK_CMD") or "").strip()
    if raw:
        try:
            argv = shlex.split(raw)
        except ValueError:
            argv = []
        if argv:
            return argv if "{text}" in argv else argv + ["{text}"]
    base = (home or Path.home()) / "Applications" / "mlx-tts"
    if (base / "speak.py").is_file():
        return [str(base / "venv" / "bin" / "python3"), str(base / "speak.py"),
                "{text}", "--speed", "1.1", "--background"]
    if which("say"):
        return ["say", "{text}"]
    return []


SPEAK_CMD = build_speak_cmd()

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE = re.compile(r"`([^`]*)`")
_LINE_LEAD = re.compile(r"^[ \t]*(?:#{1,6}|[>|]|[-*•])[ \t]*", re.MULTILINE)
_EMPHASIS = re.compile(r"[*_]{1,3}")


def strip_for_speech(text: str) -> str:
    """Prose only. Code blocks and markup do not survive text-to-speech.

    Emphasis is deleted rather than replaced with a space, so `**#372**.` stays
    `#372.` -- an issue number is worth hearing, and a stray space before the
    full stop is not.
    """
    out = _FENCE.sub(" ", text or "")
    out = _INLINE.sub(r"\1", out)
    out = _LINE_LEAD.sub("", out)   # headings, quotes, bullets: line-leading only
    out = _EMPHASIS.sub("", out)
    return " ".join(out.split())[:SPEECH_LIMIT]


def _run_speak(text: str) -> None:
    if not SPEAK_CMD:
        return
    argv = [text if part == "{text}" else part for part in SPEAK_CMD]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class ReplySpeaker:
    """Speaks exactly the manager turns we started, exactly once each.

    Armed when a message is sent; disarmed by the first reply that follows. A
    turn the manager ran on its own has nothing armed and stays silent -- which
    is what stops the daemon narrating work you did not ask about.
    """

    def __init__(self, runner=None) -> None:
        self._run = runner or _run_speak
        self._armed: dict[str, float] = {}
        self._muted: dict[str, bool] = {}
        self._spoken: set[str] = set()

    def arm(self, session_id: str, *, muted: bool = False, now: float | None = None) -> None:
        self._armed[session_id] = time.time() if now is None else now
        self._muted[session_id] = bool(muted)

    def disarm(self, session_id: str) -> None:
        self._armed.pop(session_id, None)

    def check(self, session_id: str, done_at: float, text: str, message_id: str) -> str | None:
        armed_at = self._armed.get(session_id)
        if armed_at is None or not text or not message_id:
            return None
        if done_at <= armed_at:
            return None                    # the turn ended before we asked
        if message_id in self._spoken:
            return None
        self._spoken.add(message_id)
        del self._armed[session_id]        # one message sent, one reply spoken
        if self._muted.get(session_id):
            return None
        speech = strip_for_speech(text)
        if not speech:
            return None
        self._run(speech)
        return speech
