"""The roster over HTTP: desks that outlive the process sitting at them.

`server/roster.py` already knows how to join desks to sessions. These tests pin
the wiring: that the daemon reads the real roster file, joins it against the
live snapshot, writes edits back, and can put a process at a desk on request.

The signal we care about most is the OFFLINE one -- today a card disappears the
instant its Terminal tab closes, and a desk must not.

Nothing here spawns anything: BOTH launchers are replaced in every test that
reaches one, and the channel is injected rather than read off the host. Doubling
`spawn_terminal` alone was enough on the owner's Mac and was not enough on his
Linux box, where `spawn.start` picks the headless launcher and ran the real CLI.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import roster
from server import spawn

ACME = {
    "name": "acme-growth",
    "cwd": "/Users/samcarter/Projects/acme",
    "engine": "claude",
    "mission": "Own Acme's paid acquisition.",
    "model": "claude-opus-5",
}


@pytest.fixture
def roster_file(tmp_path, monkeypatch):
    path = tmp_path / "roster.json"
    monkeypatch.setattr(app_mod, "ROSTER_PATH", path)
    return path


@pytest.fixture
def client(roster_file):
    return TestClient(app_mod.app)


def seat(monkeypatch, sessions):
    monkeypatch.setattr(app_mod, "_state", {"sessions": sessions})


def write_roster(path, desks):
    path.write_text(json.dumps({"version": 1, "agents": desks}))


def by_name(rows, name):
    return next(r for r in rows if r["name"] == name)


# --- GET: the join ----------------------------------------------------------


def test_a_desk_with_nobody_at_it_is_reported_offline_not_dropped(
    client, roster_file, monkeypatch
):
    write_roster(roster_file, [ACME])
    seat(monkeypatch, [])

    rows = client.get("/api/roster").json()["desks"]

    acme = by_name(rows, "acme-growth")
    assert acme["state"] == "OFFLINE"
    assert acme["desk"] is True
    assert acme["session_id"] is None
    assert acme["mission"] == ACME["mission"]


def test_a_desk_shows_the_state_of_whoever_is_sitting_at_it(
    client, roster_file, monkeypatch
):
    write_roster(roster_file, [ACME])
    seat(
        monkeypatch,
        [{"session_id": "sid-p", "name": "acme-growth", "pid": 4242,
          "state": "WORKING"}],
    )

    acme = by_name(client.get("/api/roster").json()["desks"], "acme-growth")

    assert acme["state"] == "WORKING"
    assert acme["session_id"] == "sid-p"
    assert acme["pid"] == 4242


def test_a_live_session_with_no_desk_is_still_listed(client, roster_file, monkeypatch):
    write_roster(roster_file, [])
    seat(monkeypatch, [{"session_id": "sid-z", "name": "scratch", "pid": 9,
                        "state": "IDLE"}])

    rows = client.get("/api/roster").json()["desks"]

    assert by_name(rows, "scratch")["desk"] is False


def test_no_roster_file_is_an_empty_list_not_an_error(client, monkeypatch):
    seat(monkeypatch, [])
    res = client.get("/api/roster")
    assert res.status_code == 200
    assert res.json()["desks"] == []


# --- POST: upsert -----------------------------------------------------------


def test_posting_a_desk_writes_it_to_the_roster_file(client, roster_file):
    res = client.post("/api/roster", json=ACME)

    assert res.status_code == 200
    assert res.json()["ok"] is True
    desks = roster.load_roster(roster_file)
    assert [d.name for d in desks] == ["acme-growth"]
    assert desks[0].mission == ACME["mission"]
    assert desks[0].created_at > 0, "a desk must record when it was created"


def test_posting_the_same_name_twice_updates_rather_than_duplicates(
    client, roster_file
):
    client.post("/api/roster", json=ACME)
    client.post("/api/roster", json={**ACME, "mission": "Own Acme's retention."})

    desks = roster.load_roster(roster_file)
    assert len(desks) == 1
    assert desks[0].mission == "Own Acme's retention."


@pytest.mark.parametrize(
    "payload",
    [
        {"cwd": "/p", "engine": "claude", "mission": "m"},
        {"name": "x", "engine": "claude", "mission": "m"},
        {"name": "x", "cwd": "/p", "mission": "m"},
        {},
    ],
)
def test_an_incomplete_desk_is_refused_and_nothing_is_written(
    client, roster_file, payload
):
    assert client.post("/api/roster", json=payload).status_code == 400
    assert roster.load_roster(roster_file) == []


def test_an_unknown_engine_is_refused(client, roster_file):
    res = client.post("/api/roster", json={**ACME, "engine": "cursor"})
    assert res.status_code == 400
    assert roster.load_roster(roster_file) == []


# --- DELETE -----------------------------------------------------------------


def test_deleting_a_desk_removes_it(client, roster_file):
    client.post("/api/roster", json=ACME)

    res = client.delete("/api/roster/acme-growth")

    assert res.status_code == 200
    assert roster.load_roster(roster_file) == []


def test_deleting_a_desk_that_was_never_there_is_a_404(client, roster_file):
    assert client.delete("/api/roster/ghost").status_code == 404


# --- POST /start ------------------------------------------------------------


#: The two channels `spawn.start` can pick. The board's start button is not a
#: Mac feature -- this route is the deck's OWN door, and on the box it is the
#: headless launcher it must reach -- so these are swept, not pinned.
CHANNELS = [
    spawn.Channel("terminal", "gui_session", "a logged-in Mac"),
    spawn.Channel("background", "no_osascript", "no Terminal.app"),
]
CHANNEL_IDS = [channel.name for channel in CHANNELS]

#: What each launcher answers with. `spawn.start` folds this into the one result
#: shape both channels publish, so `pretrust` is not optional in either.
VOUCHED = {"ok": True, "reason": "", "detail": "", "muted": []}


@pytest.fixture
def launchers(monkeypatch):
    """Both launchers, recorded instead of run. Returns the list of desks.

    Both, not one: doubling `spawn_terminal` alone left the headless launcher
    live on the box, where it is the one this route actually takes.
    """
    started = []

    monkeypatch.setattr(spawn, "spawn_terminal", lambda desk, **kw: started.append(
        desk) or {"ok": True, "detail": "tab 3", "pretrust": VOUCHED})
    monkeypatch.setattr(spawn, "spawn_background", lambda desk, **kw: started.append(
        desk) or {"ok": True, "agent_id": "0b697cee", "pretrust": VOUCHED})
    return started


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_starting_a_desk_spawns_a_session_for_that_desk(
    channel, client, roster_file, launchers, monkeypatch
):
    client.post("/api/roster", json=ACME)
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)

    res = client.post("/api/roster/acme-growth/start")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["channel"] == channel.name
    assert len(launchers) == 1
    assert launchers[0].name == "acme-growth"
    assert launchers[0].cwd == ACME["cwd"]
    assert launchers[0].mission == ACME["mission"]


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_starting_an_unknown_desk_is_a_404_and_spawns_nothing(
    channel, client, roster_file, launchers, monkeypatch
):
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)

    assert client.post("/api/roster/ghost/start").status_code == 404
    assert launchers == []


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_a_refused_spawn_is_a_409_carrying_the_reason(channel, client,
                                                      roster_file, monkeypatch):
    """The refusal has to survive whichever launcher raised it. A route that
    only forwarded the Mac launcher's slug would answer the box with a 500."""
    client.post("/api/roster", json=ACME)

    def boom(desk, **kw):
        raise spawn.SpawnError("spawn_refused", "the launcher said no")

    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)
    monkeypatch.setattr(spawn, "spawn_terminal", boom)
    monkeypatch.setattr(spawn, "spawn_background", boom)

    res = client.post("/api/roster/acme-growth/start")

    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["reason"] == "spawn_refused"
    assert "the launcher said no" in body["detail"]
