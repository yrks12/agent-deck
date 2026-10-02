"""The desk tools that reach the owner's Mac (MB3): MCP server `mac`.

The defects this closes, before any of it exists:

* A desk had no way to run anything on the owner's Mac. The store and the
  node routes (M1) queue jobs and carry results, but nothing a desk can call
  ever enqueues one.
* A wait on a Mac that is not there is a hang: a desk whose tool call sits on
  an asleep laptop is a desk that stops answering. Offline must come back as
  one owner card and a sentence, in seconds.
* MEASURED on the box (claude 2.1.286, docs/mac-bridge.md "Measured"): Esc
  sends `notifications/cancelled` for the call's request id within 0.1 s, and
  `claude stop` SIGTERMs the server with no cancel and no EOF first. A server
  that reads stdin only between calls never sees the cancel, and one that dies
  on SIGTERM leaves the Mac running a command nobody is waiting for.

Hermetic: tmp store, tmp handoffs, a fake Mac thread driving
`mac_nodes.Store` exactly as `mac_api` does. No network, no CLI.
"""

import json
from pathlib import Path
import os
import threading
import time

import pytest

from server import handoff, mac_nodes, spawn
from server.roster import Desk

try:
    from server import mac_mcp
except ImportError:  # RED: the module does not exist yet
    mac_mcp = None

MAC = "Test Mac"

MB3_TOOLS = {"run", "job", "cancel", "read", "write", "list", "open",
             "screenshot", "status",
             # Mac control (tests/test_mac_control_mcp.py)
             "click", "move", "drag", "scroll", "type_text", "key",
             # more than one display (tests/test_mac_multidisplay_mcp.py)
             "list_displays"}
MB3_REASONS = {
    "no_mac", "mac_offline", "mac_paused", "ambiguous_mac", "unknown_mac",
    "mac_not_answering", "awaiting_grant", "denied", "out_of_scope",
    "blocked_path", "capability_off", "tcc_denied", "needs_admin", "bad_input",
    "bad_path", "no_such_path", "not_a_directory", "not_a_file", "not_text",
    "too_large", "timed_out", "cancelled", "lost", "node_busy", "rate_limited",
    "queue_failed",
    # Mac control
    "control_off", "owner_active", "secure_field", "needs_screenshot",
    # more than one display
    "no_such_display"}


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class Rig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        assert mac_mcp is not None, "server/mac_mcp.py does not exist"
        self.clock = Clock()
        self.store = mac_nodes.Store(tmp_path / "mac", clock=self.clock)
        self.handoffs = tmp_path / "handoffs.json"
        monkeypatch.setattr(mac_mcp, "STORE", self.store)
        monkeypatch.setattr(mac_mcp, "HANDOFFS_PATH", self.handoffs)
        monkeypatch.setattr(mac_mcp, "POLL_EVERY", 0.02)
        self.node_id = None
        self.behave = None
        self._stop = threading.Event()
        self._thread = None

    def pair(self, name=MAC, machine="m-1", caps=None):
        row = self.store.register(machine, name, "macOS 26.0", "1.5.0",
                                  caps or list(mac_nodes.CAPABILITIES), "ask",
                                  None)
        self.node_id = self.node_id or row["node_id"]
        return row["node_id"]

    def poll(self, node_id=None, mode="ask", grants=None, free=4):
        return self.store.poll(node_id or self.node_id, free, [], mode,
                               grants or {})

    def events(self, job_id, *events):
        out = [{"seq": i, **e} for i, e in enumerate(events, start=1)]
        return self.store.record_events(self.node_id, job_id, out)

    def mac(self, behave):
        """A fake Mac: polls every 20 ms and hands each job to `behave`."""
        self.behave = behave
        self.poll()   # online before the first call, not whenever the thread runs

        def loop():
            while not self._stop.is_set():
                jobs, _ = self.poll()
                for job in jobs:
                    threading.Thread(target=behave, args=(job,),
                                     daemon=True).start()
                time.sleep(0.02)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.stop()


def call(desk, name, **args):
    """One tools/call through the JSON-RPC handler: (json body, isError, reply)."""
    reply = mac_mcp.Server(desk).handle({"jsonrpc": "2.0", "id": 7,
                                         "method": "tools/call",
                                         "params": {"name": name,
                                                    "arguments": args}})
    result = reply["result"]
    text = next(c["text"] for c in result["content"] if c["type"] == "text")
    return json.loads(text), result["isError"], reply


# ── S-10 / S-11: schema and wiring ───────────────────────────────────────────


def test_the_tools_are_mb3s_and_none_takes_a_desk():
    assert mac_mcp is not None, "server/mac_mcp.py does not exist"
    names = {t["name"] for t in mac_mcp.TOOLS}
    assert names == MB3_TOOLS
    for tool in mac_mcp.TOOLS:
        props = tool["inputSchema"].get("properties") or {}
        assert "desk" not in props, f"{tool['name']} takes a desk field"
        assert tool["inputSchema"].get("additionalProperties") is False


def test_the_refusal_set_is_exactly_mb3s():
    assert mac_mcp is not None, "server/mac_mcp.py does not exist"
    assert set(mac_mcp.REFUSALS) == MB3_REASONS


def test_the_spawn_argv_carries_mac_beside_deck_with_the_desk_bound():
    argv = spawn.build_argv(Desk(name="atlas", cwd="/tmp", engine="claude",
                                 mission="m"), background=True, seed="hi")
    (config,) = [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-config"]
    servers = json.loads(Path(config).read_text())["mcpServers"]
    assert {"mac", "deck"} <= set(servers)
    mac = servers["mac"]
    assert mac["type"] == "stdio"
    assert mac["args"] == ["-m", "server.mac_mcp", "--desk", "atlas"]
    # The Mac tools may wait behind ToolSearch; only `deck` is always loaded.
    assert not mac.get("alwaysLoad")


def test_initialize_list_and_unknown_methods_answer_and_never_crash():
    assert mac_mcp is not None, "server/mac_mcp.py does not exist"
    s = mac_mcp.Server("atlas")
    init = s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25"}})
    assert init["result"]["serverInfo"]["name"] == "mac"
    assert init["result"]["protocolVersion"] == "2025-11-25"
    listed = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == MB3_TOOLS
    # MEASURED: claude 2.1.286 opens with `server/discover` (an id'd request).
    other = s.handle({"jsonrpc": "2.0", "id": "d-1", "method": "server/discover"})
    assert other["id"] == "d-1" and "error" in other
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) \
        is None


# ── S-1: the round trip ──────────────────────────────────────────────────────


def test_run_round_trips_through_the_store_to_a_fake_mac(rig):
    rig.pair()

    def behave(job):
        rig.events(job["id"],
                   {"type": "started", "data": {"pid": 1, "sandboxed": True}},
                   {"type": "stdout", "data": "ProductVersion:\t26.0\n"},
                   {"type": "result", "data": {"state": "done", "exit": 0,
                                               "duration_ms": 40}})

    rig.mac(behave)
    body, is_error, _ = call("atlas", "run", command="sw_vers")
    assert not is_error, body
    assert body["ok"] and body["exit"] == 0 and body["mac"] == MAC
    assert body["stdout"] == "ProductVersion:\t26.0\n" and body["stderr"] == ""
    assert body["sandboxed"] is True and body["duration_ms"] == 40
    assert body["truncated"] is False and body["job_id"].startswith("mj_")
    assert rig.store.job(body["job_id"])["desk"] == "atlas"


def test_a_non_zero_exit_is_an_answer_not_a_refusal(rig):
    rig.pair()
    rig.mac(lambda job: rig.events(
        job["id"], {"type": "started", "data": {}},
        {"type": "stderr", "data": "nope\n"},
        {"type": "result", "data": {"state": "done", "exit": 3}}))
    body, is_error, _ = call("atlas", "run", command="false")
    assert body["ok"] and body["exit"] == 3 and body["stderr"] == "nope\n"


def test_a_refusal_from_the_mac_carries_its_reason_and_detail(rig):
    rig.pair()
    rig.mac(lambda job: rig.events(
        job["id"], {"type": "result", "data": {
            "state": "refused", "reason": "blocked_path",
            "detail": "~/.ssh is on the Never touch list"}}))
    body, is_error, _ = call("atlas", "read", path="~/.ssh/config")
    assert is_error and body == {"ok": False, "reason": "blocked_path",
                                 "detail": "~/.ssh is on the Never touch list"}


# ── S-7: what a desk gets back is capped ─────────────────────────────────────


def test_a_huge_stdout_comes_back_capped_with_the_cut_mark(rig):
    rig.pair()
    chunk = "x" * mac_nodes.CHUNK_MAX

    def behave(job):
        events = [{"type": "started", "data": {}}]
        events += [{"type": "stdout", "data": chunk}] * 3
        events += [{"type": "result", "data": {"state": "done", "exit": 0}}]
        rig.events(job["id"], *events)

    rig.mac(behave)
    body, _, _ = call("atlas", "run", command="yes | head -c 200000")
    assert body["ok"] and body["truncated"] is True
    assert len(body["stdout"]) <= mac_mcp.RETURN_MAX
    assert mac_mcp.CUT_MARK in body["stdout"]


# ── S-2 / S-3: offline never hangs, and files ONE card ───────────────────────


def test_an_offline_mac_answers_in_two_seconds_with_one_card(rig):
    rig.pair()
    rig.poll()
    rig.clock.offset += mac_nodes.ONLINE_WINDOW + 1
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="sw_vers")
    assert time.monotonic() - t0 <= 2.0
    assert is_error and body["reason"] == "mac_offline"
    cards = handoff.load(rig.handoffs)
    assert len(cards) == 1
    card = cards[0]
    assert card.agent == "atlas" and card.kind == "other"
    assert card.needs == f"Open Shaliach on {MAC} so atlas can use it"
    assert card.state == "Nothing ran on the Mac. atlas wanted to: sw_vers"
    assert card.where == MAC
    assert card.id in body["detail"] and "Do not retry" in body["detail"]
    assert rig.store.offline_card(rig.node_id, "atlas") == card.id

    again, _, _ = call("atlas", "list", path="~")
    assert again["reason"] == "mac_offline" and card.id in again["detail"]
    assert len(handoff.load(rig.handoffs)) == 1, "a second card for one outage"

    # A different desk gets its own card.
    call("scout", "run", command="ls")
    assert len(handoff.load(rig.handoffs)) == 2


def test_the_mac_coming_back_closes_the_card_and_tells_the_desk_once(
        rig, monkeypatch):
    """S-2's second half, end to end through the real router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import mac_api

    told = []
    app = FastAPI()
    app.include_router(mac_api.build_router(
        rig.store, tell=lambda desk, text: told.append((desk, text)),
        owner_line=lambda text: None, handoffs_path=rig.handoffs))
    client = TestClient(app)
    monkeypatch.setenv("AGENT_DECK_TOKEN", "t" * 40)
    bearer = {"Authorization": "Bearer " + "t" * 40}
    body = client.post("/v1/nodes", headers=bearer, json={
        "machine_id": "m-9", "name": MAC, "os": "", "app_version": "",
        "capabilities": list(mac_nodes.CAPABILITIES), "mode": "ask"}).json()
    rig.node_id = body["node_id"]
    headers = {**bearer, "X-Deck-Node": f"{body['node_id']}.{body['node_secret']}"}
    poll = {"wait": 0, "free_slots": 4, "running": [], "mode": "ask", "grants": {}}
    client.post(f"/v1/nodes/{rig.node_id}/poll", headers=headers, json=poll)
    rig.clock.offset += mac_nodes.ONLINE_WINDOW + 1

    out, _, _ = call("atlas", "run", command="sw_vers")
    assert out["reason"] == "mac_offline" and told == []
    client.post(f"/v1/nodes/{rig.node_id}/poll", headers=headers, json=poll)
    client.post(f"/v1/nodes/{rig.node_id}/poll", headers=headers, json=poll)
    assert [h.status for h in handoff.load(rig.handoffs)] == ["done"]
    assert len(told) == 1 and told[0][0] == "atlas"


def test_a_card_he_already_closed_is_not_reused_for_the_next_outage(rig):
    rig.pair()
    rig.poll()
    rig.clock.offset += mac_nodes.ONLINE_WINDOW + 1
    first, _, _ = call("atlas", "run", command="a")
    card = handoff.load(rig.handoffs)[0]
    handoff.resolve(rig.handoffs, card.id, "skipped")
    second, _, _ = call("atlas", "run", command="b")
    cards = handoff.load(rig.handoffs)
    assert len(cards) == 2 and cards[1].id in second["detail"]


def test_a_paused_mac_answers_paused_with_one_card(rig):
    rig.pair()
    rig.poll(mode="paused")
    body, is_error, _ = call("atlas", "run", command="ls")
    assert is_error and body["reason"] == "mac_paused"
    call("atlas", "run", command="ls")
    (card,) = handoff.load(rig.handoffs)
    assert card.id in body["detail"] and card.where == MAC


def test_no_mac_files_one_card_a_day_and_names_the_direct_download(rig):
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="sw_vers")
    assert time.monotonic() - t0 <= 2.0
    assert is_error and body["reason"] == "no_mac"
    detail = body["detail"]
    assert "Settings" in detail and "Mac" in detail
    assert "App Store" in detail and "direct-download" in detail
    (card,) = handoff.load(rig.handoffs)
    assert card.needs == "Turn on Mac access in Shaliach → Settings → Mac"
    assert card.id in detail
    call("atlas", "status")
    call("atlas", "read", path="~/x")
    assert len(handoff.load(rig.handoffs)) == 1
    rig.clock.offset += mac_nodes.NO_MAC_CARD_TTL + 1
    call("atlas", "read", path="~/x")
    assert len(handoff.load(rig.handoffs)) == 2


# ── S-4: nobody picks it up / it stops reporting ─────────────────────────────


def test_a_mac_that_stops_polling_is_mac_not_answering(rig):
    rig.pair()
    rig.poll()

    def later():
        time.sleep(0.2)
        rig.clock.offset += mac_nodes.CLAIM_WAIT + 1

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="ls")
    assert time.monotonic() - t0 < 5
    assert is_error and body["reason"] == "mac_not_answering"


def test_a_claimed_job_the_mac_forgets_is_lost(rig):
    rig.pair()

    def behave(job):
        rig.events(job["id"], {"type": "started", "data": {}})
        time.sleep(0.2)
        rig.stop()      # the Mac goes quiet
        rig.clock.offset += mac_nodes.LOST_AFTER + 1

    rig.mac(behave)
    body, is_error, _ = call("atlas", "run", command="sleep 100",
                             timeout_s=600)
    assert is_error and body["reason"] == "lost"


# ── the waits are bounded ────────────────────────────────────────────────────


def test_a_run_past_its_timeout_is_cancelled_and_carries_partial_output(
        rig, monkeypatch):
    monkeypatch.setattr(mac_mcp, "RUN_GRACE", 0.3)
    rig.pair()
    cancel_seen = []

    def behave(job):
        rig.events(job["id"], {"type": "started", "data": {}},
                   {"type": "stdout", "data": "partial\n"})
        while not cancel_seen:
            if rig.store.record_events(rig.node_id, job["id"], []):
                cancel_seen.append(True)
            time.sleep(0.05)

    rig.mac(behave)
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="sleep 999", timeout_s=1)
    took = time.monotonic() - t0
    assert is_error and body["reason"] == "timed_out"
    assert body["stdout"] == "partial\n"
    assert took < 1 + 0.3 + 1.0
    time.sleep(0.2)
    assert cancel_seen, "the Mac was never told to kill it"


def test_no_call_ever_blocks_past_timeout_plus_the_hard_grace(rig, monkeypatch):
    """A Mac that claims and then keeps the job 'claimed' forever (keepalives
    only, never started) must not hold the desk past timeout_s + 90."""
    monkeypatch.setattr(mac_mcp, "HARD_GRACE", 0.5)
    rig.pair()

    def behave(job):
        for _ in range(100):
            try:
                rig.store.record_events(rig.node_id, job["id"], [])
            except mac_nodes.MacError:
                return
            time.sleep(0.05)

    rig.mac(behave)
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="x", timeout_s=1)
    assert time.monotonic() - t0 < 1 + 0.5 + 0.5
    assert is_error


def test_a_grant_nobody_answers_is_awaiting_grant_after_the_grant_wait(
        rig, monkeypatch):
    monkeypatch.setattr(mac_mcp, "GRANT_WAIT", 0.3)
    rig.pair()

    def behave(job):
        rig.events(job["id"], {"type": "awaiting_grant"})
        for _ in range(60):
            rig.store.record_events(rig.node_id, job["id"], [])
            time.sleep(0.05)

    rig.mac(behave)
    t0 = time.monotonic()
    body, is_error, _ = call("atlas", "run", command="sw_vers")
    assert time.monotonic() - t0 < 2
    assert is_error and body["reason"] == "awaiting_grant"
    assert "you will get a message when" in body["detail"]


# ── background, job, cancel ──────────────────────────────────────────────────


def test_background_returns_a_job_id_then_job_pages_output_and_cancel_kills(rig):
    rig.pair()
    go = threading.Event()
    killed = []

    def behave(job):
        rig.events(job["id"], {"type": "started", "data": {}},
                   {"type": "stdout", "data": "one\n"})
        go.wait(3)
        rig.store.record_events(rig.node_id, job["id"], [
            {"seq": 3, "type": "stdout", "data": "two\n"}])
        while not rig.store.record_events(rig.node_id, job["id"], []):
            time.sleep(0.02)
        killed.append(job["id"])
        rig.store.record_events(rig.node_id, job["id"], [
            {"seq": 4, "type": "result", "data": {"state": "cancelled",
                                                  "signal": "TERM"}}])

    rig.mac(behave)
    started, is_error, _ = call("atlas", "run", command="npm test",
                                background=True)
    assert not is_error and started["state"] == "running"
    job_id = started["job_id"]

    first, _, _ = call("atlas", "job", job_id=job_id)
    assert first["ok"] and first["state"] in ("running", "claimed")
    assert first["stdout"] == "one\n" and first["next_stdout"] == 4
    go.set()
    time.sleep(0.2)
    second, _, _ = call("atlas", "job", job_id=job_id,
                        since_stdout=first["next_stdout"])
    assert second["stdout"] == "two\n" and second["next_stdout"] == 8

    other, is_error, _ = call("scout", "job", job_id=job_id)
    assert is_error and other["reason"] == "bad_input"
    theirs, is_error, _ = call("scout", "cancel", job_id=job_id)
    assert is_error

    cancelled, is_error, _ = call("atlas", "cancel", job_id=job_id)
    assert not is_error and cancelled["ok"]
    deadline = time.monotonic() + 3
    while not killed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert killed == [job_id]
    time.sleep(0.1)
    final, _, _ = call("atlas", "job", job_id=job_id)
    assert final["state"] == "cancelled"


# ── S-5: cancellation reaches the Mac ────────────────────────────────────────


class Pipe:
    """A line-oriented stdin the server reads while a test writes."""

    def __init__(self) -> None:
        r, w = os.pipe()
        self.reader = os.fdopen(r, "r")
        self.writer = os.fdopen(w, "w")

    def send(self, msg):
        self.writer.write(json.dumps(msg) + "\n")
        self.writer.flush()

    def close(self):
        self.writer.close()


class Sink:
    def __init__(self) -> None:
        self.lines = []
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.lines.extend(json.loads(x) for x in text.splitlines() if x)

    def flush(self):
        pass


def _running_mac(rig, cancels):
    def behave(job):
        rig.events(job["id"], {"type": "started", "data": {}})
        for _ in range(200):
            try:
                if rig.store.record_events(rig.node_id, job["id"], []):
                    cancels.append(job["id"])
                    rig.store.record_events(rig.node_id, job["id"], [
                        {"seq": 2, "type": "result",
                         "data": {"state": "cancelled"}}])
                    return
            except mac_nodes.MacError:
                return
            time.sleep(0.02)
    return behave


def _wait_for(pred, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_notifications_cancelled_mid_call_reaches_the_mac(rig):
    rig.pair()
    cancels = []
    rig.mac(_running_mac(rig, cancels))
    stdin, out = Pipe(), Sink()
    server = mac_mcp.Server("atlas")
    t = threading.Thread(target=server.serve, args=(stdin.reader, out),
                         daemon=True)
    t.start()
    stdin.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "run",
                           "arguments": {"command": "sleep 300"}}})
    assert _wait_for(lambda: any(j.get("state") == "running"
                                 for j in rig.store._all_jobs()))
    # The call is still in flight; stdin must still be read.
    stdin.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": 2, "reason": "AbortError: user-cancel"}})
    assert _wait_for(lambda: len(cancels) == 1), "the Mac never heard the cancel"
    time.sleep(0.2)
    assert not any(line.get("id") == 2 for line in out.lines), \
        "a cancelled request must not be answered"
    stdin.close()
    t.join(3)


def test_stdin_eof_cancels_every_foreground_job_but_not_background(rig):
    rig.pair()
    cancels = []
    rig.mac(_running_mac(rig, cancels))
    stdin, out = Pipe(), Sink()
    server = mac_mcp.Server("atlas")
    t = threading.Thread(target=server.serve, args=(stdin.reader, out),
                         daemon=True)
    t.start()
    stdin.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "run", "arguments": {
                    "command": "sleep 300", "background": True}}})
    assert _wait_for(lambda: any(line.get("id") == 1 for line in out.lines))
    stdin.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "run",
                           "arguments": {"command": "sleep 301"}}})
    assert _wait_for(lambda: sum(j.get("state") == "running"
                                 for j in rig.store._all_jobs()) == 2)
    stdin.close()
    t.join(3)
    assert not t.is_alive()
    assert _wait_for(lambda: len(cancels) == 1)
    fg = next(j for j in rig.store._all_jobs() if "301" in j["summary"])
    assert cancels == [fg["id"]]


def test_sigterm_cancels_in_flight_foreground_jobs(rig):
    """MEASURED: `claude stop` SIGTERMs the server with no cancel, no EOF."""
    rig.pair()
    cancels = []
    rig.mac(_running_mac(rig, cancels))
    server = mac_mcp.Server("atlas")
    t = threading.Thread(target=server.handle, args=({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "run", "arguments": {"command": "sleep 302"}}},),
        daemon=True)
    t.start()
    assert _wait_for(lambda: any(j.get("state") == "running"
                                 for j in rig.store._all_jobs()))
    server.cancel_all()
    assert _wait_for(lambda: len(cancels) == 1)


# ── the other kinds ──────────────────────────────────────────────────────────


def _answers(rig, payload, state="done"):
    rig.mac(lambda job: rig.events(job["id"], {"type": "result", "data": {
        "state": state, "payload": payload}}))


def test_read_returns_the_page(rig):
    rig.pair()
    _answers(rig, {"text": "hello", "size": 5, "offset": 0, "returned": 5,
                   "truncated": False})
    body, is_error, _ = call("atlas", "read", path="~/w/a.txt")
    assert not is_error
    assert body == {"ok": True, "mac": MAC, "path": "~/w/a.txt", "text": "hello",
                    "size": 5, "offset": 0, "returned": 5, "truncated": False}


def test_write_list_and_open(rig):
    rig.pair()
    replies = {"write": {"bytes": 3, "path": "/Users/o/w/a.txt"},
               "list": {"path": "/Users/o/w", "entries": [
                   {"name": "a.txt", "kind": "file", "size": 3,
                    "modified": 1.0}], "truncated": False},
               "open": {}}
    rig.mac(lambda job: rig.events(job["id"], {"type": "result", "data": {
        "state": "done", "payload": replies[job["kind"]]}}))
    wrote, _, _ = call("atlas", "write", path="~/w/a.txt", content="abc")
    assert wrote == {"ok": True, "mac": MAC, "path": "/Users/o/w/a.txt",
                     "bytes": 3}
    listed, _, _ = call("atlas", "list", path="~/w")
    assert listed["ok"] and listed["entries"][0]["name"] == "a.txt"
    assert listed["path"] == "/Users/o/w" and listed["truncated"] is False
    opened, _, _ = call("atlas", "open", target="https://example.com")
    assert opened == {"ok": True, "mac": MAC}


def test_screenshot_is_an_image_block(rig):
    rig.pair()
    _answers(rig, {"mime": "image/jpeg", "base64": "AAAA", "width": 10,
                   "height": 5})
    body, is_error, reply = call("atlas", "screenshot")
    assert not is_error and body == {"ok": True, "mac": MAC, "width": 10,
                                     "height": 5}
    image = next(c for c in reply["result"]["content"] if c["type"] == "image")
    assert image == {"type": "image", "mimeType": "image/jpeg", "data": "AAAA"}


def test_bad_input_is_refused_before_anything_is_queued(rig):
    rig.pair()
    rig.poll()
    body, is_error, _ = call("atlas", "run", command="ls", timeout_s=601)
    assert is_error and body["reason"] == "bad_input"
    body, is_error, _ = call("atlas", "run", command="")
    assert is_error and body["reason"] == "bad_input"
    body, is_error, _ = call("atlas", "nope")
    assert is_error and body["reason"] == "bad_input"
    assert rig.store._all_jobs() == []


def test_capability_off_and_unknown_mac_are_refusals_without_a_card(rig):
    rig.pair(caps=["run", "read"])
    rig.poll()
    body, is_error, _ = call("atlas", "screenshot")
    assert is_error and body["reason"] == "capability_off"
    body, is_error, _ = call("atlas", "run", command="ls", mac="Nope")
    assert is_error and body["reason"] == "unknown_mac"
    assert handoff.load(rig.handoffs) == []


# ── status ───────────────────────────────────────────────────────────────────


def test_status_names_each_mac_and_this_desks_access(rig):
    rig.pair()
    other = rig.pair(name="Studio", machine="m-2")
    now = rig.clock()
    rig.poll(grants={"atlas": {"state": "hour", "until": now + 600},
                     "scout": {"state": "hour", "until": now - 1},
                     "acme": {"state": "denied", "until": now + 600}})
    rig.store.poll(other, 4, [], "full", {})
    body, _, _ = call("atlas", "status")
    macs = {m["name"]: m for m in body["macs"]}
    assert macs[MAC]["your_access"] == "hour" and macs[MAC]["online"] is True
    assert macs[MAC]["until"] == pytest.approx(now + 600)
    assert macs[MAC]["primary"] is True and macs[MAC]["mode"] == "ask"
    assert macs["Studio"]["your_access"] == "full"
    assert call("scout", "status")[0]["macs"][0]["your_access"] == "none"
    assert call("acme", "status")[0]["macs"][0]["your_access"] == "denied"
    assert call("nobody", "status")[0]["macs"][0]["your_access"] == "none"
