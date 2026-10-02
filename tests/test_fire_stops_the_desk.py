"""Firing a desk must stop its session and take its computer away.

MEASURED defect: `DELETE /v1/agents/{name}` removed the roster row and nothing
else. The desk's `claude` background job kept running (and counted against the
live-desk cap) and its `deck-desk-<name>` container kept ~670 MiB.

Hermetic: `spawn.stop_job` and `sandbox._run` are the seams, so no `claude` or
`docker` is ever executed.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, sandbox, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"


def _desk(name):
    return {"name": name, "cwd": "/tmp/" + name, "engine": "claude",
            "mission": "m", "label": "L", "charter": "c", "reports_to": None}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(office, "OFFICE_FILE", tmp_path / "office.json")
    (tmp_path / "messages.jsonl").write_text("")
    # sess-live is on the board for `doomed`; jobs: one live, one stopped
    # earlier, one asleep-but-not-marked-stopped, and one for another desk.
    (tmp_path / "office.json").write_text(json.dumps({
        "generated_at": 0.0,
        "sessions": {"sess-live": {"name": "doomed", "live": True}}}))
    monkeypatch.setattr(office, "live_session_ids",
                        lambda n: {"sess-live"} if n == "doomed" else set())
    jobs = tmp_path / "jobs"
    for short, name, sid, state in [
            ("aaaa1111", "doomed", "sess-live", "idle"),
            ("bbbb2222", "doomed", "sess-old", "stopped"),
            ("cccc3333", "doomed", "sess-asleep", "idle"),
            ("dddd4444", "keeper", "sess-keeper", "idle")]:
        (jobs / short).mkdir(parents=True)
        (jobs / short / "state.json").write_text(json.dumps({
            "name": name, "sessionId": sid, "daemonShort": short,
            "state": state, "cwd": "/tmp/" + name}))
    monkeypatch.setattr(spawn, "JOBS_DIR", jobs)

    stopped, docker = [], []
    monkeypatch.setattr(spawn, "stop_job",
                        lambda j: stopped.append(j) or True)

    def fake_run(argv, *, timeout=None):
        docker.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sandbox, "_run", fake_run)

    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        _desk("doomed"), _desk("keeper")]}))
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=tmp_path / "agent_prefs.json", asks_path=tmp_path / "asks.json",
        rules_path=tmp_path / "autoreview.json",
        routines_path=tmp_path / "routines.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return SimpleNamespace(client=TestClient(app), stopped=stopped,
                           docker=docker)


HDR = {"Authorization": f"Bearer {TOKEN}"}


def test_delete_stops_only_that_desks_jobs(world):
    r = world.client.delete("/v1/agents/doomed", headers=HDR)
    assert r.status_code == 200
    assert sorted(world.stopped) == ["aaaa1111", "cccc3333"], world.stopped


def test_delete_removes_only_that_desks_container(world):
    world.client.delete("/v1/agents/doomed", headers=HDR)
    removes = [a for a in world.docker if a[:2] == ["docker", "rm"]]
    assert removes == [["docker", "rm", "--force", "deck-desk-doomed"]]
    assert not any("keeper" in " ".join(a) for a in world.docker)


def test_refused_fire_stops_nothing(world):
    r = world.client.delete("/v1/agents/nope", headers=HDR)
    assert r.status_code == 404
    assert world.stopped == [] and world.docker == []
