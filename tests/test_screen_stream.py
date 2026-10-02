"""The desk's screen as a stream: frames pushed when it changes, input on the
same socket, the old poll as the fallback.

MEASURED 2026-10-01 from the Mac over WireGuard (96 ms ping), healthy box,
probe desk: one `GET screen.jpg` is 0.52-0.69 s -- a `docker exec` (60 ms)
plus a fresh ffmpeg (230 ms) per frame plus the round trip -- so polling
back-to-back peaks at 1.84 fps, and the app polls at 1 Hz. A typed key took
0.81-0.99 s to show up even with back-to-back polling.

So one ffmpeg per watched desk stays running (`x11grab` -> `mpdecimate` ->
MJPEG), the whole display including Chrome's own UI and native dialogs, and
only frames that differ are produced. Each viewer gets the newest frame with
at most `WINDOW` unacknowledged in flight: a slow phone drops frames, never
falls behind.

Hermetic. The feed's process is never started here; the hub is replaced at
its seam, and the parsing and backpressure are pure.
"""

import json
import struct
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import api as api_mod
from server import browser_reaper, office, sandbox, screen_stream
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}


def jpeg(body: bytes = b"\x01\x02") -> bytes:
    """A minimal baseline-shaped JPEG: SOI, a DQT segment, SOS, data, EOI."""
    dqt = b"\xff\xdb\x00\x05\x00\xff\xd9"          # a table holding FF D9
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + dqt + sos + body + b"\xff\xd9"


# ── splitting ffmpeg's MJPEG into frames ────────────────────────────────────


def test_two_frames_and_a_half_split_into_two_and_a_remainder():
    a, b, c = jpeg(b"\x01"), jpeg(b"\x02"), jpeg(b"\x03")
    frames, rest = screen_stream.split_jpegs(a + b + c[:7])
    assert frames == [a, b]
    assert rest == c[:7]
    frames, rest = screen_stream.split_jpegs(rest + c[7:])
    assert frames == [c] and rest == b""


def test_ff_d9_inside_a_header_segment_does_not_end_the_frame():
    """A naive split on FF D9 cuts this frame at the quantisation table."""
    whole = jpeg(b"\x10\x20")
    assert screen_stream.split_jpegs(whole) == ([whole], b"")


def test_stuffed_and_restart_bytes_in_the_scan_are_not_the_end():
    whole = jpeg(b"\x11\xff\x00\x22\xff\xd3\x33")
    assert screen_stream.split_jpegs(whole) == ([whole], b"")


def test_garbage_before_a_frame_is_skipped_not_fatal():
    whole = jpeg()
    assert screen_stream.split_jpegs(b"junk" + whole) == ([whole], b"")


# ── the one long-running grab ───────────────────────────────────────────────


def test_the_stream_runs_inside_the_desk_container_on_its_display():
    argv = screen_stream.stream_argv("acme")
    prefix = sandbox.exec_argv("acme", [])
    assert argv[:2] == ["docker", "exec"] and "--interactive" in argv
    assert "deck-desk-acme" in argv
    line = argv[-4]
    assert line == screen_stream.STREAM
    assert "x11grab" in line and "mpdecimate" in line and "mjpeg" in line
    assert "DISPLAY=:99" in argv and '-i "$DISPLAY"' in line
    assert "-nostdin" in line
    # the stdin-EOF kill: a dead host side cannot leave ffmpeg running
    assert "cat >/dev/null" in line and "kill" in line
    assert len(prefix) > 0


def test_fps_and_quality_are_arguments_not_spliced_into_the_shell():
    argv = screen_stream.stream_argv("acme", fps=8, quality=9)
    assert argv[-3:] == ["stream", "8", "9"]
    for bad in (dict(fps=0), dict(fps=61), dict(quality=1), dict(quality=32),
                dict(fps="8; rm -rf /")):
        with pytest.raises(ValueError):
            screen_stream.stream_argv("acme", **bad)


# ── backpressure: newest wins, at most WINDOW in flight ─────────────────────


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_at_most_window_frames_are_in_flight_without_an_ack():
    slot = screen_stream.FrameSlot(window=2, clock=Clock())
    taken = []
    for i in range(3):
        slot.offer(jpeg(bytes([i])), captured_at=0.0)
        taken.append(slot.take())
    assert [t[0] for t in taken[:2]] == [1, 2]
    assert taken[2] is None, "a third frame went out with two unacked"


def test_a_slow_viewer_gets_the_newest_frame_not_a_backlog():
    slot = screen_stream.FrameSlot(window=1, clock=Clock())
    slot.offer(jpeg(b"\x01"), captured_at=0.0)
    seq, _, _ = slot.take()
    for i in range(2, 6):
        slot.offer(jpeg(bytes([i])), captured_at=0.0)
    slot.ack(seq)
    seq2, data, _ = slot.take()
    assert data == jpeg(b"\x05")
    assert slot.dropped == 3
    assert slot.take() is None


def test_an_ack_frees_the_window():
    slot = screen_stream.FrameSlot(window=1, clock=Clock())
    slot.offer(jpeg(b"\x01"), captured_at=0.0)
    seq, _, _ = slot.take()
    slot.offer(jpeg(b"\x02"), captured_at=0.0)
    assert slot.take() is None
    slot.ack(seq)
    assert slot.take()[1] == jpeg(b"\x02")


def test_an_identical_frame_is_not_sent_twice():
    """mpdecimate lets a keepalive frame through every few seconds; when the
    screen has not changed its bytes are the same, and resending 30 KB of
    nothing is the idle bandwidth this exists to avoid."""
    slot = screen_stream.FrameSlot(window=2, clock=Clock())
    slot.offer(jpeg(b"\x07"), captured_at=0.0)
    seq, _, _ = slot.take()
    slot.ack(seq)
    slot.offer(jpeg(b"\x07"), captured_at=1.0)
    assert slot.take() is None


def test_a_viewer_that_never_acks_is_not_frozen_forever():
    clock = Clock()
    slot = screen_stream.FrameSlot(window=1, ack_timeout=5.0, clock=clock)
    slot.offer(jpeg(b"\x01"), captured_at=0.0)
    slot.take()
    slot.offer(jpeg(b"\x02"), captured_at=0.0)
    assert slot.take() is None
    clock.now += 5.1
    assert slot.take()[1] == jpeg(b"\x02")


# ── the socket ──────────────────────────────────────────────────────────────


class FakeHub:
    """The feed, without ffmpeg: frames are pushed by the test."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.joined = []
        self.left = []

    def join(self, desk, viewer):
        self.joined.append(desk)
        self.viewer = viewer
        for f in self.frames:
            viewer.push(f, captured_at=time.time())

    def leave(self, desk, viewer):
        self.left.append(desk)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def client(bus, monkeypatch):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=bus / "agent_prefs.json", asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json", routines_path=bus / "routines.json",
    )
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


URL = "/v1/agents/acme/screen/stream"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _refused(client, url=URL, headers=None):
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.receive_text()
    return caught.value.code


def test_no_token_no_socket(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub([jpeg()]))
    assert _refused(client) == 4401


def test_a_wrong_token_no_socket(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub([jpeg()]))
    assert _refused(client, headers={"Authorization": "Bearer nope"}) == 4401


def test_no_token_configured_is_closed_not_open(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub([jpeg()]))
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    assert _refused(client, headers=AUTH) == 4503


def test_an_unknown_desk_is_refused_by_name(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub([jpeg()]))
    assert _refused(client, url="/v1/agents/ghost/screen/stream",
                    headers=AUTH) == 4404


def unpack(blob):
    seq, age_ms = struct.unpack(">II", blob[:8])
    return seq, age_ms, blob[8:]


def test_hello_then_frames_with_a_sequence_and_an_age(client, monkeypatch):
    hub = FakeHub([jpeg(b"\x01")])
    monkeypatch.setattr(screen_stream, "HUB", hub)
    with client.websocket_connect(URL, headers=AUTH) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert (hello["width"], hello["height"]) == sandbox.SIZE
        assert hello["window"] == screen_stream.WINDOW
        seq, age_ms, data = unpack(ws.receive_bytes())
        assert seq == 1 and data == jpeg(b"\x01")
        assert age_ms < 5000
    assert hub.joined == ["acme"] and hub.left == ["acme"]


def test_frames_wait_for_acks_over_the_wire(client, monkeypatch):
    frames = [jpeg(bytes([i])) for i in range(1, 6)]
    hub = FakeHub(frames[:1])
    monkeypatch.setattr(screen_stream, "HUB", hub)
    monkeypatch.setattr(screen_stream, "WINDOW", 1)
    monkeypatch.setattr(screen_stream, "TICK_SECONDS", 0.2)
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        seq, _, first = unpack(ws.receive_bytes())
        assert first == frames[0]
        for f in frames[1:]:
            hub.viewer.push(f, captured_at=time.time())
        # window full: nothing but a tick until the ack
        assert ws.receive_json()["type"] == "tick"
        ws.send_json({"type": "ack", "seq": seq})
        message = ws.receive()
        while "bytes" not in message or message["bytes"] is None:
            message = ws.receive()
        seq, _, newest = unpack(message["bytes"])
        assert newest == frames[-1], "the backlog went out instead of the newest"


def test_input_goes_over_the_same_socket(client, monkeypatch):
    sent = []
    monkeypatch.setattr(screen_stream, "HUB", FakeHub())
    monkeypatch.setattr(sandbox, "send_input",
                        lambda desk, action: sent.append((desk, action)))
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        ws.send_json({"type": "input", "id": 7, "action": "click", "x": 3, "y": 4})
        reply = ws.receive_json()
    assert reply == {"type": "input", "id": 7, "ok": True}
    assert sent == [("acme", {"action": "click", "x": 3, "y": 4})]


def test_bad_input_is_answered_not_fatal(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub())

    def refuse(desk, action):
        raise ValueError("click (99999,1) is outside the 1280x800 screen")

    monkeypatch.setattr(sandbox, "send_input", refuse)
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        ws.send_json({"type": "input", "id": 1, "action": "click",
                      "x": 99999, "y": 1})
        reply = ws.receive_json()
        assert reply["ok"] is False and reply["reason"] == "bad_input"
        ws.send_json({"type": "input", "id": 2, "action": "key", "key": "a"})
        assert ws.receive_json()["id"] == 2


def test_a_stopped_computer_wakes_and_tells_the_client_to_poll(
        client, monkeypatch):
    """The fallback: a client that is told `computer_not_running` goes back to
    polling `GET .../screen`, which says `waking` until it is up."""
    woken = []
    monkeypatch.setattr(screen_stream, "HUB", FakeHub())
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    monkeypatch.setattr(browser_reaper, "wake",
                        lambda desk: woken.append(desk) or True)
    with client.websocket_connect(URL, headers=AUTH) as ws:
        said = ws.receive_json()
        assert said == {"type": "error", "reason": "computer_not_running",
                        "fallback": "poll"}
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
    assert caught.value.code == 4409
    assert woken == ["acme"]


def test_a_dead_feed_tells_the_client_to_poll(client, monkeypatch):
    class DeadHub(FakeHub):
        def join(self, desk, viewer):
            viewer.fail("no_frame", "ffmpeg exited")

    monkeypatch.setattr(screen_stream, "HUB", DeadHub())
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()  # hello
        said = ws.receive_json()
        assert said["type"] == "error" and said["fallback"] == "poll"
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
    assert caught.value.code == 1011


def test_watching_the_stream_marks_the_desk_as_used(client, monkeypatch):
    monkeypatch.setattr(screen_stream, "HUB", FakeHub([jpeg()]))
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        ws.receive_bytes()
    assert "acme" in browser_reaper.last_uses(["acme"])


def test_the_status_route_advertises_the_stream(client):
    body = client.get("/v1/agents/acme/screen", headers=AUTH).json()
    assert body["stream_url"] == "/v1/agents/acme/screen/stream"
    # the poll stays: it is the fallback
    assert body["frame_url"] == "/v1/agents/acme/screen.jpg"


# ── the hub: one feed per desk, shared, stopped when nobody watches ─────────


class FakeProc:
    """`docker exec ... ffmpeg`, as far as the hub can tell: a pipe."""

    def __init__(self):
        r, w = __import__("os").pipe()
        self.stdout = open(r, "rb", buffering=0)
        self._w = w
        self.closed = False
        self.stdin = self

    def emit(self, data):
        __import__("os").write(self._w, data)

    def die(self):
        __import__("os").close(self._w)

    def close(self):          # stdin.close(): the container-side kill
        if not self.closed:
            self.closed = True
            try:
                self.die()
            except OSError:
                pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.close()


class Collector:
    """A viewer that records instead of awaiting."""

    def __init__(self):
        self.frames = []
        self.failed = None
        self.event = __import__("threading").Event()

    def push(self, data, *, captured_at):
        self.frames.append(data)
        self.event.set()

    def fail(self, reason, detail=""):
        self.failed = reason
        self.event.set()


def _wait(pred, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_two_viewers_of_one_desk_share_one_grab():
    procs = []
    hub = screen_stream.StreamHub(spawn=lambda desk: procs.append(FakeProc())
                                  or procs[-1], linger=0.05)
    a, b = Collector(), Collector()
    hub.join("acme", a)
    hub.join("acme", b)
    assert len(procs) == 1
    procs[0].emit(jpeg(b"\x01")[:5])
    procs[0].emit(jpeg(b"\x01")[5:])
    assert _wait(lambda: a.frames and b.frames)
    assert a.frames == b.frames == [jpeg(b"\x01")]
    hub.leave("acme", a)
    hub.leave("acme", b)


def test_a_late_viewer_gets_the_current_picture_at_once():
    """mpdecimate sends nothing until the screen changes, so a viewer who
    joins a still screen would otherwise wait forever for a first frame."""
    proc = FakeProc()
    hub = screen_stream.StreamHub(spawn=lambda desk: proc, linger=0.05)
    first = Collector()
    hub.join("acme", first)
    proc.emit(jpeg(b"\x09"))
    assert _wait(lambda: first.frames)
    late = Collector()
    hub.join("acme", late)
    assert late.frames == [jpeg(b"\x09")]
    hub.leave("acme", first)
    hub.leave("acme", late)


def test_nobody_watching_stops_the_grab_after_the_linger():
    proc = FakeProc()
    hub = screen_stream.StreamHub(spawn=lambda desk: proc, linger=0.05)
    viewer = Collector()
    hub.join("acme", viewer)
    hub.leave("acme", viewer)
    assert _wait(lambda: proc.closed), "ffmpeg kept running with no viewer"
    assert "acme" not in hub.feeds


def test_coming_back_inside_the_linger_keeps_the_same_grab():
    procs = []
    hub = screen_stream.StreamHub(spawn=lambda desk: procs.append(FakeProc())
                                  or procs[-1], linger=0.3)
    viewer = Collector()
    hub.join("acme", viewer)
    hub.leave("acme", viewer)
    hub.join("acme", viewer)
    time.sleep(0.4)
    assert len(procs) == 1 and not procs[0].closed
    hub.leave("acme", viewer)


def test_a_grab_that_dies_tells_its_viewers_to_fall_back():
    proc = FakeProc()
    hub = screen_stream.StreamHub(spawn=lambda desk: proc, linger=0.05)
    viewer = Collector()
    hub.join("acme", viewer)
    proc.die()
    assert _wait(lambda: viewer.failed == "stream_ended")
    assert "acme" not in hub.feeds


def test_the_installer_gives_uvicorn_a_websocket_implementation():
    """Without `websockets` (or wsproto) in the venv uvicorn refuses every
    upgrade, and the stream silently never happens -- the box's venv had
    neither on 2026-10-01."""
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "deploy" /
              "install-deck.sh").read_text()
    assert __import__("re").search(r"pip install[^\n]*\bwebsockets==\d", script)
