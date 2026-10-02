"""The desk's terminal as a real terminal: a PTY on a WebSocket.

`POST .../terminal` runs one line with a 20 s cap, so vim, top, ssh and
anything that asks a question could not run. This socket is bash in a tmux
session inside the desk's container, attached through `docker exec -it` on a
PTY the deck owns: bytes in, bytes out, resize, and backpressure so a client on
a slow link stops the reading rather than the deck buffering without end.

The PTY code below is REAL -- a local process in a real pseudo-terminal stands
in for `docker exec`, so echo, the window size and SIGWINCH are the kernel's,
not a fake's. Nothing here starts Docker.
"""

import asyncio
import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import api as api_mod
from server import browser_reaper, office, sandbox, terminal_stream
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}
URL = "/v1/agents/acme/terminal/stream"


# ── the argv: inside the container, on a tty, in tmux ───────────────────────


def test_the_deck_window_is_bash_in_a_persistent_tmux_session():
    argv = terminal_stream.attach_argv("acme", "deck")
    assert argv[:2] == ["docker", "exec"]
    assert "--interactive" in argv and "--tty" in argv
    assert sandbox.container_name("acme") in argv
    assert f"{sandbox.DESK_UID}:{sandbox.DESK_GID}" in argv
    line = argv[-1]
    assert "new-session -A -s deck" in line  # attach if there, else create


def test_the_agent_window_is_read_only_at_the_tmux_level():
    """`-f read-only`, not `-r`: MEASURED on tmux 3.3a, `-r` also sets
    ignore-size, and the agent window stayed 80x24 inside a 100x30 view."""
    line = terminal_stream.attach_argv("acme", "agent")[-1]
    assert "attach-session -f read-only -t agent" in line
    assert ".deck/agent.log" in line


def test_an_unknown_window_is_refused_not_defaulted():
    with pytest.raises(ValueError):
        terminal_stream.attach_argv("acme", "root")


def test_size_is_checked_not_clamped():
    assert terminal_stream.check_size(80, 24) == (80, 24)
    for cols, rows in ((0, 24), (80, 0), (501, 24), (80, 201), ("80", 24)):
        with pytest.raises(ValueError):
            terminal_stream.check_size(cols, rows)


# ── the socket, over a fake WebSocket, on a real PTY ────────────────────────


class FakeWS:
    """What `serve` uses of Starlette's WebSocket."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.texts: list[dict] = []
        self.out = bytearray()
        self.closed: int | None = None
        self.changed = asyncio.Event()

    async def send_text(self, text):
        self.texts.append(json.loads(text))
        self.changed.set()

    async def send_bytes(self, data):
        self.out += data
        self.changed.set()

    async def receive(self):
        return await self.inbox.get()

    async def close(self, code=1000):
        self.closed = code
        self.changed.set()

    # test side
    def type(self, data: bytes):
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": data})

    def say(self, body: dict):
        self.inbox.put_nowait({"type": "websocket.receive",
                               "text": json.dumps(body)})

    def hang_up(self):
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    async def until(self, pred, seconds=5.0):
        deadline = asyncio.get_running_loop().time() + seconds
        while not pred():
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), min(left, 0.1))
            except asyncio.TimeoutError:
                pass
        return True


def local(command: str):
    """A spawn that runs `command` in a real PTY instead of `docker exec`."""
    def spawn(argv, cols, rows):
        return terminal_stream.spawn_pty(["/bin/sh", "-c", command], cols, rows)
    return spawn


@pytest.fixture(autouse=True)
def no_reaper(monkeypatch):
    monkeypatch.setattr(browser_reaper, "touch", lambda desk, **kw: None)
    # `cat` and `sleep` do not know tmux's detach key; do not wait on them.
    monkeypatch.setattr(terminal_stream, "DETACH_WAIT", 0.1)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_hello_then_typed_bytes_come_back_as_output():
    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24,
            spawn=local("stty -echo; cat")))
        assert await ws.until(lambda: ws.texts)
        hello = ws.texts[0]
        assert hello["type"] == "hello" and hello["window"] == "deck"
        assert hello["read_only"] is False
        assert hello["windows"] == ["deck", "agent"]
        assert (hello["cols"], hello["rows"]) == (80, 24)
        ws.type(b"ping-from-the-keyboard\n")
        assert await ws.until(lambda: b"ping-from-the-keyboard" in ws.out)
        ws.hang_up()
        await task
    run(go())


def test_the_pty_starts_at_the_size_the_client_asked_for():
    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=91, rows=33,
            spawn=local("stty size; sleep 5")))
        assert await ws.until(lambda: b"33 91" in ws.out)
        ws.hang_up()
        await task
    run(go())


def test_a_resize_reaches_the_program_as_sigwinch():
    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24,
            spawn=local("trap 'stty size' WINCH; echo ready; "
                        "while :; do sleep 0.05; done")))
        assert await ws.until(lambda: b"ready" in ws.out)
        ws.say({"type": "resize", "cols": 120, "rows": 40})
        assert await ws.until(lambda: b"40 120" in ws.out)
        ws.hang_up()
        await task
    run(go())


def test_a_bad_resize_is_ignored_not_fatal():
    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24,
            spawn=local("stty -echo; cat")))
        assert await ws.until(lambda: ws.texts)
        ws.say({"type": "resize", "cols": 99999, "rows": -1})
        ws.say({"type": "nonsense"})
        ws.type(b"still-alive\n")
        assert await ws.until(lambda: b"still-alive" in ws.out)
        ws.hang_up()
        await task
    run(go())


def test_output_stops_at_the_window_until_the_client_acks(monkeypatch):
    monkeypatch.setattr(terminal_stream, "READ_WINDOW", 4096)
    monkeypatch.setattr(terminal_stream, "CHUNK", 1024)

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24,
            spawn=local("head -c 200000 /dev/zero | tr '\\0' x; sleep 5")))
        assert await ws.until(lambda: len(ws.out) >= 4096)
        await asyncio.sleep(0.4)
        held = len(ws.out)
        assert held < 4096 + 1024, "the deck kept reading with no ack"
        ws.say({"type": "ack", "bytes": held})
        assert await ws.until(lambda: len(ws.out) > held)
        ws.hang_up()
        await task
    run(go())


def test_the_agent_window_drops_typed_input():
    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="agent", cols=80, rows=24,
            spawn=local("stty -echo; cat")))
        assert await ws.until(lambda: ws.texts)
        assert ws.texts[0]["read_only"] is True
        ws.type(b"rm -rf everything\n")
        ws.say({"type": "resize", "cols": 81, "rows": 24})
        await asyncio.sleep(0.4)
        assert b"rm -rf" not in ws.out
        ws.hang_up()
        await task
    run(go())


def test_the_shell_exiting_says_so_and_closes():
    async def go():
        ws = FakeWS()
        await terminal_stream.serve(ws, "acme", window="deck", cols=80,
                                    rows=24, spawn=local("echo bye; exit 3"))
        assert b"bye" in ws.out
        assert ws.texts[-1] == {"type": "exit", "code": 3}
        assert ws.closed == 1000
    run(go())


def test_an_image_without_tmux_is_terminal_unavailable():
    """Exit 127 before any output: the container is the old image."""
    async def go():
        ws = FakeWS()
        await terminal_stream.serve(ws, "acme", window="deck", cols=80,
                                    rows=24, spawn=local("exit 127"))
        assert ws.texts[-1]["type"] == "error"
        assert ws.texts[-1]["reason"] == "terminal_unavailable"
        assert ws.closed == 1011
    run(go())


def test_hanging_up_kills_the_attach_not_the_session():
    """The attach process dies with the socket; tmux (in the container)
    keeps the shell, which is the point of tmux."""
    procs = []

    def spawn(argv, cols, rows):
        proc, fd = terminal_stream.spawn_pty(["/bin/sh", "-c", "sleep 30"],
                                             cols, rows)
        procs.append(proc)
        return proc, fd

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24, spawn=spawn))
        assert await ws.until(lambda: ws.texts)
        ws.hang_up()
        await task
    run(go())
    assert procs[0].poll() is not None


def test_hanging_up_detaches_from_tmux_first(tmp_path):
    """MEASURED on the box: killing `docker exec` leaves its tmux client
    running in the container (the shim holds the tty), and every closed tab
    left one more attached client behind. tmux's own detach key ends it."""
    got = tmp_path / "keys"

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(terminal_stream.serve(
            ws, "acme", window="deck", cols=80, rows=24,
            spawn=local(f"stty raw -echo; echo ready; "
                        f"dd bs=1 count=2 of={got} 2>/dev/null")))
        assert await ws.until(lambda: b"ready" in ws.out)
        ws.hang_up()
        await task
    run(go())
    assert got.read_bytes() == terminal_stream.DETACH


# ── the route: auth and refusals before the socket is accepted ──────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=tmp_path / "agent_prefs.json",
        asks_path=tmp_path / "asks.json",
        rules_path=tmp_path / "autoreview.json",
        routines_path=tmp_path / "routines.json",
    )
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    monkeypatch.setattr(terminal_stream, "SPAWN", local("stty -echo; cat"))
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def _refused(client, url=URL, headers=None):
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.receive_text()
    return caught.value.code


def test_no_token_no_terminal(client):
    assert _refused(client) == 4401


def test_a_wrong_token_no_terminal(client):
    assert _refused(client, headers={"Authorization": "Bearer nope"}) == 4401


def test_no_token_configured_is_closed_not_open(client, monkeypatch):
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    assert _refused(client, headers=AUTH) == 4503


def test_an_unknown_desk_is_refused_by_name(client):
    assert _refused(client, url="/v1/agents/ghost/terminal/stream",
                    headers=AUTH) == 4404


def test_an_unknown_window_or_size_is_a_4400(client):
    assert _refused(client, url=URL + "?window=root", headers=AUTH) == 4400
    assert _refused(client, url=URL + "?cols=0", headers=AUTH) == 4400


def test_over_the_route_keys_go_in_and_output_comes_out(client):
    with client.websocket_connect(URL + "?cols=100&rows=30",
                                  headers=AUTH) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["desk"] == "acme"
        assert (hello["cols"], hello["rows"]) == (100, 30)
        ws.send_bytes(b"over-the-wire\n")
        seen = b""
        while b"over-the-wire" not in seen:
            seen += ws.receive_bytes()


def test_a_stopped_computer_wakes_and_says_so(client, monkeypatch):
    woken = []
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    monkeypatch.setattr(browser_reaper, "wake",
                        lambda desk: woken.append(desk) or True)
    with client.websocket_connect(URL, headers=AUTH) as ws:
        said = ws.receive_json()
        assert said["type"] == "error"
        assert said["reason"] == "computer_not_running"
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
    assert caught.value.code == 4409
    assert woken == ["acme"]


def test_the_status_route_advertises_the_terminal_stream(client):
    body = client.get("/v1/agents/acme/screen", headers=AUTH).json()
    assert body["terminal_url"] == "/v1/agents/acme/terminal/stream"


@pytest.mark.skipif(sys.platform == "win32", reason="no PTY")
def test_the_one_shot_route_is_still_there(client, monkeypatch):
    monkeypatch.setattr(sandbox, "run_command", lambda desk, command, cwd=None:
                        {"exit": 0, "stdout": "ok\n", "stderr": "",
                         "truncated": False})
    reply = client.post("/v1/agents/acme/terminal", headers=AUTH,
                        json={"command": "echo ok"})
    assert reply.status_code == 200 and reply.json()["stdout"] == "ok\n"
