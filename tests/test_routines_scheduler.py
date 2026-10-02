"""Routines over HTTP, and the scheduler that actually fires them.

`server/routines.py` is pure cron maths and file writes; none of it runs unless
something ticks. These tests pin the tick.

The load-bearing one is `test_a_delivery_that_raises_is_still_advanced`. If the
scheduler advances *after* delivering, a routine whose delivery throws stays due
forever and re-fires on every tick -- one message per tick into a session that
is trying to work. Advancing first is what makes a failure cost one delivery
instead of thousands.

Hermetic: routines file, message queue and bus all live in tmp_path.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import office
from server import onboard
from server import routines as routines_mod

CRON = {"kind": "cron", "spec": "0 8 * * 1-5", "tz": "Europe/London"}


@pytest.fixture
def routines_file(tmp_path, monkeypatch):
    path = tmp_path / "routines.json"
    monkeypatch.setattr(app_mod, "ROUTINES_PATH", path)
    return path


@pytest.fixture
def bus(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(app_mod, "BUS_FILE", path)
    return path


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """The office message queue, pointed away from the real bus directory."""
    messages = tmp_path / "messages.jsonl"
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "MESSAGES_FILE", messages)
    return messages


@pytest.fixture
def client(routines_file, bus, queue):
    return TestClient(app_mod.app)


def write_routines(path, entries):
    path.write_text(json.dumps({"version": 1, "routines": entries}))


def a_due_routine(**over):
    entry = {
        "id": "rt1",
        "agent": "acme-growth",
        "prompt": "Check yesterday's spend.",
        "trigger": dict(CRON),
        "next_run_at": time.time() - 1,
        "enabled": True,
        "runs": [],
    }
    entry.update(over)
    return entry


def lines(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def fire(now=None):
    return asyncio.run(app_mod._fire_due_routines(time.time() if now is None else now))


# --- the routes -------------------------------------------------------------


def test_posting_a_cron_routine_stores_it_with_its_next_fire_time(
    client, routines_file
):
    res = client.post(
        "/api/routines",
        json={"id": "rt1", "agent": "acme-growth",
              "prompt": "Check yesterday's spend.", "trigger": CRON},
    )

    assert res.status_code == 200
    assert res.json()["ok"] is True
    stored = routines_mod.load_routines(routines_file)
    assert [r.id for r in stored] == ["rt1"]
    assert stored[0].next_run_at is not None
    assert stored[0].next_run_at > time.time(), "a new routine must be scheduled ahead"
    assert stored[0].next_run_at == routines_mod.next_fire(
        CRON["spec"], CRON["tz"], stored[0].next_run_at - 1
    )


def test_get_lists_routines_with_their_run_history(client, routines_file):
    write_routines(routines_file, [a_due_routine(runs=[{"ts": 1.0, "ok": True,
                                                        "detail": "delivered"}])])

    body = client.get("/api/routines").json()

    assert [r["id"] for r in body["routines"]] == ["rt1"]
    assert body["routines"][0]["agent"] == "acme-growth"
    assert body["routines"][0]["runs"] == [{"ts": 1.0, "ok": True,
                                            "detail": "delivered"}]


def test_a_malformed_cron_spec_is_refused_and_nothing_is_stored(
    client, routines_file
):
    res = client.post(
        "/api/routines",
        json={"id": "bad", "agent": "a", "prompt": "p",
              "trigger": {"kind": "cron", "spec": "not a cron", "tz": "UTC"}},
    )

    assert res.status_code == 400
    assert routines_mod.load_routines(routines_file) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"agent": "a", "prompt": "p", "trigger": CRON},
        {"id": "x", "prompt": "p", "trigger": CRON},
        {"id": "x", "agent": "a", "trigger": CRON},
        {"id": "x", "agent": "a", "prompt": "p"},
    ],
)
def test_an_incomplete_routine_is_refused(client, routines_file, payload):
    assert client.post("/api/routines", json=payload).status_code == 400
    assert routines_mod.load_routines(routines_file) == []


def test_deleting_a_routine_removes_it(client, routines_file):
    write_routines(routines_file, [a_due_routine()])

    assert client.delete("/api/routines/rt1").status_code == 200
    assert routines_mod.load_routines(routines_file) == []


def test_deleting_a_routine_that_was_never_there_is_a_404(client, routines_file):
    assert client.delete("/api/routines/ghost").status_code == 404


# --- the scheduler ----------------------------------------------------------


def test_a_due_routine_is_delivered_to_its_agent(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine()])
    injected = []
    monkeypatch.setattr(
        app_mod, "_try_inject", lambda to, text: injected.append((to, text)) or True
    )

    fire()

    assert injected == [("acme-growth",
                         office.attribute("Check yesterday's spend.", "routine"))]
    recs = lines(queue)
    queued = [r for r in recs if r.get("id")]
    acked = {r["ack"] for r in recs if r.get("ack")}
    assert len(queued) == 1
    assert queued[0]["to"] == "acme-growth"
    assert queued[0]["id"] in acked, "an injected prompt must not be delivered twice"


def test_an_offline_agent_keeps_the_prompt_queued_for_its_next_turn(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine()])
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: False)

    fire()

    assert office.pending_counts().get("acme-growth") == 1


def test_firing_advances_the_routine_so_it_does_not_fire_again(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine()])
    calls = []
    monkeypatch.setattr(
        app_mod, "_try_inject", lambda to, text: calls.append(to) or True
    )

    now = time.time()
    fire(now)
    fire(now)

    assert len(calls) == 1, "the routine fired twice in the same second"
    assert routines_mod.load_routines(routines_file)[0].next_run_at > now


def test_a_delivery_that_raises_is_still_advanced(
    routines_file, bus, queue, monkeypatch
):
    """The flood detector.

    Advancing after delivery leaves a throwing routine due forever: every tick
    re-fires it, one message per tick, into a session trying to work. This is
    the test that fails the moment the order is inverted.
    """
    write_routines(routines_file, [a_due_routine()])
    attempts = []

    def boom(routine):
        attempts.append(routine.id)
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(app_mod, "_deliver_routine", boom)

    now = time.time()
    fire(now)          # must not raise: one bad routine cannot kill the loop
    fire(now)

    assert attempts == ["rt1"], f"delivery was retried {len(attempts)} times"
    assert routines_mod.load_routines(routines_file)[0].next_run_at > now
    assert lines(bus)[0]["ok"] is False
    assert routines_mod.runs(routines_file, "rt1")[0]["ok"] is False


def test_every_firing_lands_on_the_bus_in_the_hook_line_shape(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine()])
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: True)

    fire()

    written = lines(bus)
    assert len(written) == 1
    line = written[0]
    assert set(line) == {"ts", "event", "id", "agent", "ok"}
    assert line["event"] == "routine"
    assert line["id"] == "rt1"
    assert line["agent"] == "acme-growth"
    assert line["ok"] is True
    assert abs(line["ts"] - time.time()) < 60


def test_the_run_is_recorded_in_the_routines_history(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine()])
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: True)

    fire()

    history = routines_mod.runs(routines_file, "rt1")
    assert len(history) == 1
    assert history[0]["ok"] is True


def test_a_disabled_routine_never_fires(routines_file, bus, queue, monkeypatch):
    write_routines(routines_file, [a_due_routine(enabled=False)])
    calls = []
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: calls.append(to))

    fire()

    assert calls == []
    assert lines(bus) == []


def test_a_routine_that_is_not_due_yet_does_not_fire(
    routines_file, bus, queue, monkeypatch
):
    write_routines(routines_file, [a_due_routine(next_run_at=time.time() + 3600)])
    calls = []
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: calls.append(to))

    fire()

    assert calls == []


# --- the wiring itself ------------------------------------------------------


def test_startup_runs_the_scheduler_alongside_the_collector_loop(monkeypatch):
    """Everything above is dead code if nothing ticks it."""
    started = []

    def capture(coro):
        started.append(coro.cr_code.co_name)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", capture)
    asyncio.run(app_mod._startup())

    assert "_loop" in started, "the collector loop stopped starting"
    assert "_routine_loop" in started, "nothing ever fires a routine"


# --- a routine aimed at a desk that renamed itself ---------------------------


@pytest.fixture
def roster_file(tmp_path, monkeypatch):
    """A roster in tmp_path, so the alias file the resolver reads is tmp too.
    `onboard.aliases_path` derives it from the roster's directory."""
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        {"name": "acme-growth", "cwd": "/tmp", "engine": "claude",
         "mission": "ads", "reports_to": None},
    ]}))
    monkeypatch.setattr(app_mod, "ROSTER_PATH", path)
    return path


def test_a_routine_fires_at_the_desks_current_name_after_it_renamed_itself(
    routines_file, bus, queue, roster_file, monkeypatch
):
    """A routine stores the agent name it was CREATED with. That desk naming
    itself used to mean the prompt was queued for a name nobody occupies --
    forever, every schedule, silently: `office.send` takes any string, so there
    is no error and no failed run in the history to look at.

    Resolution belongs at delivery time, not at write time, so a rename that
    happens long after the routine was written still lands. Asserts the GOOD
    signal: the injected recipient and the queued record's `to` are both the
    NEW name.
    """
    write_routines(routines_file, [a_due_routine()])
    onboard.apply_patch(roster_file, "acme-growth",
                        onboard.DeskPatch(name="growth", label="Growth",
                                          charter="Own the spend."))
    injected = []
    monkeypatch.setattr(
        app_mod, "_try_inject", lambda to, text: injected.append((to, text)) or True
    )

    fire()

    assert injected == [("growth",
                         office.attribute("Check yesterday's spend.", "routine"))]
    queued = [r for r in lines(queue) if r.get("id")]
    assert [r["to"] for r in queued] == ["growth"], \
        "the routine was queued for a name nobody occupies"


def test_an_offline_desk_that_renamed_itself_keeps_the_prompt_under_its_new_name(
    routines_file, bus, queue, roster_file, monkeypatch
):
    """The queue is what a routine relies on for a desk that is not up. The
    pending count has to accrue against the name the desk answers to now, or
    the office hook never hands it over."""
    write_routines(routines_file, [a_due_routine()])
    onboard.apply_patch(roster_file, "acme-growth",
                        onboard.DeskPatch(name="growth", label="Growth",
                                          charter="Own the spend."))
    monkeypatch.setattr(app_mod, "_try_inject", lambda to, text: False)

    fire()

    assert office.pending_counts() == {"growth": 1}


def test_a_routine_for_a_desk_that_never_renamed_goes_to_the_stored_name(
    routines_file, bus, queue, roster_file, monkeypatch
):
    """The no-op case: resolution must not perturb the ordinary delivery."""
    write_routines(routines_file, [a_due_routine()])
    injected = []
    monkeypatch.setattr(
        app_mod, "_try_inject", lambda to, text: injected.append((to, text)) or True
    )

    fire()

    assert injected == [("acme-growth",
                         office.attribute("Check yesterday's spend.", "routine"))]
