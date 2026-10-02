"""The desk's screen as a stream: one grab per watched desk, frames pushed
when the display changes, input on the same socket.

MEASURED 2026-10-01 from the Mac over WireGuard (96 ms ping) against a probe
desk on a healthy box: one `GET screen.jpg` was 0.52-0.69 s -- `docker exec`
(60 ms) plus a fresh ffmpeg that opens the display, grabs once and exits
(230 ms) plus the round trip and 33 KB -- so back-to-back polling peaked at
1.84 fps and the app polls at 1 Hz. A typed key took 0.81-0.99 s to show.

**Why an x11grab stream, not CDP screencast or VNC.** Three were weighed:

* CDP `Page.startScreencast` sends only on change and costs nothing idle,
  but it is the *page*: no address bar, no tab strip, no native dialog, no
  permission prompt -- exactly what the owner takes over to deal with.
* x11vnc/noVNC shows everything but is a new listening service in the image,
  which `docker/desk-computer/Dockerfile` refuses on purpose, and a rebuild.
* ffmpeg is already in the image and already grabs the whole display. Kept
  running instead of restarted per frame, with `mpdecimate` dropping frames
  that did not change, it produces frames only while something moves.
  MEASURED on the box: ~12% of one core while watched at 12 fps, one 33 KB
  frame in 111 s of an idle page. Nothing runs while nobody watches.

**One feed per desk, however many viewers.** The Mac and the phone watching
the same desk share one ffmpeg. Each viewer gets a `FrameSlot`: the newest
frame wins and at most `WINDOW` frames are in flight unacknowledged, so a
phone on a bad link drops frames rather than falling behind the screen.

**The feed cannot outlive the host side.** `docker exec` dying does not kill
what it started, so the container-side line watches its own stdin: when the
deck closes it (or dies), `cat` sees EOF and kills ffmpeg. When ffmpeg dies
the line exits, and the deck sees EOF.

Input arrives on the same socket and goes through `sandbox.send_input`
unchanged -- the same validation, the same refusals, one HTTP round trip
fewer per gesture.

Stdlib only.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import threading
import time

import anyio

from . import browser_reaper, sandbox, screen

FPS = 12
QUALITY = 6
#: Unacknowledged frames in flight per viewer. Two, not one: at 12 fps a
#: frame is 83 ms, under a 96 ms ping, and a window of one would cap the
#: stream at one frame per round trip.
WINDOW = 2
#: A viewer that has not acked in this long is not holding the window.
ACK_TIMEOUT = 5.0
#: With nothing new to send, a text tick says the picture is still current.
TICK_SECONDS = 2.0
#: How often a watched desk is marked as used for the reaper.
TOUCH_SECONDS = 10.0
#: A feed nobody watches is stopped after this, not at once: closing and
#: reopening the panel must not restart ffmpeg.
LINGER = 5.0
#: A backstop on a stream the deck forgot about.
STREAM_SECONDS = 4 * 3600

#: The container-side line. Constant; `fps` and `quality` arrive as `$1` and
#: `$2`, already checked integers, never spliced into the string. `max` lets
#: an unchanged frame through every three seconds so changes under the
#: thresholds still land; an identical one is dropped by `FrameSlot`.
STREAM = (
    'ffmpeg -nostdin -loglevel error -f x11grab -draw_mouse 1 '
    '-framerate "$1" -i "$DISPLAY" '
    '-vf "mpdecimate=hi=512:lo=192:frac=0.33:max=$(( $1 * 3 ))" '
    '-fps_mode vfr -q:v "$2" -f mjpeg pipe:1 </dev/null & f=$!; '
    '{ cat >/dev/null; kill $f 2>/dev/null; } 0<&0 >/dev/null 2>&1 & w=$!; '
    'wait $f; s=$?; kill $w 2>/dev/null; exit $s'
)


def _int_in(value, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{what} must be {low}-{high}, got {value}")
    return value


def stream_argv(desk: str, *, fps: int = FPS, quality: int = QUALITY) -> list[str]:
    """The long-running grab inside `desk`'s container. PURE."""
    fps = _int_in(fps, 1, 60, "fps")
    quality = _int_in(quality, 2, 31, "quality")
    argv = sandbox.exec_argv(desk, ["timeout", str(STREAM_SECONDS), "bash", "-c",
                                    STREAM, "stream", str(fps), str(quality)])
    argv.insert(2, "--interactive")
    sandbox._sweep(argv)
    return argv


# ── MJPEG -> frames ─────────────────────────────────────────────────────────


def _frame_end(buf: bytes, start: int) -> int | None:
    """Index just past the EOI of the JPEG starting at `start`, or None if it
    is not all here yet. Walks the header segments by their lengths, so an
    FF D9 inside a table is not mistaken for the end; in the scan, FF 00 is
    a stuffed byte and FF D0-D7 a restart marker."""
    i = start + 2
    n = len(buf)
    while True:
        if i + 4 > n:
            return None
        if buf[i] != 0xFF:
            raise ValueError("not a marker")
        marker = buf[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:
            return i + 2
        length = (buf[i + 2] << 8) | buf[i + 3]
        i += 2 + length
        if marker == 0xDA:
            break
    while True:
        j = buf.find(b"\xff", i)
        if j < 0 or j + 1 >= n:
            return None
        nxt = buf[j + 1]
        if nxt == 0xD9:
            return j + 2
        i = j + 1 if nxt == 0xFF else j + 2


def split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    """Whole JPEGs out of an MJPEG byte stream, and what is left. PURE."""
    frames: list[bytes] = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            return frames, (b"\xff" if buf.endswith(b"\xff") else b"")
        try:
            end = _frame_end(buf, start)
        except ValueError:
            buf = buf[start + 2:]
            continue
        if end is None:
            return frames, buf[start:]
        frames.append(buf[start:end])
        buf = buf[end:]


# ── backpressure ────────────────────────────────────────────────────────────


class FrameSlot:
    """One viewer's queue: the newest frame wins, at most `window` in flight.

    Not thread-safe by itself; `Viewer` holds the lock.
    """

    def __init__(self, *, window: int = WINDOW, ack_timeout: float = ACK_TIMEOUT,
                 clock=time.monotonic) -> None:
        self.window = max(1, int(window))
        self.ack_timeout = ack_timeout
        self.clock = clock
        self.seq = 0
        self.dropped = 0
        self._pending: tuple[bytes, float] | None = None
        self._last: bytes | None = None
        self._in_flight: dict[int, float] = {}

    def offer(self, data: bytes, *, captured_at: float) -> None:
        if data == self._last:
            return
        self._last = data
        if self._pending is not None:
            self.dropped += 1
        self._pending = (data, captured_at)

    def take(self):
        """(seq, jpeg, captured_at) if one may go out now, else None."""
        now = self.clock()
        for seq, sent in list(self._in_flight.items()):
            if now - sent >= self.ack_timeout:
                del self._in_flight[seq]
        if self._pending is None or len(self._in_flight) >= self.window:
            return None
        data, captured = self._pending
        self._pending = None
        self.seq += 1
        self._in_flight[self.seq] = now
        return self.seq, data, captured

    def ack(self, seq: int) -> None:
        for sent in [s for s in self._in_flight if s <= seq]:
            del self._in_flight[sent]


class StreamFailed(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Viewer:
    """A `FrameSlot` the feed's thread pushes into and a socket awaits."""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, window: int = WINDOW,
                 ack_timeout: float = ACK_TIMEOUT) -> None:
        self.loop = loop
        self.slot = FrameSlot(window=window, ack_timeout=ack_timeout)
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._failed: StreamFailed | None = None

    def _wake(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            pass  # the socket's loop is gone; nothing is waiting

    def push(self, data: bytes, *, captured_at: float) -> None:
        with self._lock:
            self.slot.offer(data, captured_at=captured_at)
        self._wake()

    def fail(self, reason: str, detail: str = "") -> None:
        self._failed = StreamFailed(reason, detail)
        self._wake()

    def ack(self, seq: int) -> None:
        with self._lock:
            self.slot.ack(seq)
        self._wake()

    async def next(self, timeout: float):
        """The next frame to send, or None after `timeout` with nothing new."""
        deadline = self.loop.time() + timeout
        while True:
            if self._failed is not None:
                raise self._failed
            with self._lock:
                item = self.slot.take()
                pending = self.slot._pending is not None
            if item is not None:
                return item
            left = deadline - self.loop.time()
            if left <= 0:
                return None
            self._event.clear()
            # a frame waiting on the window is re-checked as acks time out
            wait = min(left, 0.25) if pending else left
            try:
                await asyncio.wait_for(self._event.wait(), wait)
            except asyncio.TimeoutError:
                pass


# ── the feed ────────────────────────────────────────────────────────────────


def _spawn(desk: str) -> subprocess.Popen:
    return subprocess.Popen(stream_argv(desk), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class _Feed:
    def __init__(self, hub: "StreamHub", desk: str) -> None:
        self.hub = hub
        self.desk = desk
        self.viewers: set = set()
        self.latest: tuple[bytes, float] | None = None
        self.proc = hub.spawn(desk)
        self.stopping = False
        self.timer: threading.Timer | None = None
        self.thread = threading.Thread(target=self._read, daemon=True,
                                       name=f"screen-{desk}")
        self.thread.start()

    def _read(self) -> None:
        buf = b""
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            frames, buf = split_jpegs(buf)
            if len(buf) > screen.MAX_FRAME_BYTES:
                buf = b""
            for data in frames:
                now = time.time()
                with self.hub.lock:
                    self.latest = (data, now)
                    viewers = list(self.viewers)
                for viewer in viewers:
                    viewer.push(data, captured_at=now)
        with self.hub.lock:
            viewers = list(self.viewers) if not self.stopping else []
            if self.hub.feeds.get(self.desk) is self:
                del self.hub.feeds[self.desk]
        for viewer in viewers:
            viewer.fail("stream_ended", "the screen stream stopped")
        self._close()

    def _close(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            self.proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, AttributeError):
            self.proc.kill()

    def stop(self) -> None:
        with self.hub.lock:
            if self.viewers:
                return
            self.stopping = True
            if self.hub.feeds.get(self.desk) is self:
                del self.hub.feeds[self.desk]
        self._close()


class StreamHub:
    """desk -> one running grab, shared by every viewer of that desk."""

    def __init__(self, spawn=_spawn, linger: float = LINGER) -> None:
        self.spawn = spawn
        self.linger = linger
        self.lock = threading.Lock()
        self.feeds: dict[str, _Feed] = {}

    def join(self, desk: str, viewer: Viewer) -> None:
        with self.lock:
            feed = self.feeds.get(desk)
        if feed is None:
            try:
                feed = _Feed(self, desk)
            except (OSError, ValueError, sandbox.SandboxError) as exc:
                viewer.fail("no_frame", str(exc))
                return
            with self.lock:
                self.feeds[desk] = feed
        with self.lock:
            if feed.timer is not None:
                feed.timer.cancel()
                feed.timer = None
            feed.viewers.add(viewer)
            latest = feed.latest
        if latest is not None:  # mpdecimate sends nothing until it changes
            viewer.push(latest[0], captured_at=latest[1])

    def leave(self, desk: str, viewer: Viewer) -> None:
        with self.lock:
            feed = self.feeds.get(desk)
            if feed is None:
                return
            feed.viewers.discard(viewer)
            if feed.viewers:
                return
            feed.timer = threading.Timer(self.linger, feed.stop)
            feed.timer.daemon = True
            feed.timer.start()


HUB = StreamHub()


# ── the socket ──────────────────────────────────────────────────────────────


def header(seq: int, age_seconds: float) -> bytes:
    """8 bytes before each JPEG: sequence, then age in ms, big-endian. PURE."""
    age_ms = max(0, min(int(age_seconds * 1000), 0xFFFFFFFF))
    return struct.pack(">II", seq & 0xFFFFFFFF, age_ms)


async def serve(ws, desk: str) -> None:
    """Run one accepted socket until either side goes away."""
    hub = HUB
    loop = asyncio.get_running_loop()
    viewer = Viewer(loop, window=WINDOW, ack_timeout=ACK_TIMEOUT)
    send_lock = asyncio.Lock()

    async def send_json(body: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(body))

    width, height = sandbox.SIZE
    await send_json({"type": "hello", "desk": desk, "width": width,
                     "height": height, "display": sandbox.DISPLAY,
                     "fps": FPS, "window": WINDOW})
    browser_reaper.touch(desk, view=True)
    hub.join(desk, viewer)
    inputs: asyncio.Queue = asyncio.Queue()

    async def sender() -> None:
        touched = time.monotonic()
        while True:
            try:
                item = await viewer.next(TICK_SECONDS)
            except StreamFailed as failed:
                await send_json({"type": "error", "reason": failed.reason,
                                 "detail": failed.detail, "fallback": "poll"})
                await ws.close(code=1011)
                return
            if time.monotonic() - touched >= TOUCH_SECONDS:
                browser_reaper.touch(desk, view=True)
                touched = time.monotonic()
            if item is None:
                await send_json({"type": "tick", "at": time.time()})
                continue
            seq, data, captured = item
            async with send_lock:
                await ws.send_bytes(header(seq, time.time() - captured) + data)

    async def receiver() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if not text:
                continue
            try:
                body = json.loads(text)
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            kind = body.get("type")
            if kind == "ack":
                try:
                    viewer.ack(int(body.get("seq")))
                except (TypeError, ValueError):
                    pass
            elif kind == "input":
                await inputs.put(body)

    async def worker() -> None:
        while True:
            body = dict(await inputs.get())
            ident = body.pop("id", None)
            body.pop("type", None)
            reply = {"type": "input", "id": ident, "ok": True}
            try:
                await asyncio.to_thread(sandbox.send_input, desk, body)
            except ValueError as exc:
                reply = {"type": "input", "id": ident, "ok": False,
                         "reason": "bad_input", "detail": str(exc)}
            except sandbox.SandboxError as exc:
                reply = {"type": "input", "id": ident, "ok": False,
                         "reason": exc.reason, "detail": exc.detail}
            browser_reaper.touch(desk, view=True)
            await send_json(reply)

    async def until_done(job) -> None:
        try:
            await job()
        except Exception as exc:  # one side failing ends the socket, quietly
            if not _is_disconnect(exc):
                print(f"[agent-deck] screen stream {desk}: {exc!r}", flush=True)
        group.cancel_scope.cancel()

    try:
        # anyio, not bare tasks: Starlette's own cancellation must reach all
        # three, and the first to finish ends the other two.
        async with anyio.create_task_group() as group:
            for job in (sender, receiver, worker):
                group.start_soon(until_done, job)
    finally:
        hub.leave(desk, viewer)


def _is_disconnect(exc: BaseException) -> bool:
    return type(exc).__name__ in ("WebSocketDisconnect", "ConnectionClosed",
                                  "ConnectionClosedOK", "ConnectionClosedError",
                                  "ClientDisconnected")
