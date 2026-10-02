"""His own Mac's terminal, relayed by the deck (server/mac_terminal.py).

What these pin:

* **The gate.** Only his bearer opens a viewer, and only while his Mac is in
  Full access and online. Ask mode, Paused, asleep -> refused with the one
  sentence he can act on, before anything is offered to the Mac.
* **The Mac's end** needs its node secret on top of the bearer, and the
  session id the poll handed it; a guess is refused.
* **The framing.** Bytes cross verbatim both ways; text crosses only as the
  desks' terminal wire allows (`resize`/`ack` up, `hello`/`exit`/`error`
  down) -- anything else is dropped.
* **It ends** when the viewer leaves (the Mac's socket closes), when Full
  access goes, and after the idle timeout.
* Agents get nothing new: the `mac` MCP tool list has no terminal.
"""

from __future__ import annotations

import json
import signal
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import mac_api, mac_mcp, mac_nodes, mac_terminal

TOKEN = "test-token-not-a-real-one"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
COPY = ("Turn on Full access on your Mac (Shaliach → Settings → Mac) to use "
        "its terminal.")


class Rig:
    def __init__(self, tmp_path, relay) -> None:
        self.store = mac_nodes.Store(tmp_path / "mac")
        self.relay = relay
        app = FastAPI()
        app.include_router(mac_api.build_router(
            self.store, tell=lambda *a: None, owner_line=lambda *a: None,
            handoffs_path=tmp_path / "handoffs.json", terminals=relay))
        app.include_router(mac_terminal.build_router(self.store, relay))
        self.app = app

    def mac(self, client, mode="full", machine="m-1"):
        body = client.post("/v1/nodes", headers=BEARER, json={
            "machine_id": machine, "name": "Sam's MacBook Pro",
            "os": "macOS 26.0", "app_version": "1.5.0",
            "capabilities": ["run"], "mode": mode}).json()
        node = body["node_id"]
        headers = {**BEARER, "X-Deck-Node": f"{node}.{body['node_secret']}"}
        self.poll(client, node, headers, mode=mode)
        return node, headers

    def poll(self, client, node, headers, mode="full"):
        return client.post(f"/v1/nodes/{node}/poll", headers=headers, json={
            "wait": 0, "free_slots": 1, "running": [], "mode": mode,
            "grants": {}}).json()

    def offer(self, client, node, headers, mode="full"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            got = self.poll(client, node, headers, mode).get("terminal")
            if got:
                return got
            time.sleep(0.02)
        raise AssertionError("the poll never offered the terminal")


@pytest.fixture(autouse=True)
def no_hang():
    """A pipe that never ends must FAIL the test, not hang the suite."""
    def boom(*_):
        raise AssertionError("timed out: a socket was left open")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)

    def build(**kw):
        return Rig(tmp_path, mac_terminal.Relay(**kw))
    return build


def _closed(ws) -> int:
    """The close code, skipping whatever was said before it."""
    for _ in range(50):
        message = ws.receive()
        if message["type"] == "websocket.close":
            return message.get("code", 1000)
    raise AssertionError("the socket never closed")


# ── the gate ────────────────────────────────────────────────────────────────


def test_the_gate_is_full_access_on_a_mac_that_is_online():
    now = time.time()
    node = {"name": "Mac", "mode": "full", "last_seen": now}
    assert mac_terminal.refusal(node, now) is None
    assert mac_terminal.refusal({**node, "mode": "ask"}, now) == (
        "full_access_required", COPY)
    reason, why = mac_terminal.refusal({**node, "mode": "paused"}, now)
    assert reason == "mac_paused" and COPY in why
    assert mac_terminal.refusal({**node, "last_seen": now - 600}, now)[0] == "mac_asleep"
    assert mac_terminal.refusal(None, now)[0] == "unknown_node"


@pytest.mark.parametrize("mode", ["ask", "paused"])
def test_a_viewer_is_refused_with_the_copy_and_nothing_is_offered(make, mode):
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client, mode=mode)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER) as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            assert COPY in msg["detail"]
            assert _closed(ws) == 4409
        assert "terminal" not in rig.poll(client, node, headers, mode)


def test_no_bearer_no_viewer_and_no_node_secret_no_mac_end(make):
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream"):
                pass
        assert exc.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session=x", headers=BEARER):
                pass
        assert exc.value.code == 4401
        _, other = rig.mac(client, machine="m-2")   # another Mac's own secret
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session=x", headers=other):
                pass
        assert exc.value.code == 4401


def test_a_guessed_session_is_refused(make):
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER):
            rig.offer(client, node, headers)
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session=guess",
                    headers=headers) as mac:
                assert not rig.relay.is_open(node)
                assert json.loads(mac.receive_text())["reason"] == "no_session"


# ── allowed: the pipe and its framing ───────────────────────────────────────


def test_full_access_pipes_bytes_both_ways_and_only_the_wire_s_text(make):
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with client.websocket_connect(
                f"/v1/nodes/{node}/terminal/stream?cols=100&rows=30&from=iphone",
                headers=BEARER) as viewer:
            offer = rig.offer(client, node, headers)
            assert (offer["cols"], offer["rows"], offer["from"]) == (100, 30, "iPhone")
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session={offer['session']}",
                    headers=headers) as mac:
                mac.send_text(json.dumps({"type": "hello", "window": "deck",
                                          "windows": ["deck"]}))
                mac.send_text(json.dumps({"type": "keylog", "x": 1}))  # dropped
                mac.send_bytes(b"hello\r\n$ ")
                assert json.loads(viewer.receive_text())["type"] == "hello"
                assert viewer.receive_bytes() == b"hello\r\n$ "
                viewer.send_bytes(b"echo hi\r")
                viewer.send_text(json.dumps({"type": "run", "cmd": "x"}))  # dropped
                viewer.send_text(json.dumps({"type": "resize", "cols": 90, "rows": 20}))
                assert mac.receive_bytes() == b"echo hi\r"
                assert json.loads(mac.receive_text()) == {
                    "type": "resize", "cols": 90, "rows": 20}
                assert rig.relay.is_open(node)
                assert "terminal" not in rig.poll(client, node, headers)
                mac.send_text(json.dumps({"type": "exit", "code": 0}))
                assert json.loads(viewer.receive_text()) == {"type": "exit", "code": 0}


def test_the_viewer_leaving_closes_the_mac_end(make):
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER) as viewer:
            offer = rig.offer(client, node, headers)
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session={offer['session']}",
                    headers=headers) as mac:
                viewer.close(1000)   # he left the tab
                assert _closed(mac) == 1000
        assert not rig.relay.is_open(node)


def test_it_closes_after_the_idle_timeout(make):
    rig = make(idle=0.3, tick=0.05)
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER) as viewer:
            offer = rig.offer(client, node, headers)
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session={offer['session']}",
                    headers=headers) as mac:
                msg = json.loads(viewer.receive_text())
                assert msg["reason"] == "idle"
                assert _closed(mac) == 1000


def test_full_access_going_away_closes_a_live_session(make):
    rig = make(tick=0.05)
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER) as viewer:
            offer = rig.offer(client, node, headers)
            with client.websocket_connect(
                    f"/v1/nodes/{node}/terminal/mac?session={offer['session']}",
                    headers=headers) as mac:
                rig.poll(client, node, headers, mode="ask")
                msg = json.loads(viewer.receive_text())
                assert msg["reason"] == "full_access_required"
                assert _closed(mac) == 1000


def test_a_mac_that_never_dials_in_is_reported(make):
    rig = make(join_wait=0.2)
    with TestClient(rig.app) as client:
        node, _ = rig.mac(client)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER) as viewer:
            assert json.loads(viewer.receive_text())["reason"] == "mac_no_answer"


def test_agents_get_no_terminal_tool():
    assert not [t["name"] for t in mac_mcp.TOOLS if "terminal" in t["name"]]


def test_a_waiting_long_poll_wakes_with_the_offer(make):
    """The Mac learns of the viewer within a beat, not at the poll's end."""
    import threading
    rig = make()
    with TestClient(rig.app) as client:
        node, headers = rig.mac(client)
        got = {}

        def long_poll():
            began = time.monotonic()
            got["body"] = client.post(f"/v1/nodes/{node}/poll", headers=headers, json={
                "wait": 8, "free_slots": 1, "running": [], "mode": "full",
                "grants": {}}).json()
            got["took"] = time.monotonic() - began

        t = threading.Thread(target=long_poll)
        t.start()
        time.sleep(0.3)
        with client.websocket_connect(f"/v1/nodes/{node}/terminal/stream",
                                      headers=BEARER):
            t.join(6)
        assert got["body"].get("terminal"), got
        assert got["took"] < 3
