"""The desk's terminal as a real terminal: bash on a PTY, over a WebSocket.

`POST /v1/agents/{name}/terminal` runs one line and returns its output, capped
at 20 s. That is a command box, not a terminal: vim, top, ssh, a REPL and
anything that asks a question could not run in it. The owner asked for a
terminal that "looks and feels like a terminal".

**The shape.** The deck opens `docker exec --interactive --tty` on a PTY it
owns, so the docker CLI sees a real terminal, puts it in raw mode and passes
the size through. Inside the container the line is tmux, not bash:

* `deck`  -- `tmux new-session -A -s deck`: the owner's bash, persistent. A
  dropped socket, a sleeping laptop or a second viewer (the phone, the xterm
  on the desk's own desktop) all attach to the same shell.
* `agent` -- `tmux attach-session -f read-only -t agent`: read-only at the
  tmux level (and not `-r`, which also stops the view sizing the window),
  a `tail -F` of `~/.deck/agent.log`, which `hooks/cc-mirror.js` writes with
  every Bash command the desk's agent runs and its output. The desk's Bash
  runs on the box, not in the container (MEASURED: `claude --bg` is a host
  process), so this is a mirror, not the agent's own shell.

**The wire.** Binary messages are raw bytes both ways. Text messages are JSON:
`hello` first, `exit` or `error` last; from the client `resize` and `ack`.
`ack` is cumulative output bytes processed; with more than `READ_WINDOW`
unacked the deck stops reading the PTY, the PTY fills, and tmux slows down
-- a phone on a bad link stops the reading instead of the deck buffering
output without end.

**Resize** is `TIOCSWINSZ` on the master. The docker CLI holds the slave as
its controlling terminal, gets SIGWINCH and resizes the exec's tty, and tmux
redraws -- the same chain as a person resizing an ssh window.

Stdlib only.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
import time

import anyio

from . import browser_reaper, sandbox

WINDOWS: tuple[str, ...] = ("deck", "agent")
#: Unacked output bytes before the deck stops reading the PTY.
READ_WINDOW = 256 * 1024
#: One read from the PTY, at most.
CHUNK = 64 * 1024
MAX_COLS = 500
MAX_ROWS = 200
#: How often an active terminal marks the desk as used for the reaper.
TOUCH_SECONDS = 10.0
#: What the client reports as its terminal type. SwiftTerm emulates xterm.
TERM = "xterm-256color"
#: The exit code of "this image has no tmux": the container is the old image.
NO_TMUX = 127
#: tmux's default prefix and `d`: detach this client. MEASURED on the box:
#: killing `docker exec` does not end the tmux client inside the container
#: (the shim holds its tty), so every closed tab left one more client
#: attached. The key works on a read-only client too.
DETACH = b"\x02d"
#: How long a hung-up attach gets to detach before it is signalled.
DETACH_WAIT = 1.0

#: Container-side lines. Constant: the window name picks one, nothing from
#: the client is spliced in.
_NEED_TMUX = f"command -v tmux >/dev/null 2>&1 || exit {NO_TMUX}; "
LINES: dict[str, str] = {
    "deck": _NEED_TMUX + "exec tmux -u new-session -A -s deck",
    "agent": (
        _NEED_TMUX
        + 'mkdir -p "$HOME/.deck" && touch "$HOME/.deck/agent.log"; '
        + "tmux has-session -t agent 2>/dev/null || "
        + "tmux new-session -d -s agent "
        + '"tail -n 400 -F $HOME/.deck/agent.log"; '
        + "exec tmux -u attach-session -f read-only -t agent"
    ),
}


def check_window(window: str) -> str:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")
    return window


def check_size(cols, rows) -> tuple[int, int]:
    """A terminal grid, checked, never clamped. PURE."""
    for value, top, what in ((cols, MAX_COLS, "cols"), (rows, MAX_ROWS, "rows")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{what} must be an integer, got {value!r}")
        if not 1 <= value <= top:
            raise ValueError(f"{what} must be 1-{top}, got {value}")
    return cols, rows


def attach_argv(desk: str, window: str) -> list[str]:
    """`docker exec -it` into `desk`'s tmux session `window`. PURE."""
    line = LINES[check_window(window)]
    argv = sandbox.exec_argv(desk, ["bash", "-c", line],
                             workdir=sandbox.DESK_HOME)
    argv[2:2] = ["--interactive", "--tty", "--env", f"TERM={TERM}",
                 "--env", f"DECK_DESK={desk}"]
    sandbox._sweep(argv)
    return argv


# ── the PTY ─────────────────────────────────────────────────────────────────


def set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _controlling_tty() -> None:  # runs in the child, before exec
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def spawn_pty(argv: list[str], cols: int, rows: int):
    """Run `argv` with a new PTY as its terminal. Returns (proc, master_fd).

    The slave becomes the child's controlling terminal so a size change on
    the master reaches it as SIGWINCH.
    """
    master, slave = pty.openpty()
    try:
        set_size(master, cols, rows)
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                preexec_fn=_controlling_tty, close_fds=True)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    return proc, master


def _docker_spawn(argv, cols, rows):
    return spawn_pty(argv, cols, rows)


#: The seam a test replaces: (argv, cols, rows) -> (proc, master_fd).
SPAWN = _docker_spawn


async def _readable(loop: asyncio.AbstractEventLoop, fd: int) -> None:
    ready = loop.create_future()
    loop.add_reader(fd, lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        loop.remove_reader(fd)


async def _read(loop, fd: int) -> bytes:
    """Up to `CHUNK` bytes, or b"" once the other side has gone (EIO/EOF)."""
    while True:
        try:
            return os.read(fd, CHUNK)
        except BlockingIOError:
            await _readable(loop, fd)
        except OSError:
            return b""


async def _write(loop, fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            ready = loop.create_future()
            loop.add_writer(fd, lambda: ready.done() or ready.set_result(None))
            try:
                await ready
            finally:
                loop.remove_writer(fd)


def _end(proc: subprocess.Popen, fd: int) -> None:
    """Hang up the attach. The master is closed BEFORE waiting: a process
    exiting with output still queued for its terminal blocks until it drains
    (MEASURED on macOS: `tr` stuck in state E), and closing the master is what
    discards that output."""
    if proc.poll() is None:
        try:
            os.write(fd, DETACH)
            proc.wait(timeout=DETACH_WAIT)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except OSError:
            proc.terminate()
    try:
        os.close(fd)
    except OSError:
        pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


# ── the socket ──────────────────────────────────────────────────────────────


async def serve(ws, desk: str, *, window: str, cols: int, rows: int,
                spawn=None) -> None:
    """Run one accepted socket until either side goes away."""
    loop = asyncio.get_running_loop()
    read_only = window == "agent"
    spawn = spawn or SPAWN
    proc, fd = spawn(attach_argv(desk, window), cols, rows)
    sent = 0
    acked = 0
    credit = asyncio.Event()
    touched = [0.0]

    def touch() -> None:
        now = time.monotonic()
        if now - touched[0] >= TOUCH_SECONDS:
            touched[0] = now
            browser_reaper.touch(desk)

    await ws.send_text(json.dumps({
        "type": "hello", "desk": desk, "window": window,
        "windows": list(WINDOWS), "read_only": read_only,
        "cols": cols, "rows": rows}))
    touch()

    async def pump() -> None:
        nonlocal sent
        while True:
            while sent - acked >= READ_WINDOW:
                credit.clear()
                await credit.wait()
            data = await _read(loop, fd)
            if not data:
                break
            await ws.send_bytes(data)
            sent += len(data)
            touch()
        code = await asyncio.to_thread(proc.wait)
        if code == NO_TMUX and sent == 0:
            await ws.send_text(json.dumps({
                "type": "error", "reason": "terminal_unavailable",
                "detail": "this desk's computer predates the terminal; it "
                          "gets one when it next restarts",
                "fallback": "command"}))
            await ws.close(code=1011)
        else:
            await ws.send_text(json.dumps({"type": "exit", "code": code}))
            await ws.close(code=1000)

    async def receiver() -> None:
        nonlocal acked
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if not read_only and data:
                    await _write(loop, fd, data)
                    touch()
                continue
            try:
                body = json.loads(message.get("text") or "")
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            kind = body.get("type")
            if kind == "resize":
                try:
                    set_size(fd, *check_size(body.get("cols"), body.get("rows")))
                except (ValueError, OSError):
                    pass
            elif kind == "ack":
                value = body.get("bytes")
                if isinstance(value, int) and not isinstance(value, bool):
                    acked = max(acked, min(value, sent))
                    credit.set()

    tasks = [asyncio.create_task(pump()), asyncio.create_task(receiver())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not _is_disconnect(exc):
                print(f"[agent-deck] terminal {desk}/{window}: {exc!r}",
                      flush=True)
    finally:
        for task in tasks:
            task.cancel()
        # Shielded: Starlette cancels the handler when the client goes, and
        # the attach must still be hung up and its PTY closed.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(_end, proc, fd)


def _is_disconnect(exc: BaseException) -> bool:
    return type(exc).__name__ in ("WebSocketDisconnect", "ConnectionClosed",
                                  "ConnectionClosedOK", "ConnectionClosedError",
                                  "ClientDisconnected")
