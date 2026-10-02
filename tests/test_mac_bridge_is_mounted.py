"""The Mac bridge must be served by the REAL app, end to end (M3 / MB4).

Measured 2026-09-30: `server/mac_api.py` was built and tested on a hand-made
FastAPI, but `server/app.py` never mounted it. The live deck answered 404 on
`/v1/nodes`, so the Mac app could not register and no desk could ever reach
the owner's Mac -- every `mcp__mac__*` call ended `no_mac`.

These drive `server.app.app` itself, with the desk side played by the real
`mac_mcp.Server` and the Mac played over HTTP:

* `/v1/nodes` needs the bearer and lists what registered; a new Mac puts one
  line in the owner's thread (`owner_line`).
* A desk's `run` reaches the Mac; the Mac (which enforces the grant) reports
  `awaiting_grant` -- the card on his screen -- and the deck's audit copy says
  so.
* His tap (`POST .../grants`) reaches the desk through `tell`: an office line
  from `deck` plus a wake with reason `mac`. Then the Mac runs it and the
  result comes back to the desk.
* A deny, or no answer, ends the job refused: no output ever reaches the desk.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import mac_mcp, mac_nodes, office

TOKEN = "deck-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DESK = "wake-probe"
SW_VERS = "ProductName:\tmacOS\nProductVersion:\t26.0\n"


class Mac:
    """The owner's Mac, as the app speaks to the deck."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        r = client.post("/v1/nodes", headers=AUTH, json={
            "machine_id": "m-test", "name": "Test Mac", "os": "macOS 26.0",
            "app_version": "1.0.0",
            "capabilities": ["run", "read", "write", "list", "open"],
            "mode": "ask"})
        assert r.status_code == 201, r.text
        body = r.json()
        self.node = body["node_id"]
        self.headers = {**AUTH,
                        "X-Deck-Node": f"{body['node_id']}.{body['node_secret']}"}
        self.seq = 0

    def poll(self, wait: float = 0) -> list[dict]:
        r = self.client.post(f"/v1/nodes/{self.node}/poll", headers=self.headers,
                             json={"wait": wait, "free_slots": 4, "running": [],
                                   "mode": "ask", "grants": {}})
        assert r.status_code == 200, r.text
        return r.json()["jobs"]

    def claim(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.poll(wait=1)
            if jobs:
                return jobs[0]
        raise AssertionError("no job reached the Mac")

    def events(self, job_id: str, *events: tuple[str, object]) -> None:
        batch = []
        for kind, data in events:
            self.seq += 1
            event = {"seq": self.seq, "type": kind}
            if data is not None:
                event["data"] = data
            batch.append(event)
        r = self.client.post(f"/v1/nodes/{self.node}/jobs/{job_id}/events",
                             headers=self.headers, json={"events": batch})
        assert r.status_code == 200, r.text

    def tap(self, decision: str) -> dict:
        r = self.client.post(f"/v1/nodes/{self.node}/grants", headers=self.headers,
                             json={"desk": DESK, "decision": decision,
                                   "until": None})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def deck(monkeypatch, tmp_path):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    root = tmp_path / "mac"
    served = getattr(app_mod, "_mac_store", None)
    if served is not None:  # absent = the RED state; the routes 404 instead
        for name, value in vars(mac_nodes.Store(root)).items():
            monkeypatch.setattr(served, name, value)
    # The desk's `mac` MCP process and the deck share one store on disk.
    monkeypatch.setattr(mac_mcp, "STORE", mac_nodes.Store(root))
    monkeypatch.setattr(mac_mcp, "HANDOFFS_PATH", tmp_path / "handoffs.json")
    woken: list[tuple[str, str]] = []
    monkeypatch.setattr(app_mod, "_try_inject", lambda target, text: False)
    monkeypatch.setattr(app_mod, "_wake_later",
                        lambda name, reason: woken.append((name, reason)))
    client = TestClient(app_mod.app)
    client.woken = woken
    return client


def _office(to: str) -> list[dict]:
    try:
        lines = office.MESSAGES_FILE.read_text().splitlines()
    except OSError:
        return []
    return [r for r in map(json.loads, lines) if r.get("to") == to]


def _ask(desk: str, command: str) -> tuple[threading.Thread, dict]:
    """The desk calls mcp__mac__run; its answer lands in `out`."""
    out: dict = {}

    def call() -> None:
        out["answer"] = mac_mcp.Server(desk).tool("run", {"command": command})

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, out


def _audit(deck: TestClient, mac: Mac) -> list[dict]:
    r = deck.get(f"/v1/nodes/{mac.node}/jobs", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["jobs"]


# ── mounted, behind auth ─────────────────────────────────────────────────────


def test_nodes_needs_the_bearer_on_the_real_app(deck):
    r = deck.get("/v1/nodes")
    assert r.status_code == 401, (r.status_code, r.text)


def test_a_registered_mac_is_listed_and_the_owner_is_told(deck):
    mac = Mac(deck)
    mac.poll()   # the poll is the heartbeat: online from here
    r = deck.get("/v1/nodes", headers=AUTH)
    assert r.status_code == 200
    [row] = r.json()["nodes"]
    assert row["node_id"] == mac.node and row["name"] == "Test Mac"
    assert row["online"] is True
    assert any(rec["text"] == "A Mac connected as *Test Mac*"
               and rec["from"] == "deck" for rec in _office(office.OWNER_INBOX))


# ── the desk's request, his card, his tap, the result ───────────────────────


def test_a_desk_request_reaches_the_mac_and_waits_on_his_card(deck):
    mac = Mac(deck)
    mac.poll()
    thread, out = _ask(DESK, "sw_vers")
    job = mac.claim()
    assert (job["desk"], job["kind"], job["args"]["command"]) == \
        (DESK, "run", "sw_vers")
    mac.events(job["id"], ("awaiting_grant", None))
    [row] = _audit(deck, mac)
    assert row["state"] == "awaiting_grant", row
    assert "answer" not in out, "the desk was answered before he tapped"
    mac.events(job["id"], ("result", {"state": "refused",
                                      "reason": "awaiting_grant",
                                      "detail": "not answered yet"}))
    thread.join(5)


def test_after_his_grant_the_job_runs_and_the_result_reaches_the_desk(deck):
    mac = Mac(deck)
    mac.poll()
    thread, out = _ask(DESK, "sw_vers")
    job = mac.claim()
    mac.events(job["id"], ("awaiting_grant", None))

    told = mac.tap("hour")
    assert told == {"ok": True, "told": True}
    assert (DESK, "mac") in deck.woken, deck.woken
    lines = [r for r in _office(DESK) if r["from"] == "deck"]
    assert any("allowed you to use his Mac (Test Mac) for 1 hour" in r["text"]
               for r in lines), lines

    mac.events(job["id"], ("started", {"pid": 4123, "cwd": "/tmp",
                                       "sandboxed": True}),
               ("stdout", SW_VERS),
               ("result", {"state": "done", "exit": 0, "duration_ms": 12}))
    thread.join(10)
    answer = out["answer"]
    assert answer["ok"] is True, answer
    assert answer["exit"] == 0 and answer["stdout"] == SW_VERS
    assert _audit(deck, mac)[0]["state"] == "done"


@pytest.mark.parametrize("decision,reason", [("deny", "denied"),
                                             (None, "awaiting_grant")])
def test_denied_or_unanswered_never_runs(deck, decision, reason):
    mac = Mac(deck)
    mac.poll()
    thread, out = _ask(DESK, "sw_vers")
    job = mac.claim()
    mac.events(job["id"], ("awaiting_grant", None))
    if decision:
        assert mac.tap(decision)["told"] is True
        assert any("said no to you using his Mac" in r["text"]
                   for r in _office(DESK)), _office(DESK)
    mac.events(job["id"], ("result", {"state": "refused", "reason": reason,
                                      "detail": "he did not allow it"}))
    thread.join(10)
    answer = out["answer"]
    assert answer["ok"] is False and answer["reason"] == reason, answer
    assert "stdout" not in answer
    [row] = _audit(deck, mac)
    assert row["state"] == "refused" and row["started_at"] is None, row
