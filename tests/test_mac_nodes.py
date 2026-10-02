"""The Mac bridge's store (MB2): nodes, the job queue, and the states a job reaches.

What these pin, each as behaviour a desk or the owner would notice:

* A job goes queued -> claimed -> running -> done and the desk reads the Mac's
  stdout back (S-1, store half).
* **Offline never hangs.** A Mac that has not polled for ONLINE_WINDOW is
  offline and a job for it is refused at once, naming the Mac -- it is never
  queued to wait on a machine that is not there (S-2, store half). A queued job
  nobody picks up expires `mac_not_answering` at CLAIM_WAIT; a claimed job that
  stops reporting is `lost` at LOST_AFTER (S-4).
* Output is capped: 2 MiB in, at most 1 MiB kept, the dropped count recorded,
  the head and the newest bytes kept (S-7).
* Limits: a desk's 3rd concurrent job waits; the 61st in a minute is
  `rate_limited`; the 33rd queued on one Mac is `node_busy` (S-8).
* The node secret is stored hashed; the plaintext exists only in the pairing
  answer.
* Two kinds of process write the same files, so every read-modify-write is
  locked and every write is whole: parallel writers lose nothing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

try:  # absent, every test must still FAIL on behaviour, not error at collection
    from server import mac_nodes
except ImportError:  # pragma: no cover - the RED state
    mac_nodes = None

REPO = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    assert mac_nodes is not None, "server/mac_nodes.py does not exist"
    return mac_nodes.Store(tmp_path / "mac", clock=clock)


def pair(store, machine="m-1", name="Sam's MacBook Pro", mode="ask"):
    return store.register(machine, name, "macOS 26.0", "1.5.0",
                          ["run", "read", "write", "list", "open"], mode, None)


def online(store, node_id, free=4, running=None, mode="ask"):
    return store.poll(node_id, free, running or [], mode, {})


def run_job(store, desk="atlas", command="sw_vers", mac=None, **kw):
    return store.enqueue(desk, "run", {"command": command}, mac=mac, **kw)


# ── pairing ──────────────────────────────────────────────────────────────────


def test_pairing_returns_a_secret_once_and_stores_only_its_hash(store, tmp_path):
    node = pair(store)
    assert node["status"] == 201 and node["event"] == "new"
    assert node["node_id"].startswith("mac_") and len(node["node_id"]) == 16
    assert len(node["node_secret"]) == 43
    assert node["primary"] is True

    raw = (tmp_path / "mac" / "nodes.json").read_text()
    assert node["node_secret"] not in raw, "the plaintext secret reached disk"
    assert hashlib.sha256(node["node_secret"].encode()).hexdigest() in raw

    header = f"{node['node_id']}.{node['node_secret']}"
    assert store.check_node(header) == node["node_id"]
    for bad in (None, "", node["node_id"], f"{node['node_id']}.wrong",
                f"mac_000000000000.{node['node_secret']}"):
        with pytest.raises(mac_nodes.MacError) as err:
            store.check_node(bad)
        assert (err.value.reason, err.value.status) == ("unknown_node", 401)


def test_a_known_mac_with_its_secret_refreshes_and_without_it_re_pairs(store):
    first = pair(store)
    header = f"{first['node_id']}.{first['node_secret']}"

    again = store.register("m-1", "Renamed", "macOS 26.1", "1.5.1", ["run"],
                           "full", header)
    assert again["status"] == 200 and again["event"] is None
    assert "node_secret" not in again
    assert again["node_id"] == first["node_id"] and again["name"] == "Renamed"
    assert store.check_node(header) == first["node_id"]

    repaired = store.register("m-1", "Renamed", "", "", [], "ask", None)
    assert repaired["status"] == 201 and repaired["event"] == "re-paired"
    assert repaired["node_id"] == first["node_id"]
    with pytest.raises(mac_nodes.MacError):
        store.check_node(header)          # the old secret is dead
    assert store.check_node(f"{first['node_id']}.{repaired['node_secret']}")


def test_only_the_first_mac_is_primary(store):
    assert pair(store, "m-1")["primary"] is True
    assert pair(store, "m-2", "Studio")["primary"] is False


@pytest.mark.parametrize("machine,name", [
    ("", "Mac"), (None, "Mac"), ("m", ""), ("m", "x" * 65), ("m", "bad\x07name"),
])
def test_registration_refuses_bad_input(store, machine, name):
    with pytest.raises(mac_nodes.MacError) as err:
        store.register(machine, name, "", "", [], "ask", None)
    assert (err.value.reason, err.value.status) == ("bad_input", 400)


# ── S-1: a round trip ────────────────────────────────────────────────────────


def test_a_job_round_trips_from_desk_to_mac_and_back(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    assert job["state"] == "queued" and job["node"] == node["node_id"]

    jobs, cancel = online(store, node["node_id"])
    assert cancel == [] and [j["id"] for j in jobs] == [job["id"]]
    assert set(jobs[0]) == {"id", "desk", "kind", "args", "timeout_s",
                            "background", "created_at"}
    assert jobs[0]["args"] == {"command": "sw_vers", "cwd": None, "env": {}}
    assert store.job(job["id"])["state"] == "claimed"

    clock.t += 1
    assert store.record_events(node["node_id"], job["id"], [
        {"seq": 1, "type": "started",
         "data": {"pid": 4123, "cwd": "/Users/y", "sandboxed": True}},
        {"seq": 2, "type": "stdout", "data": "ProductName:\tmacOS\n"},
        {"seq": 3, "type": "result", "data": {"state": "done", "exit": 0,
                                              "duration_ms": 812, "payload": {}}},
    ]) is False
    done = store.job(job["id"])
    assert (done["state"], done["exit"], done["sandboxed"]) == ("done", 0, True)
    line = "ProductName:\tmacOS\n"
    assert store.output(job["id"], "stdout", 0) == (line, len(line), False)
    # nothing is handed out twice
    assert online(store, node["node_id"]) == ([], [])


def test_events_are_idempotent_and_a_settled_job_takes_no_more(store):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    batch = [{"seq": 1, "type": "stdout", "data": "a"},
             {"seq": 2, "type": "stdout", "data": "b"}]
    store.record_events(node["node_id"], job["id"], batch)
    store.record_events(node["node_id"], job["id"], batch)       # a retry
    assert store.output(job["id"], "stdout", 0)[0] == "ab"
    result = [{"seq": 3, "type": "result", "data": {"state": "failed", "exit": 1}}]
    store.record_events(node["node_id"], job["id"], result)
    store.record_events(node["node_id"], job["id"], result)      # retried result: ok
    with pytest.raises(mac_nodes.MacError) as err:
        store.record_events(node["node_id"], job["id"],
                            [{"seq": 4, "type": "stdout", "data": "late"}])
    assert (err.value.reason, err.value.status) == ("job_settled", 409)


def test_a_mac_cannot_report_on_another_macs_job(store):
    a, b = pair(store, "m-1", "A"), pair(store, "m-2", "B")
    online(store, a["node_id"])
    online(store, b["node_id"])
    job = run_job(store, mac="A")
    online(store, a["node_id"])
    with pytest.raises(mac_nodes.MacError) as err:
        store.record_events(b["node_id"], job["id"], [])
    assert (err.value.reason, err.value.status) == ("not_your_job", 403)
    with pytest.raises(mac_nodes.MacError) as err:
        store.record_events(a["node_id"], "mj_000000000000", [])
    assert (err.value.reason, err.value.status) == ("unknown_job", 404)


def test_the_result_payload_comes_back_with_the_job(store):
    node = pair(store)
    online(store, node["node_id"])
    job = store.enqueue("atlas", "read", {"path": "~/w/a.txt"}, mac=None)
    online(store, node["node_id"])
    payload = {"text": "hi", "size": 2, "offset": 0, "returned": 2,
               "truncated": False}
    store.record_events(node["node_id"], job["id"], [
        {"seq": 1, "type": "result", "data": {"state": "done", "payload": payload}}])
    assert store.job(job["id"])["payload"] == payload


# ── S-2: offline never queues ────────────────────────────────────────────────


def test_a_mac_that_stopped_polling_is_offline_and_nothing_is_queued_for_it(
        store, clock):
    node = pair(store)
    online(store, node["node_id"])
    clock.t += mac_nodes.ONLINE_WINDOW + 1
    assert mac_nodes.presence(store.node(node["node_id"]), clock.t) == "offline"
    with pytest.raises(mac_nodes.MacError) as err:
        run_job(store)
    assert err.value.reason == "mac_offline"
    assert err.value.node["node_id"] == node["node_id"]
    assert "Sam's MacBook Pro" in err.value.detail
    assert list((store.root / "jobs").glob("mj_*.json")) == []


def test_a_paused_mac_refuses_as_paused(store):
    node = pair(store)
    online(store, node["node_id"], mode="paused")
    with pytest.raises(mac_nodes.MacError) as err:
        run_job(store)
    assert err.value.reason == "mac_paused"


def test_a_paused_poll_hands_out_nothing(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    assert online(store, node["node_id"], mode="paused") == ([], [])
    assert store.job(job["id"])["state"] == "queued"


def test_offline_cards_are_popped_once_when_the_mac_is_back(store):
    node = pair(store)
    store.set_offline_card(node["node_id"], "atlas", "abc")
    store.set_offline_card(node["node_id"], "nova", "def")
    assert store.offline_card(node["node_id"], "atlas") == "abc"
    assert store.take_back_online(node["node_id"]) == {"atlas": "abc", "nova": "def"}
    assert store.take_back_online(node["node_id"]) == {}
    assert store.offline_card(node["node_id"], "atlas") is None


def test_offline_cards_stay_while_the_mac_is_paused(store):
    node = pair(store)
    online(store, node["node_id"], mode="paused")
    store.set_offline_card(node["node_id"], "atlas", "abc")
    assert store.take_back_online(node["node_id"]) == {}
    assert store.offline_card(node["node_id"], "atlas") == "abc"


def test_no_mac_card_is_remembered_for_24_hours(store, clock):
    assert store.no_mac_card("atlas") is None
    store.set_no_mac_card("atlas", "xyz")
    clock.t += 23 * 3600
    assert store.no_mac_card("atlas") == "xyz"
    clock.t += 2 * 3600
    assert store.no_mac_card("atlas") is None


# ── S-4: expiries ────────────────────────────────────────────────────────────


def test_an_unclaimed_job_expires_mac_not_answering_at_claim_wait(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    clock.t += 1
    job = run_job(store)
    clock.t += mac_nodes.CLAIM_WAIT - 0.5
    assert store.sweep() == []
    clock.t += 1
    changed = store.sweep()
    assert [j["id"] for j in changed] == [job["id"]]
    row = store.job(job["id"])
    assert (row["state"], row["reason"]) == ("expired", "mac_not_answering")


def test_a_claimed_job_with_no_events_is_lost_at_lost_after(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    clock.t += mac_nodes.LOST_AFTER - 1
    store.record_events(node["node_id"], job["id"], [])    # keepalive
    clock.t += mac_nodes.LOST_AFTER - 1
    assert store.sweep() == []
    clock.t += 2
    assert [j["id"] for j in store.sweep()] == [job["id"]]
    assert store.job(job["id"])["state"] == "lost"
    # and a Mac still holding it is told to kill it
    assert online(store, node["node_id"], running=[job["id"]])[1] == [job["id"]]


def test_settled_jobs_are_purged_after_keep_days(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    store.record_events(node["node_id"], job["id"], [
        {"seq": 1, "type": "stdout", "data": "x"},
        {"seq": 2, "type": "result", "data": {"state": "done", "exit": 0}}])
    clock.t += mac_nodes.KEEP_DAYS * 86400 + 1
    store.sweep()
    assert list((store.root / "jobs").iterdir()) == []


# ── cancel ───────────────────────────────────────────────────────────────────


def test_cancel_reaches_the_mac_through_poll_and_events(store):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    with pytest.raises(mac_nodes.MacError) as err:
        store.request_cancel(job["id"], "nova")
    assert err.value.reason == "not_your_job"
    store.request_cancel(job["id"], "atlas")
    assert store.record_events(node["node_id"], job["id"], []) is True
    assert online(store, node["node_id"], running=[job["id"]])[1] == [job["id"]]


def test_cancelling_a_queued_job_settles_it_and_it_is_never_delivered(store):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    assert store.request_cancel(job["id"], "atlas")["state"] == "cancelled"
    assert online(store, node["node_id"]) == ([], [])


# ── S-7: output caps ─────────────────────────────────────────────────────────


def test_two_mib_of_stdout_keeps_at_most_one_mib_and_counts_the_rest(store):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    chunk = mac_nodes.CHUNK_MAX
    total = 2 << 20
    events, seq, sent = [], 0, 0
    while sent < total:
        seq += 1
        fill = "HEAD" if sent == 0 else ("TAIL" if sent + chunk >= total else "mid.")
        events.append({"seq": seq, "type": "stdout",
                       "data": (fill * (chunk // 4))[:min(chunk, total - sent)]})
        sent += min(chunk, total - sent)
    for i in range(0, len(events), 8):
        store.record_events(node["node_id"], job["id"], events[i:i + 8])

    stored = (store.root / "jobs" / f"{job['id']}.stdout").stat().st_size
    assert stored <= mac_nodes.OUT_KEEP
    meta = store.job(job["id"])["out"]["stdout"]
    assert meta["total"] == total and meta["dropped"] == total - stored
    text, nxt, truncated = store.output(job["id"], "stdout", 0)
    assert nxt == total and truncated is True
    assert text.startswith("HEAD") and text.endswith("TAIL")
    # a reader already past the gap is not told it was truncated
    tail, nxt, truncated = store.output(job["id"], "stdout", total - 4)
    assert (tail, nxt, truncated) == ("TAIL", total, False)


def test_an_oversized_chunk_is_refused(store):
    node = pair(store)
    online(store, node["node_id"])
    job = run_job(store)
    online(store, node["node_id"])
    with pytest.raises(mac_nodes.MacError) as err:
        store.record_events(node["node_id"], job["id"], [
            {"seq": 1, "type": "stdout", "data": "x" * (mac_nodes.CHUNK_MAX + 1)}])
    assert err.value.reason == "bad_input"


# ── S-8: limits ──────────────────────────────────────────────────────────────


def test_a_desks_third_concurrent_job_waits(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    ids = [run_job(store)["id"] for _ in range(3)]
    other = run_job(store, desk="nova")["id"]
    jobs, _ = online(store, node["node_id"])
    assert [j["id"] for j in jobs] == [ids[0], ids[1], other]
    assert store.job(ids[2])["state"] == "queued"
    clock.t += mac_nodes.CLAIM_WAIT + 1
    store.sweep()
    assert store.job(ids[2])["state"] == "queued", \
        "a job held back by the desk limit is waiting, not unanswered"
    store.record_events(node["node_id"], ids[0], [
        {"seq": 1, "type": "result", "data": {"state": "done", "exit": 0}}])
    jobs, _ = online(store, node["node_id"])
    assert [j["id"] for j in jobs] == [ids[2]]


def test_a_mac_never_gets_more_than_four_or_free_slots(store):
    node = pair(store)
    online(store, node["node_id"])
    for desk in ("a", "b", "c"):
        run_job(store, desk=desk)
        run_job(store, desk=desk)
    assert len(online(store, node["node_id"], free=1)[0]) == 1
    assert len(online(store, node["node_id"], free=9)[0]) == 3


def test_the_61st_job_in_a_minute_is_rate_limited(store, clock):
    node = pair(store)
    online(store, node["node_id"])
    for _ in range(mac_nodes.PER_DESK_PER_MIN):
        store.request_cancel(run_job(store)["id"], "atlas")
    with pytest.raises(mac_nodes.MacError) as err:
        run_job(store)
    assert (err.value.reason, err.value.status) == ("rate_limited", 429)
    run_job(store, desk="nova")           # per desk, not per deck
    clock.t += 61
    online(store, node["node_id"], free=0)
    run_job(store)


def test_the_33rd_queued_job_on_one_mac_is_node_busy(store):
    node = pair(store)
    online(store, node["node_id"])
    for i in range(mac_nodes.QUEUE_MAX_PER_NODE):
        run_job(store, desk=f"d{i}")
    with pytest.raises(mac_nodes.MacError) as err:
        run_job(store, desk="late")
    assert (err.value.reason, err.value.status) == ("node_busy", 429)


@pytest.mark.parametrize("kind,args,reason", [
    ("run", {"command": ""}, "bad_input"),
    ("run", {"command": "x" * 16385}, "bad_input"),
    ("run", {"command": "ls", "cwd": "relative/dir"}, "bad_path"),
    ("run", {"command": "ls", "env": {f"K{i}": "v" for i in range(33)}}, "bad_input"),
    ("read", {"path": "/a", "length": 0}, "bad_input"),
    ("read", {"path": "/a", "length": 64001}, "bad_input"),
    ("read", {"path": "/a", "offset": -1}, "bad_input"),
    ("write", {"path": "/a", "content": "x" * ((5 << 20) + 1)}, "too_large"),
    ("write", {"path": "/a", "content": "x", "mode": "clobber"}, "bad_input"),
    ("launch", {}, "bad_input"),
])
def test_job_arguments_are_checked_against_the_wire_contract(store, kind, args,
                                                              reason):
    node = pair(store)
    online(store, node["node_id"])
    with pytest.raises(mac_nodes.MacError) as err:
        store.enqueue("atlas", kind, args, mac=None)
    assert err.value.reason == reason


def test_timeout_is_one_to_six_hundred_seconds(store):
    node = pair(store)
    online(store, node["node_id"])
    for bad in (0, 601, "120", True):
        with pytest.raises(mac_nodes.MacError):
            run_job(store, timeout_s=bad)
    assert run_job(store, timeout_s=600)["timeout_s"] == 600


# ── which Mac ────────────────────────────────────────────────────────────────


def test_node_selection_follows_mb2(store, clock):
    with pytest.raises(mac_nodes.MacError) as err:
        store.resolve_node(None)
    assert err.value.reason == "no_mac"
    a = pair(store, "m-1", "Laptop")
    b = pair(store, "m-2", "Studio")
    # none online: the primary, so the offline answer can name it
    assert store.resolve_node(None)["node_id"] == a["node_id"]
    online(store, b["node_id"])
    assert store.resolve_node(None)["node_id"] == b["node_id"]   # the single online
    online(store, a["node_id"])
    assert store.resolve_node(None)["node_id"] == a["node_id"]   # several: primary
    assert store.resolve_node("studio")["node_id"] == b["node_id"]
    assert store.resolve_node(b["node_id"])["node_id"] == b["node_id"]
    with pytest.raises(mac_nodes.MacError) as err:
        store.resolve_node("iMac")
    assert err.value.reason == "unknown_mac"
    pair(store, "m-3", "studio")
    with pytest.raises(mac_nodes.MacError) as err:
        store.resolve_node("Studio")
    assert err.value.reason == "ambiguous_mac"


def test_making_one_primary_clears_the_others_and_forgetting_refuses_its_jobs(
        store):
    a = pair(store, "m-1", "A")
    b = pair(store, "m-2", "B")
    store.update_node(b["node_id"], primary=True)
    assert [n["primary"] for n in store.nodes()] == [False, True]
    online(store, b["node_id"])
    job = run_job(store, mac="B")
    changed = store.forget(b["node_id"])
    assert [j["id"] for j in changed] == [job["id"]]
    row = store.job(job["id"])
    assert (row["state"], row["reason"]) == ("refused", "unknown_mac")
    assert store.nodes()[0]["node_id"] == a["node_id"]
    assert store.nodes()[0]["primary"] is True


# ── two writers ──────────────────────────────────────────────────────────────

_WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from server import mac_nodes
store = mac_nodes.Store(sys.argv[2])
node_id, desk = sys.argv[3], sys.argv[4]
for i in range(8):
    store.enqueue(desk, "run", {"command": f"echo {i}"}, mac=node_id)
    store.set_offline_card(node_id, f"{desk}-{i}", f"card-{desk}-{i}")
"""


def test_parallel_writers_from_other_processes_lose_nothing(tmp_path):
    assert mac_nodes is not None, "server/mac_nodes.py does not exist"
    store = mac_nodes.Store(tmp_path / "mac")
    node = pair(store)
    online(store, node["node_id"], free=0)
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(REPO),
                               str(store.root), node["node_id"], f"desk{p}"])
             for p in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    jobs = [json.loads(p.read_text())
            for p in (store.root / "jobs").glob("mj_*.json")]
    assert len(jobs) == 32 and len({j["id"] for j in jobs}) == 32
    cards = json.loads(store.nodes_path.read_text())["nodes"][node["node_id"]][
        "offline_cards"]
    assert len(cards) == 32, "a read-modify-write of nodes.json was lost"
    assert not [p for p in store.root.rglob("*.tmp")], "a temp file was left"
