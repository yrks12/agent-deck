"""Orphan containers never hold a slot; Atlas and the desk he is watching
are never refused.

MEASURED on the box, 2026-10-01: a test container `deck-desk-scrtest` was on
no roster. It counted against the cap of three, the reaper never judged it
(`plan` leaves what is not on the roster alone), and Atlas, who needed a
browser, was answered `browsers_full` by a slot nobody could use or free.

Hermetic: no Docker, `sandbox._run` is the one door.
"""

from types import SimpleNamespace

import pytest

from server import browser_reaper as reaper
from server import sandbox

NOW = 1_000_000.0
CAP3 = {"DECK_BROWSER_MAX_LIVE": "3"}


class _Docker:
    def __init__(self, names=()):
        self.names = list(names)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=None):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            out = "\n".join(f"deck-desk-{n}" for n in self.names).encode()
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def removed(self):
        return [c[-1][len("deck-desk-"):] for c in self.calls
                if c[:3] == ["docker", "rm", "--force"]]


@pytest.fixture(autouse=True)
def _bus(tmp_path, monkeypatch):
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "VIEW_DIR", tmp_path / "view", raising=False)
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(reaper, "MEMORY_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(reaper, "available_fraction", lambda: 0.5)
    monkeypatch.setattr(reaper, "_first_seen", {})
    monkeypatch.setattr(reaper, "chief_name", lambda: "atlas", raising=False)


def _world(monkeypatch, tmp_path, running, states, used=None):
    docker = _Docker(running)
    monkeypatch.setattr(sandbox, "_run", docker)
    reaper.write_states(states, tmp_path / "states.json", now=NOW)
    for desk, age in (used or {}).items():
        reaper.touch(desk, root=tmp_path / "use", now=NOW - age)
    return docker


# ── 1. orphans ──────────────────────────────────────────────────────────────


def test_a_container_on_no_roster_does_not_hold_a_slot(monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["scrtest", "a", "b"],
                    {"a": "WORKING", "b": "WORKING", "c": "IDLE"})
    assert reaper.make_room("c", now=NOW, env=CAP3) == []
    assert docker.removed() == []


def test_roster_desks_still_fill_the_cap(monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path, ["scrtest", "a", "b", "d"],
           {"a": "WORKING", "b": "WORKING", "d": "WORKING", "c": "IDLE"})
    with pytest.raises(sandbox.SandboxError) as caught:
        reaper.make_room("c", now=NOW, env=CAP3)
    assert caught.value.reason == "browsers_full"


def test_an_orphan_idle_for_ten_minutes_is_swept(monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["scrtest", "a"], {"a": "WORKING"},
                    used={"scrtest": 11 * 60})
    assert reaper.sweep({"a": "WORKING"}, now=NOW, env={}) == ["scrtest"]
    assert docker.removed() == ["scrtest"]


def test_a_young_orphan_is_left_alone(monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["scrtest"], {"a": "WORKING"},
                    used={"scrtest": 5 * 60})
    assert reaper.sweep({"a": "WORKING"}, now=NOW, env={}) == []
    assert docker.removed() == []


# ── 2. Atlas and the desk he is watching ────────────────────────────────────

BUSY_RECENT = {"a": 30, "b": 20, "c": 10}   # all used inside the grace window


def test_an_ordinary_desk_is_refused_when_every_slot_was_just_used(
        monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path, ["a", "b", "c"],
           {"a": "IDLE", "b": "IDLE", "c": "IDLE", "new": "IDLE"}, BUSY_RECENT)
    with pytest.raises(sandbox.SandboxError):
        reaper.make_room("new", now=NOW, env=CAP3)


def test_atlas_evicts_the_least_recently_used_idle_browser(
        monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["a", "b", "c"],
                    {"a": "IDLE", "b": "IDLE", "c": "IDLE", "atlas": "IDLE"},
                    BUSY_RECENT)
    assert reaper.make_room("atlas", now=NOW, env=CAP3) == ["a"]
    assert docker.removed() == ["a"]


def test_the_desk_he_is_watching_gets_the_same_priority(monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["a", "b", "c"],
                    {"a": "IDLE", "b": "IDLE", "c": "IDLE", "pine": "ASLEEP"},
                    BUSY_RECENT)
    reaper.touch("pine", root=tmp_path / "use", now=NOW - 2, view=True)
    reaper.make_room("pine", now=NOW, env=CAP3)
    assert docker.removed() == ["a"]


def test_a_view_that_ended_long_ago_is_no_priority(monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path, ["a", "b", "c"],
           {"a": "IDLE", "b": "IDLE", "c": "IDLE", "pine": "IDLE"}, BUSY_RECENT)
    reaper.touch("pine", root=tmp_path / "use", now=NOW - 600, view=True)
    with pytest.raises(sandbox.SandboxError):
        reaper.make_room("pine", now=NOW, env=CAP3)


def test_atlas_queues_while_every_slot_is_mid_action_then_gets_one(
        monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["a", "b", "c"],
                    {"a": "WORKING", "b": "WORKING", "c": "WORKING",
                     "atlas": "IDLE"}, {"a": 500, "b": 400, "c": 300})
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:   # b finishes its action
            reaper.write_states({"a": "WORKING", "b": "IDLE", "c": "WORKING",
                                 "atlas": "IDLE"}, tmp_path / "states.json",
                                now=NOW)

    reaper.make_room("atlas", now=NOW, env=CAP3, sleep=nap)
    assert len(naps) == 2
    assert docker.removed() == ["b"]


def test_atlas_is_never_refused_even_if_the_wait_runs_out(
        monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["a", "b", "c"],
                    {"a": "WORKING", "b": "WORKING", "c": "WORKING",
                     "atlas": "IDLE"}, {"a": 500, "b": 400, "c": 300})
    reaper.make_room("atlas", now=NOW, env=CAP3, sleep=lambda s: None)
    assert docker.removed() == ["a"]     # the least recently used goes


def test_atlas_never_evicts_the_desk_he_is_watching(monkeypatch, tmp_path):
    docker = _world(monkeypatch, tmp_path, ["a", "b", "pine"],
                    {"a": "WORKING", "b": "WORKING", "pine": "IDLE",
                     "atlas": "IDLE"}, {"a": 500, "b": 400, "pine": 900})
    reaper.touch("pine", root=tmp_path / "use", now=NOW - 3, view=True)
    reaper.make_room("atlas", now=NOW, env=CAP3, sleep=lambda s: None)
    assert docker.removed() == ["a"]
