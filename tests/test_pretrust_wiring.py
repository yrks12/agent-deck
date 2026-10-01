"""The trust write has to happen on the path that actually opens a window.

`server/pretrust.py` is correct in isolation and worth nothing if the interview
door never calls it. These pin the join: the workspace the deck allocated is
vouched for *before* `spawn_terminal`, a folder the owner named is not, and a
pretrust that could not happen leaves something on the desk a human can act on
instead of an `OFFLINE` indistinguishable from "hasn't started yet".

Hermetic: tmp roster, tmp prefs, tmp `.claude.json`. Nothing spawns.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import approval, office, pretrust, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    # The real spawn path runs here now, and it installs the approver on its
    # way. Redirected, because the real one sits beside the live events.jsonl
    # every session on this Mac appends to.
    monkeypatch.setattr(approval, "SETTINGS_PATH",
                        tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A stand-in for `~/.claude.json`. Redirected, never the real one -- a test
    that edits the owner's live config is a worse bug than any it could find."""
    path = tmp_path / "dot-claude.json"
    path.write_text(json.dumps({"projects": {"/Users/x/one": {
        "hasTrustDialogAccepted": True}}}, indent=2))
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", path)
    return path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": []}))
    return path


@pytest.fixture
def spawned(monkeypatch, config, osascript_is_on_path):
    """Records the command line, and the config as it stood at that instant --
    the ordering assertion needs the state *before* the window opened.

    Intercepts `subprocess.run` and nothing above it. The trust write lives
    inside `spawn.spawn_terminal` (it had to: `start_agent` and
    `app.roster_start` were skipping it), so a stub of `spawn_terminal` would
    remove the very thing these tests assert on and then pass.

    Captured as bytes, not parsed here: one of these tests corrupts the config
    on purpose, and a `json.loads` in the stub would raise `JSONDecodeError` --
    a `ValueError`, which `interview_agent` catches as a spawn refusal. The
    fixture would then be reporting its own failure as the code's."""
    calls: list[dict] = []

    class _Ran:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kwargs):
        calls.append({"script": argv[-1],
                      "config_at_spawn": config.read_bytes()})
        return _Ran()

    # This file is about the Mac door, so say so instead of letting the
    # channel be decided by ambient probing. `spawn.choose_channel` shells out
    # to `launchctl managername`, which goes through the very `subprocess.run`
    # this fixture stubs -- so without the pin the stub answers a question it
    # was never written for, the deck concludes it is headless, and these
    # tests grade the wrong channel. Both channels are swept for the same
    # gates in tests/test_spawn_channel.py.
    #
    # `osascript_is_on_path` is the other half of the same sentence, and it was
    # missing. `spawn_terminal` reads `shutil.which("osascript")` a SECOND time,
    # on its own account, and the Linux box answers None. MEASURED there, same
    # commit as the Mac's clean run: all six tests in this file failed with
    # `SpawnError: osascript is not on PATH` -- the right refusal for the
    # product, and the wrong one for a file that has already said which install
    # it describes.
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: spawn.Channel(
        "terminal", "aqua", "a logged-in graphical session"))
    monkeypatch.setattr(spawn.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def client(bus, roster_file, monkeypatch):
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
    )
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _without_bypass(raw: bytes) -> dict:
    """The account-level bypass disclaimer (`pretrust.accept_bypass`) is written
    for every claude desk; what must stay untouched is everything else."""
    import json
    data = json.loads(raw)
    data.pop("bypassPermissionsModeAccepted", None)
    return data


def test_the_allocated_workspace_is_trusted_before_the_window_opens(
    client, bus, config, spawned
):
    """GOOD signal, and an ordering one: the key is already in the config at the
    moment `spawn_terminal` is called. Trusting it afterwards is trusting it too
    late -- the dialog is drawn the instant the CLI starts."""
    response = client.post("/v1/agents/interview",
                           json={"role_hint": "handle my email"}, headers=auth())
    assert response.status_code == 201, response.text

    workspace = str(bus / "workspaces" / response.json()["agent"]["name"])
    assert f"cd {workspace} &&" in spawned[0]["script"]
    at_spawn = json.loads(spawned[0]["config_at_spawn"])
    assert at_spawn["projects"][workspace]["hasTrustDialogAccepted"] is True
    assert "/Users/x/one" in at_spawn["projects"]


def test_a_folder_the_owner_named_is_vouched_for_at_interview(
    client, tmp_path, config, spawned
):
    """"Set it up myself" is his directory. The deck opens a window there and
    accepts nothing on his behalf.

    A REAL repository, not a bare `mkdir`. `hire.hire` now moves a seat where
    work cannot happen into a workspace of its own (`server/seat.py`), and an
    empty directory nobody has ever built anything in is exactly that -- so a
    bare `mkdir` here would be asserting this property about a folder the deck
    no longer seats anyone in. What "his directory" means is a project, and a
    project has a `.git`.

    Made by hand rather than by `git init`, because the `spawned` fixture in
    this file doubles `subprocess.run` for the whole test -- a real `git init`
    lands in `spawned[0]` and the assertions below then read the wrong call.
    """
    before = config.read_bytes()
    theirs = tmp_path / "his-own-repo"
    (theirs / ".git").mkdir(parents=True)

    response = client.post("/v1/agents/interview",
                           json={"role_hint": "x", "cwd": str(theirs)},
                           headers=auth())

    assert response.status_code == 201, response.text
    assert f"cd {theirs} &&" in spawned[0]["script"]
    # OWNER RULING 2026-09-30, agents never dead-end: his folder is vouched
    # for too, so the window opens on the desk, not on the trust dialog.
    assert json.loads(config.read_text())["projects"][str(theirs.resolve())][
        "hasTrustDialogAccepted"] is True
    assert before != config.read_bytes()


def test_a_pretrust_that_could_not_happen_is_visible_on_the_desk(
    client, bus, config, spawned
):
    """The half that made this invisible for a whole build cycle. When the write
    fails the session still opens -- on the dialog -- so the desk must say so
    rather than sitting at OFFLINE looking like one that has not started."""
    config.write_text("this is not json")

    response = client.post("/v1/agents/interview",
                           json={"role_hint": "handle my email"}, headers=auth())
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["pretrust"]["ok"] is False
    assert body["pretrust"]["reason"] == "unexpected_shape"

    row = client.get(f"/v1/agents/{body['name']}", headers=auth()).json()
    assert row["state"] == "OFFLINE"
    assert row["blocked"]["reason"] == "unexpected_shape"
    assert "trust" in row["blocked"]["what"].lower()

    ledger = [json.loads(ln) for ln in
              (bus / "events.jsonl").read_text().splitlines() if ln.strip()]
    assert [e for e in ledger if e["event"] == "pretrust" and not e["ok"]]


def test_a_desk_that_starts_clean_carries_no_stale_blocked_reason(
    client, bus, config, spawned
):
    """A reason that never clears is noise, and noise is how the next real one
    gets ignored."""
    first = client.post("/v1/agents/interview",
                        json={"role_hint": "a"}, headers=auth()).json()
    assert first["pretrust"]["ok"] is True

    row = client.get(f"/v1/agents/{first['name']}", headers=auth()).json()
    assert row["blocked"] is None


def test_the_response_says_which_mcp_servers_the_new_hire_was_denied(
    client, bus, config, spawned, tmp_path
):
    """The client tells the owner what his new hire can and cannot reach. That
    has to come off the response, not be inferred from a ledger it cannot read."""
    # `bus` IS this test's tmp_path, so `bus.parent` is pytest's SHARED root --
    # writing there leaks a .mcp.json into every other test in the run, because
    # discovery walks up to `/`. `bus` itself is an ancestor of
    # `bus/workspaces/<name>` and is per-test, which is what this needs.
    (bus / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"docker-mcp": {"command": "x"},
                        "other-mcp": {"command": "y"}}}))

    response = client.post("/v1/agents/interview",
                           json={"role_hint": "handle my email"}, headers=auth())
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["pretrust"]["ok"] is True
    assert body["pretrust"]["muted"] == ["docker-mcp", "other-mcp"]

    workspace = str(bus / "workspaces" / body["agent"]["name"])
    at_spawn = json.loads(spawned[0]["config_at_spawn"])
    assert at_spawn["projects"][workspace]["disabledMcpjsonServers"] == [
        "docker-mcp", "other-mcp"]


def test_muted_is_an_empty_list_not_a_missing_key_when_there_is_nothing(
    client, bus, config, spawned
):
    """A client that has to distinguish "no servers" from "field absent" will
    get it wrong once. The key is always there."""
    body = client.post("/v1/agents/interview",
                       json={"role_hint": "x"}, headers=auth()).json()

    assert body["pretrust"]["muted"] == []
