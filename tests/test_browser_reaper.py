"""Idle desk browsers are stopped, woken on demand, and capped.

MEASURED on the box, 2026-10-01: 13 desk Chromium containers (240-800 MB
each, ~5.8 GB together) on a 7.9 GB, 4-core box. Swap was full, kswapd ate a
core, load hit 48, and one screen frame took 15-24 s -- a no-op
`docker exec true` alone took 5-10 s. Most of those desks were ASLEEP.

So `server/browser_reaper.py` decides, every sweep, which running desk
browsers are doing nothing for anyone and stops them. The home directory is
a bind mount (`sandbox.home_for`), so a stopped browser keeps its logins;
the next computer tool or screen view starts it again.

Hermetic: no Docker. `plan` is pure, and the side-effecting parts are
exercised through `sandbox._run`, the one function that runs `docker`.
"""

from types import SimpleNamespace

import pytest

from server import browser_reaper as reaper
from server import sandbox

NOW = 1_000_000.0
IDLE = 15 * 60


@pytest.fixture(autouse=True)
def _bus(tmp_path, monkeypatch):
    """Every file the reaper writes goes to tmp_path, never the bus."""
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(reaper, "MEMORY_PATH", tmp_path / "memory.json")


def _plan(states, running, last_use, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("idle_seconds", IDLE)
    kw.setdefault("max_live", 5)
    return reaper.plan(states, running, last_use, **kw)


# ── who gets stopped ────────────────────────────────────────────────────────


def test_an_asleep_desk_browser_is_stopped():
    stop = _plan({"pine": "ASLEEP"}, ["pine"], {"pine": NOW - 600})
    assert stop == ["pine"]


def test_an_offline_desk_browser_is_stopped():
    assert _plan({"x": "OFFLINE"}, ["x"], {"x": NOW - 600}) == ["x"]


def test_a_sleeping_desk_he_is_looking_at_right_now_is_kept():
    """The screen view touches the desk on every frame. Stopping a browser
    under the owner's eyes because the desk itself is asleep would blank the
    picture he opened on purpose."""
    assert _plan({"pine": "ASLEEP"}, ["pine"], {"pine": NOW - 5}) == []


def test_an_idle_desk_is_kept_until_the_idle_window_passes():
    assert _plan({"a": "IDLE"}, ["a"], {"a": NOW - IDLE + 30}) == []
    assert _plan({"a": "IDLE"}, ["a"], {"a": NOW - IDLE - 1}) == ["a"]


def test_a_working_desk_is_never_stopped_even_past_the_cap():
    states = {f"w{i}": "WORKING" for i in range(7)}
    running = sorted(states)
    last = {d: NOW - 10 * IDLE for d in running}
    assert _plan(states, running, last, max_live=3) == []


def test_a_container_for_a_desk_not_on_the_roster_is_not_ours_to_judge():
    assert _plan({}, ["ghost"], {"ghost": NOW - 10 * IDLE}) == []


def test_past_the_cap_the_least_recently_used_idle_desk_goes_first():
    states = {"a": "IDLE", "b": "IDLE", "c": "IDLE", "d": "WORKING"}
    running = ["a", "b", "c", "d"]
    # all used inside the idle window, so only the cap can stop anyone
    last = {"a": NOW - 300, "b": NOW - 600, "c": NOW - 200, "d": NOW - 900}
    assert _plan(states, running, last, max_live=2) == ["b", "a"]


def test_the_cap_does_not_take_a_browser_used_in_the_last_two_minutes():
    states = {"a": "IDLE", "b": "IDLE"}
    last = {"a": NOW - 30, "b": NOW - 40}
    assert _plan(states, ["a", "b"], last, max_live=1) == []


def test_low_memory_stops_one_more_idle_browser_than_the_cap_alone():
    states = {"a": "IDLE", "b": "IDLE"}
    last = {"a": NOW - 300, "b": NOW - 600}
    assert _plan(states, ["a", "b"], last, max_live=5) == []
    assert _plan(states, ["a", "b"], last, max_live=5,
                 low_memory=True) == ["b"]


# ── configuration ───────────────────────────────────────────────────────────


def test_defaults_are_fifteen_minutes_and_three_browsers():
    assert reaper.settings({}) == (15 * 60, 3)


def test_both_knobs_are_configurable_and_garbage_falls_back():
    assert reaper.settings({"DECK_BROWSER_IDLE_MINUTES": "3",
                            "DECK_BROWSER_MAX_LIVE": "2"}) == (180, 2)
    assert reaper.settings({"DECK_BROWSER_IDLE_MINUTES": "soon",
                            "DECK_BROWSER_MAX_LIVE": "-4"}) == (15 * 60, 3)


# ── last use crosses processes ──────────────────────────────────────────────


def test_last_use_is_a_file_so_the_computer_tool_process_can_mark_it(tmp_path):
    """`computer_mcp` runs in the desk's own process, the reaper in the
    daemon: the only thing they share is the disk."""
    reaper.touch("atlas", root=tmp_path, now=NOW - 42)
    assert reaper.last_uses(["atlas", "never"], root=tmp_path) == {
        "atlas": NOW - 42}


def test_a_hostile_desk_name_never_becomes_a_path(tmp_path):
    with pytest.raises(ValueError):
        reaper.touch("../../etc", root=tmp_path)


# ── the side effects ────────────────────────────────────────────────────────


class _Docker:
    def __init__(self, names=()):
        self.names = list(names)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=None):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            out = "\n".join(self.names).encode()
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def test_running_desks_are_read_from_docker_by_container_prefix(monkeypatch):
    docker = _Docker(["deck-desk-atlas", "ntfy", "deck-desk-pine-growth"])
    monkeypatch.setattr(sandbox, "_run", docker)
    assert reaper.running_desks() == ["atlas", "pine-growth"]


def test_retire_closes_chromium_gracefully_before_removing_the_container(
        monkeypatch):
    """`docker rm --force` alone SIGKILLs Chromium, and cookies written in
    the last ~30 s are still in memory. TERM first so the profile -- the
    logins -- is flushed to the persisted home, then remove."""
    docker = _Docker()
    monkeypatch.setattr(sandbox, "_run", docker)
    reaper.retire("atlas")
    graceful, removed = docker.calls
    assert graceful[:2] == ["docker", "exec"]
    assert "deck-desk-atlas" in graceful
    assert "kill -TERM" in graceful[-1]
    assert removed == ["docker", "rm", "--force", "deck-desk-atlas"]


def test_sweep_stops_what_the_plan_says_and_records_states(
        monkeypatch, tmp_path):
    docker = _Docker(["deck-desk-pine", "deck-desk-eng"])
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(reaper, "available_fraction", lambda: 0.5)
    reaper.touch("pine", root=tmp_path / "use", now=NOW - 600)
    reaper.touch("eng", root=tmp_path / "use", now=NOW - 600)
    stopped = reaper.sweep({"pine": "ASLEEP", "eng": "WORKING"},
                           now=NOW, env={})
    assert stopped == ["pine"]
    assert ["docker", "rm", "--force", "deck-desk-pine"] in docker.calls
    assert ["docker", "rm", "--force", "deck-desk-eng"] not in docker.calls
    assert reaper.read_states(tmp_path / "states.json", now=NOW) == {
        "pine": "ASLEEP", "eng": "WORKING"}


def test_a_desk_seen_for_the_first_time_gets_a_full_window(
        monkeypatch, tmp_path):
    """After a deploy no desk has a last-use file yet. Treating that as
    "never used" would stop every IDLE browser on the first sweep,
    including one a desk used a minute before the restart."""
    docker = _Docker(["deck-desk-a"])
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(reaper, "available_fraction", lambda: 0.5)
    monkeypatch.setattr(reaper, "_first_seen", {})
    assert reaper.sweep({"a": "IDLE"}, now=NOW, env={}) == []
    assert reaper.sweep({"a": "IDLE"}, now=NOW + IDLE + 1, env={}) == ["a"]


def test_make_room_stops_the_lru_idle_browser_before_a_new_one_starts(
        monkeypatch, tmp_path):
    names = [f"deck-desk-d{i}" for i in range(5)]
    docker = _Docker(names)
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    for i in range(5):
        reaper.touch(f"d{i}", root=tmp_path / "use", now=NOW - 300 - i)
    reaper.write_states({f"d{i}": "IDLE" for i in range(5)},
                        tmp_path / "states.json", now=NOW)
    stopped = reaper.make_room("new", now=NOW, env={"DECK_BROWSER_MAX_LIVE": "5"})
    assert stopped == ["d4"]


def test_make_room_without_fresh_states_stops_nobody_and_refuses(
        monkeypatch, tmp_path):
    """With no idea who is WORKING, stopping anyone could kill a desk's page
    mid-task -- and over the cap "by one" is how nine started at 19:24 on
    2026-10-01. So nobody is stopped and the new browser is refused."""
    docker = _Docker([f"deck-desk-d{i}" for i in range(5)])
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "missing.json")
    with pytest.raises(sandbox.SandboxError) as caught:
        reaper.make_room("new", now=NOW, env={})
    assert caught.value.reason == "browsers_full"
    assert not [c for c in docker.calls if c[:2] == ["docker", "exec"]]


# ── memory ──────────────────────────────────────────────────────────────────


MEMINFO = """MemTotal:        8131788 kB
MemFree:          111000 kB
MemAvailable:     236000 kB
"""


def test_available_fraction_reads_meminfo(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO)
    assert reaper.available_fraction(path) == pytest.approx(236000 / 8131788)


def test_low_memory_is_remembered_from_when_it_started(tmp_path):
    path = tmp_path / "memory.json"
    reaper.record_memory(0.05, path, now=NOW)
    reaper.record_memory(0.04, path, now=NOW + 200)
    assert reaper.low_memory_since(path) == NOW
    reaper.record_memory(0.30, path, now=NOW + 400)
    assert reaper.low_memory_since(path) is None


# ── waking ──────────────────────────────────────────────────────────────────


def test_wake_starts_one_background_start_per_desk_not_one_per_poll(
        monkeypatch):
    started = []
    gate = __import__("threading").Event()

    def slow_ensure(desk):
        started.append(desk)
        gate.wait(2)

    monkeypatch.setattr(reaper, "_ensure", slow_ensure)
    monkeypatch.setattr(reaper, "_waking", set())
    assert reaper.wake("atlas") is True
    assert reaper.wake("atlas") is False   # already on its way
    assert reaper.is_waking("atlas")
    gate.set()
    reaper._threads["atlas"].join(2)
    assert started == ["atlas"]
    assert not reaper.is_waking("atlas")


# ── the cap a desk's process obeys is the daemon's ──────────────────────────


def test_make_room_obeys_the_cap_the_daemon_published_not_its_own_env(
        monkeypatch, tmp_path):
    """MEASURED on the box, 2026-10-02: desks run under the claude daemon,
    not the deck's unit, so their computer tools keep the environment they
    were spawned with. Raising `DECK_BROWSER_MAX_LIVE` and restarting the
    deck changed the sweep's cap and nobody's `make_room`: music-growth was
    still refused at three. The daemon writes its cap beside the states and
    `make_room` in every process reads it from there."""
    docker = _Docker([f"deck-desk-d{i}" for i in range(3)])
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "available_fraction", lambda: 0.5)
    monkeypatch.setattr(reaper, "_first_seen", {})
    monkeypatch.setattr(reaper, "chief_name", lambda: "atlas")
    monkeypatch.setattr(reaper, "VIEW_DIR", tmp_path / "viewing")
    for i in range(3):
        reaper.touch(f"d{i}", root=tmp_path / "use", now=NOW - 30)
    reaper.sweep({f"d{i}": "WORKING" for i in range(3)} | {"new": "IDLE"},
                 now=NOW, env={"DECK_BROWSER_MAX_LIVE": "6"})
    # the desk's own process still carries the old value
    assert reaper.make_room("new", now=NOW,
                            env={"DECK_BROWSER_MAX_LIVE": "3"}) == []


def test_without_a_published_cap_the_process_env_still_decides(
        monkeypatch, tmp_path):
    docker = _Docker([f"deck-desk-d{i}" for i in range(3)])
    monkeypatch.setattr(sandbox, "_run", docker)
    monkeypatch.setattr(reaper, "chief_name", lambda: "atlas")
    monkeypatch.setattr(reaper, "VIEW_DIR", tmp_path / "viewing")
    reaper.write_states({f"d{i}": "WORKING" for i in range(3)},
                        tmp_path / "states.json", now=NOW)
    with pytest.raises(sandbox.SandboxError):
        reaper.make_room("new", now=NOW, env={"DECK_BROWSER_MAX_LIVE": "3"})
    assert reaper.make_room("new", now=NOW,
                            env={"DECK_BROWSER_MAX_LIVE": "4"}) == []
