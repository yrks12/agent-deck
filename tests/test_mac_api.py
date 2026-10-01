"""The Mac bridge's wire (MB1) and its back-online rule (MB3), over real HTTP.

What these pin:

* **No HTTP route creates a job.** The router's (method, path) set equals MB1's
  list exactly (S-6). A leaked bearer token can list Macs; it cannot run code.
* Every route refuses without the bearer; the Mac's own routes also refuse
  without, or with a wrong, `X-Deck-Node` -- including another Mac's (S-6).
* A job goes enqueue -> poll -> events -> result over the wire (S-1).
* A long-poll returns at once when something is pending, wakes when a job is
  queued mid-wait, and otherwise answers empty after `wait` -- never later.
* **Back online** (S-2, router half): the first poll after the Mac was offline
  resolves every offline card the desks filed and tells each desk exactly once.
* Grants (S-9): hour/always/deny tell the desk and wake it; revoke only queues.
* The owner's audit copy never carries output, and nothing carries the secret.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import handoff, owner

try:  # absent, every test must FAIL on behaviour, not error at collection
    from server import mac_api, mac_nodes
except ImportError:  # pragma: no cover - the RED state
    mac_api = mac_nodes = None

TOKEN = "test-token-not-a-real-one"
BEARER = {"Authorization": f"Bearer {TOKEN}"}

#: MB1, restated here rather than imported: a test that reads the list off the
#: code under test cannot notice the code growing a job-creating route.
MB1_ROUTES = {
    ("POST", "/v1/nodes"),
    ("GET", "/v1/nodes"),
    ("PATCH", "/v1/nodes/{node_id}"),
    ("DELETE", "/v1/nodes/{node_id}"),
    ("GET", "/v1/nodes/{node_id}/jobs"),
    ("POST", "/v1/nodes/{node_id}/poll"),
    ("POST", "/v1/nodes/{node_id}/jobs/{job_id}/events"),
    ("POST", "/v1/nodes/{node_id}/grants"),
}

NODE_ROUTES = [
    ("POST", "/v1/nodes/{n}/poll", {"wait": 0, "free_slots": 0, "mode": "ask"}),
    ("POST", "/v1/nodes/{n}/jobs/mj_000000000000/events", {"events": []}),
    ("POST", "/v1/nodes/{n}/grants", {"desk": "atlas", "decision": "hour"}),
]


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class Rig:
    def __init__(self, tmp_path) -> None:
        assert mac_api is not None, "server/mac_api.py does not exist"
        self.clock = Clock()
        self.store = mac_nodes.Store(tmp_path / "mac", clock=self.clock)
        self.handoffs = tmp_path / "handoffs.json"
        self.told: list[tuple[str, str]] = []
        self.queued: list[tuple[str, str]] = []
        self.owner: list[str] = []
        app = FastAPI()
        self.router = mac_api.build_router(
            self.store,
            tell=lambda desk, text: self.told.append((desk, text)),
            owner_line=lambda text: self.owner.append(text),
            queue=lambda desk, text: self.queued.append((desk, text)),
            handoffs_path=self.handoffs)
        app.include_router(self.router)
        self.client = TestClient(app)

    def pair(self, machine="m-1", name="Sam's MacBook Pro", header=None):
        headers = dict(BEARER)
        if header:
            headers["X-Deck-Node"] = header
        return self.client.post("/v1/nodes", headers=headers, json={
            "machine_id": machine, "name": name, "os": "macOS 26.0 (25A354)",
            "app_version": "1.5.0",
            "capabilities": ["run", "read", "write", "list", "open", "screenshot"],
            "mode": "ask"})

    def mac(self, machine="m-1", name="Sam's MacBook Pro"):
        body = self.pair(machine, name).json()
        return body["node_id"], {**BEARER,
                                 "X-Deck-Node": f"{body['node_id']}.{body['node_secret']}"}

    def poll(self, node_id, headers, **kw):
        body = {"wait": 0, "free_slots": 4, "running": [], "mode": "ask",
                "grants": {}}
        body.update(kw)
        return self.client.post(f"/v1/nodes/{node_id}/poll", headers=headers,
                                json=body)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    return Rig(tmp_path)


# ── S-6: the route set and auth ──────────────────────────────────────────────


def test_the_router_serves_exactly_mb1_and_no_route_creates_a_job(rig):
    served = {(method, route.path) for route in rig.router.routes
              for method in route.methods}
    assert served == MB1_ROUTES
    assert not [p for m, p in served if m in ("POST", "PUT") and
                p.rstrip("/").endswith(("/jobs", "/run", "/job"))], \
        "a route that could enqueue a job exists"


def test_every_route_refuses_without_the_bearer(rig):
    node_id, _ = rig.mac()
    for method, path in MB1_ROUTES:
        url = path.format(node_id=node_id, job_id="mj_000000000000")
        r = rig.client.request(method, url, json={})
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json()["reason"] == "unauthorized"


@pytest.mark.parametrize("method,path,body", NODE_ROUTES)
def test_node_routes_need_this_macs_own_secret(rig, method, path, body):
    node_id, good = rig.mac()
    other_id, other = rig.mac("m-2", "Studio")
    url = path.format(n=node_id)
    for headers in (BEARER, {**BEARER, "X-Deck-Node": f"{node_id}.wrong"},
                    {**BEARER, "X-Deck-Node": node_id}, other):
        r = rig.client.request(method, url, headers=headers, json=body)
        assert (r.status_code, r.json()["reason"]) == (401, "unknown_node"), headers
        assert r.json()["ok"] is False
    assert rig.client.request(method, url, headers=good, json=body).status_code \
        != 401


def test_no_listing_ever_carries_the_secret(rig):
    body = rig.pair().json()
    listing = rig.client.get("/v1/nodes", headers=BEARER).text
    assert body["node_secret"] not in listing
    assert "secret" not in listing


# ── registration ─────────────────────────────────────────────────────────────


def test_registration_pairs_refreshes_and_re_pairs_with_one_owner_line_each(rig):
    first = rig.pair()
    assert first.status_code == 201
    body = first.json()
    assert set(body) == {"node_id", "node_secret", "name", "primary"}
    assert body["primary"] is True
    assert rig.owner == ["A Mac connected as *Sam's MacBook Pro*"]

    header = f"{body['node_id']}.{body['node_secret']}"
    again = rig.pair(header=header)
    assert again.status_code == 200
    assert set(again.json()) == {"node_id", "name", "primary"}
    assert len(rig.owner) == 1, "a refresh is not news"

    repaired = rig.pair(header=f"{body['node_id']}.stolen")
    assert repaired.status_code == 201 and repaired.json()["node_secret"]
    assert rig.owner[-1] == "*Sam's MacBook Pro* re-paired"
    assert rig.poll(body["node_id"], {**BEARER, "X-Deck-Node": header}) \
        .status_code == 401, "the old secret still works after a re-pair"


@pytest.mark.parametrize("payload", [
    {"name": "Mac"}, {"machine_id": "m"}, {"machine_id": "m", "name": "x" * 65},
    {"machine_id": "m", "name": "a\nb"}, [],
])
def test_registration_refuses_bad_input(rig, payload):
    r = rig.client.post("/v1/nodes", headers=BEARER, json=payload)
    assert (r.status_code, r.json()["reason"]) == (400, "bad_input")


# ── S-1: the round trip over the wire ────────────────────────────────────────


def test_a_job_round_trips_over_http(rig):
    node_id, headers = rig.mac()
    assert rig.poll(node_id, headers).json()["jobs"] == []
    job = rig.store.enqueue("atlas", "run", {"command": "sw_vers"}, mac=None)

    r = rig.poll(node_id, headers)
    assert r.status_code == 200
    body = r.json()
    assert [j["id"] for j in body["jobs"]] == [job["id"]]
    assert body["jobs"][0]["args"]["command"] == "sw_vers"
    assert body["cancel"] == [] and isinstance(body["server_ts"], float)

    url = f"/v1/nodes/{node_id}/jobs/{job['id']}/events"
    r = rig.client.post(url, headers=headers, json={"events": [
        {"seq": 1, "type": "started", "data": {"pid": 1, "cwd": "/", "sandboxed": True}},
        {"seq": 2, "type": "stdout", "data": "ProductVersion:\t26.0\n"},
        {"seq": 3, "type": "result", "data": {"state": "done", "exit": 0,
                                              "duration_ms": 5, "payload": {}}}]})
    assert r.json() == {"ok": True, "cancel": False}
    assert rig.store.job(job["id"])["state"] == "done"
    assert rig.store.output(job["id"], "stdout", 0)[0] == "ProductVersion:\t26.0\n"

    r = rig.client.post(url, headers=headers, json={"events": [
        {"seq": 4, "type": "stdout", "data": "late"}]})
    assert (r.status_code, r.json()["reason"]) == (409, "job_settled")
    r = rig.client.post(f"/v1/nodes/{node_id}/jobs/mj_ffffffffffff/events",
                        headers=headers, json={"events": []})
    assert (r.status_code, r.json()["reason"]) == (404, "unknown_job")


def test_a_mac_posting_on_another_macs_job_is_refused(rig):
    a_id, a = rig.mac()
    b_id, b = rig.mac("m-2", "Studio")
    rig.poll(a_id, a)
    rig.poll(b_id, b)
    job = rig.store.enqueue("atlas", "run", {"command": "id"}, mac=a_id)
    rig.poll(a_id, a)
    r = rig.client.post(f"/v1/nodes/{b_id}/jobs/{job['id']}/events", headers=b,
                        json={"events": []})
    assert (r.status_code, r.json()["reason"]) == (403, "not_your_job")


def test_a_desks_cancel_reaches_the_mac_on_its_next_events_post(rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers)
    job = rig.store.enqueue("atlas", "run", {"command": "sleep 300"}, mac=None)
    rig.poll(node_id, headers)
    rig.store.request_cancel(job["id"], "atlas")
    r = rig.client.post(f"/v1/nodes/{node_id}/jobs/{job['id']}/events",
                        headers=headers, json={"events": []})
    assert r.json() == {"ok": True, "cancel": True}
    assert rig.poll(node_id, headers, running=[job["id"]]).json()["cancel"] == \
        [job["id"]]


# ── the long-poll ────────────────────────────────────────────────────────────


def test_a_long_poll_answers_empty_after_wait_and_never_later(rig):
    node_id, headers = rig.mac()
    started = time.monotonic()
    body = rig.poll(node_id, headers, wait=1).json()
    took = time.monotonic() - started
    assert body["jobs"] == [] and body["cancel"] == []
    assert 0.9 <= took < 2.0, took


def test_the_wait_is_clamped_to_the_maximum(rig, monkeypatch):
    assert mac_api.MAX_WAIT == 25.0
    node_id, headers = rig.mac()
    monkeypatch.setattr(mac_api, "MAX_WAIT", 0.5)
    started = time.monotonic()
    assert rig.poll(node_id, headers, wait=3600).status_code == 200
    assert time.monotonic() - started < 1.5


def test_a_long_poll_wakes_when_a_job_is_queued_mid_wait(rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers)
    queued = {}

    def later():
        time.sleep(0.4)
        queued["job"] = rig.store.enqueue("atlas", "run", {"command": "date"},
                                          mac=None)

    threading.Thread(target=later).start()
    started = time.monotonic()
    body = rig.poll(node_id, headers, wait=10).json()
    took = time.monotonic() - started
    assert [j["id"] for j in body["jobs"]] == [queued["job"]["id"]]
    assert took < 3.0, f"the poll slept {took:.1f}s past the job"


def test_a_pending_job_returns_at_once(rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers)
    rig.store.enqueue("atlas", "run", {"command": "date"}, mac=None)
    started = time.monotonic()
    assert len(rig.poll(node_id, headers, wait=25).json()["jobs"]) == 1
    assert time.monotonic() - started < 1.0


# ── S-2: back online ─────────────────────────────────────────────────────────


def test_the_first_poll_after_offline_resolves_the_card_and_tells_the_desk_once(
        rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers)
    rig.clock.offset += mac_nodes.ONLINE_WINDOW + 1
    with pytest.raises(mac_nodes.MacError) as err:
        rig.store.enqueue("atlas", "run", {"command": "sw_vers"}, mac=None)
    assert err.value.reason == "mac_offline"
    # what the desk tool (M2) does with that refusal:
    card = handoff.raise_handoff(
        rig.handoffs, agent="atlas", kind="other",
        needs="Open Agent Deck on Sam's MacBook Pro so atlas can use it",
        state="Nothing ran on the Mac. atlas wanted to: sw_vers",
        where="Sam's MacBook Pro")
    rig.store.set_offline_card(node_id, "atlas", card.id)
    assert rig.told == []

    rig.poll(node_id, headers)
    assert [h.status for h in handoff.load(rig.handoffs)] == ["done"]
    assert rig.told == [("atlas", handoff.resume_message(
        handoff.load(rig.handoffs)[0], "done"))]
    rig.poll(node_id, headers)
    rig.poll(node_id, headers, wait=1)
    assert len(rig.told) == 1, "a desk was told the Mac is back more than once"


def test_a_paused_poll_does_not_count_as_back(rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers, mode="paused")
    card = handoff.raise_handoff(rig.handoffs, agent="atlas", kind="other",
                                 needs="n", state="s", where="w")
    rig.store.set_offline_card(node_id, "atlas", card.id)
    rig.poll(node_id, headers, mode="paused")
    assert rig.told == [] and handoff.load(rig.handoffs)[0].status == "waiting"
    rig.poll(node_id, headers, mode="ask")
    assert len(rig.told) == 1


def test_a_card_he_already_closed_still_gets_the_desk_a_back_online_line(rig):
    node_id, headers = rig.mac()
    card = handoff.raise_handoff(rig.handoffs, agent="atlas", kind="other",
                                 needs="n", state="s", where="w")
    handoff.resolve(rig.handoffs, card.id, "skipped")
    rig.store.set_offline_card(node_id, "atlas", card.id)
    rig.poll(node_id, headers)
    assert len(rig.told) == 1 and "back online" in rig.told[0][1]
    assert handoff.load(rig.handoffs)[0].status == "skipped"


# ── S-9: grants ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("decision,phrase", [
    ("hour", "allowed you to use his Mac (Sam's MacBook Pro) for 1 hour"),
    ("always", "allowed you to use his Mac (Sam's MacBook Pro) always"),
    ("deny", "said no to you using his Mac (Sam's MacBook Pro). Do not ask again"),
])
def test_a_grant_tells_the_desk_and_wakes_it(rig, decision, phrase):
    node_id, headers = rig.mac()
    r = rig.client.post(f"/v1/nodes/{node_id}/grants", headers=headers,
                        json={"desk": "atlas", "decision": decision,
                              "until": time.time() + 3600})
    assert r.json() == {"ok": True, "told": True}
    assert len(rig.told) == 1 and rig.told[0][0] == "atlas"
    assert rig.told[0][1].startswith(f"[Agent Deck] {owner.title()} ")
    assert phrase in rig.told[0][1]
    assert rig.queued == []


def test_a_revoke_only_queues_a_line_and_never_wakes(rig):
    node_id, headers = rig.mac()
    r = rig.client.post(f"/v1/nodes/{node_id}/grants", headers=headers,
                        json={"desk": "atlas", "decision": "revoke", "until": None})
    assert r.json() == {"ok": True, "told": True}
    assert rig.told == [] and [d for d, _ in rig.queued] == ["atlas"]


@pytest.mark.parametrize("payload", [
    {"desk": "atlas", "decision": "maybe"}, {"decision": "hour"},
    {"desk": "atlas", "decision": "hour", "until": "soon"},
])
def test_a_bad_grant_is_refused(rig, payload):
    node_id, headers = rig.mac()
    r = rig.client.post(f"/v1/nodes/{node_id}/grants", headers=headers, json=payload)
    assert (r.status_code, r.json()["reason"]) == (400, "bad_input")
    assert rig.told == []


# ── the owner's routes ───────────────────────────────────────────────────────


def test_the_owner_sees_macs_and_can_rename_move_primary_and_forget(rig):
    a_id, a = rig.mac()
    b_id, b = rig.mac("m-2", "Studio")
    rig.poll(a_id, a)
    nodes = rig.client.get("/v1/nodes", headers=BEARER).json()["nodes"]
    assert [set(n) for n in nodes] == [{"node_id", "name", "os", "online", "mode",
                                        "last_seen", "primary", "running"}] * 2
    assert [(n["name"], n["online"], n["primary"]) for n in nodes] == [
        ("Sam's MacBook Pro", True, True), ("Studio", False, False)]

    r = rig.client.patch(f"/v1/nodes/{b_id}", headers=BEARER,
                         json={"name": "Studio Mac", "primary": True})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["primary"]) == ("Studio Mac", True)
    nodes = rig.client.get("/v1/nodes", headers=BEARER).json()["nodes"]
    assert [n["primary"] for n in nodes] == [False, True]
    assert rig.client.patch(f"/v1/nodes/{b_id}", headers=BEARER,
                            json={"secret": "x"}).status_code == 400

    job = rig.store.enqueue("atlas", "run", {"command": "id"}, mac=a_id)
    r = rig.client.delete(f"/v1/nodes/{a_id}", headers=BEARER)
    assert r.json() == {"ok": True, "refused": [job["id"]]}
    assert rig.store.job(job["id"])["reason"] == "unknown_mac"
    assert rig.client.delete(f"/v1/nodes/{a_id}", headers=BEARER).status_code == 404
    assert rig.poll(a_id, a).status_code == 401


def test_the_audit_copy_names_what_ran_and_never_its_output(rig):
    node_id, headers = rig.mac()
    rig.poll(node_id, headers)
    job = rig.store.enqueue("atlas", "run", {"command": "cat notes.txt"}, mac=None)
    rig.poll(node_id, headers)
    rig.client.post(f"/v1/nodes/{node_id}/jobs/{job['id']}/events", headers=headers,
                    json={"events": [
                        {"seq": 1, "type": "stdout", "data": "SECRET-CONTENT"},
                        {"seq": 2, "type": "result", "data": {"state": "done",
                                                              "exit": 0}}]})
    r = rig.client.get(f"/v1/nodes/{node_id}/jobs?limit=50", headers=BEARER)
    assert r.status_code == 200
    rows = r.json()["jobs"]
    assert [(j["id"], j["desk"], j["kind"], j["summary"], j["state"], j["exit"])
            for j in rows] == [(job["id"], "atlas", "run", "cat notes.txt", "done", 0)]
    assert "SECRET-CONTENT" not in r.text
    assert rig.client.get("/v1/nodes/mac_000000000000/jobs",
                          headers=BEARER).status_code == 404
