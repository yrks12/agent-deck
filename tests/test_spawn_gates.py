"""Every door that starts a session must close the same gates.

**The defect this pins (D2).** `Surface.interview_agent` vouched for the
workspace before opening the window and wrote the refusal onto the desk row.
`Surface.start_agent` and `app.roster_start` did neither -- so an agent that
hired a colleague and then asked the deck to start it got a Terminal window
stalled on "Is this a project you created or one you trust?" while the API row
read `state: OFFLINE, blocked: null`: indistinguishable from a desk nobody had
started. Measured live: `drift-watch` emitted `YOS_HIRE`, the deck created
`branch-scout`, `POST /v1/agents/branch-scout/start` answered
`{"ok": true, "detail": ""}`, and `~/.claude.json` carried no trust entry for
that workspace while every interview-door workspace had one.

**The class, not the instance.** The fault is not "start_agent forgot a line".
It is that this gate lived in one *caller* while everything else a spawn must
do -- `--name`, `--settings` -- had already been moved into the thing all
callers share. A fourth door added next month repeats it exactly. So the gate
belongs in `spawn.spawn_terminal`, and a door added later either hands it a
`roster_path` or does not run at all.

**The GOOD signal, never the absence of an error.** A trusted workspace means
`projects[<cwd>].hasTrustDialogAccepted is True`, present in the config *at the
instant the window opens*, and the spawn still happening. Not "no exception" --
the live bug raised nothing and answered `ok: true`.

Hermetic: tmp roster, tmp prefs, tmp `.claude.json`, tmp ledger. Nothing spawns.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import app as app_mod
from server import approval, office, pretrust, roster, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Ran:
    """What `subprocess.run` returns when osascript is happy."""

    returncode = 0
    stdout = "tab 3"
    stderr = ""


#: The five-line banner `claude --bg` really prints, byte for byte as measured
#: in tests/test_spawn_channel.py. `spawn_background` refuses `no_agent_id`
#: without an id it can read, so a launcher sweep needs the right stdout for
#: each launcher rather than one stand-in for both.
_BG_BANNER = ("backgrounded · 0b697cee · branch-scout\n"
              "  claude attach 0b697cee    open in this terminal\n")


def _ran(stdout: str):
    """A `subprocess.run` result carrying `stdout`."""
    return type("_Result", (), {"returncode": 0, "stdout": stdout,
                                "stderr": ""})


#: Both launchers the shared path can end in, with what each one's own binary
#: prints. PARAMETRIZED, not pinned, because the gate below is not the Mac's:
#: `spawn_background` vouches on exactly the same line, and the box is the
#: install where that is the launcher every hire goes through.
LAUNCHERS = [("terminal", "tab 3"), ("background", _BG_BANNER)]


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """The deck's own directory, relocated -- the real one holds the live
    `events.jsonl` every session on this Mac appends to, and two of these tests
    run the real `spawn_terminal`, which installs the approver on its way."""
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(approval, "SETTINGS_PATH",
                        tmp_path / "approve-settings.json")
    monkeypatch.setattr(approval, "EVENTS_PATH", tmp_path / "events.jsonl")
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A stand-in for `~/.claude.json`; the real one is never touched."""
    path = tmp_path / "dot-claude.json"
    path.write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", path)
    return path


@pytest.fixture
def workspace(bus):
    """The kind of directory an agent-hired junior always gets: a direct child
    of the deck's own `workspaces/`, seconds old, that the CLI has never seen.
    """
    path = bus / "workspaces" / "branch-scout"
    path.mkdir(parents=True)
    roster.save_roster(bus / "roster.json", [roster.Desk(
        name="branch-scout", cwd=str(path), engine="claude",
        mission="Watch the unmerged branches.")])
    return path


@pytest.fixture
def spawned(monkeypatch, config, osascript_is_on_path):
    """Stubs out `osascript` and nothing else, recording the command line and
    the config as it stood at that instant.

    Deliberately NOT a stub of `spawn_terminal`: the whole claim under test is
    that the shared spawn path closes the gate, so a test that replaced that
    path would be grading its own stub. Only the one call that would open a
    real window on this Mac is intercepted.

    The config is captured as bytes, not parsed: one test corrupts it on
    purpose, and a `json.loads` inside the stub would surface as the code's
    failure rather than its own.
    """
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"script": argv[-1], "config_at_spawn": config.read_bytes()})
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
def v1(bus, workspace, monkeypatch):
    """`POST /v1/agents/{name}/start` -- the door a boss agent uses on a
    colleague the harvester hired for it. This is the one that failed live."""
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=bus / "roster.json",
        prefs_path=bus / "agent_prefs.json")
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


@pytest.fixture
def legacy(bus, workspace, monkeypatch):
    """`POST /api/roster/{name}/start` -- the door `web/app.js` still calls."""
    monkeypatch.setattr(app_mod, "ROSTER_PATH", bus / "roster.json")
    return TestClient(app_mod.app)


# -- the detector ------------------------------------------------------------


def _without_bypass(raw: bytes) -> dict:
    """The account-level bypass disclaimer (`pretrust.accept_bypass`) is written
    for every claude desk; what must stay untouched is everything else."""
    import json
    data = json.loads(raw)
    data.pop("bypassPermissionsModeAccepted", None)
    return data


def test_the_v1_start_door_trusts_the_workspace_before_the_window_opens(
    v1, workspace, spawned
):
    """THE detector for D2. Same claim `test_pretrust_wiring` makes about the
    interview door, made about the door an agent hiring an agent goes through.
    """
    response = v1.post("/v1/agents/branch-scout/start", headers=AUTH)
    assert response.status_code == 200, response.text

    assert len(spawned) == 1, "the desk must still actually start"
    at_spawn = json.loads(spawned[0]["config_at_spawn"])
    assert at_spawn["projects"][str(workspace)]["hasTrustDialogAccepted"] is True


def test_the_legacy_start_door_trusts_the_workspace_too(legacy, workspace,
                                                        spawned):
    """The third caller, and the reason this is a sweep. `web/app.js` still
    posts here, so a desk started from the board must clear the same gate."""
    response = legacy.post("/api/roster/branch-scout/start")
    assert response.status_code == 200, response.text

    assert len(spawned) == 1
    at_spawn = json.loads(spawned[0]["config_at_spawn"])
    assert at_spawn["projects"][str(workspace)]["hasTrustDialogAccepted"] is True


def test_a_start_that_could_not_vouch_leaves_a_reason_on_the_row(v1, config,
                                                                 spawned):
    """The other half of the live symptom. `blocked: null` on a desk whose
    window is sitting on the trust dialog is what made this invisible; the row
    has to carry something a human can act on."""
    config.write_text("this is not json")

    response = v1.post("/v1/agents/branch-scout/start", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["pretrust"]["ok"] is False

    row = v1.get("/v1/agents/branch-scout", headers=AUTH).json()
    assert row["blocked"]["reason"] == "unexpected_shape"
    assert "trust" in row["blocked"]["what"].lower()


def test_a_start_that_vouched_clears_a_stale_reason(bus, v1, spawned):
    """A desk blocked on yesterday's attempt that starts cleanly today must not
    keep wearing the old reason. The row would then be lying in the other
    direction -- which is how a thing that now works goes on looking broken,
    and it is the same instrument fault, pointed the other way."""
    (bus / "agent_prefs.json").write_text(json.dumps({
        "version": 1, "agents": {"branch-scout": {"blocked": {
            "what": "workspace trust was not pre-accepted",
            "reason": "locked", "detail": "", "at": 1.0}}}}))
    stale = v1.get("/v1/agents/branch-scout", headers=AUTH).json()
    assert stale["blocked"]["reason"] == "locked", "precondition"

    v1.post("/v1/agents/branch-scout/start", headers=AUTH)

    row = v1.get("/v1/agents/branch-scout", headers=AUTH).json()
    assert row["blocked"] is None


@pytest.mark.parametrize("launcher,stdout", LAUNCHERS,
                         ids=[name for name, _ in LAUNCHERS])
def test_the_shared_path_is_the_one_that_vouches(launcher, stdout, bus,
                                                 workspace, config, monkeypatch,
                                                 osascript_is_on_path):
    """The sweep, stated as a test rather than as a convention.

    The launcher closes the gate itself, so a door added later cannot skip it by
    forgetting a line: it can only skip it by not spawning at all. The ledger
    line is the audit half -- a refusal has to be as loud as a success, because
    a silent OFFLINE is what hid this for a whole build cycle.

    Swept over BOTH launchers rather than pinned to the Mac's, because this
    claim is about the shared path and not about the channel. Pinning it would
    have left the box -- where `spawn_background` is the launcher every single
    hire goes through -- with no test that the workspace is ever vouched for at
    all.
    """
    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: _ran(stdout)())
    desk = roster.Desk(name="branch-scout", cwd=str(workspace),
                       engine="claude", mission="")

    result = getattr(spawn, f"spawn_{launcher}")(
        desk, roster_path=bus / "roster.json")

    assert result["pretrust"]["ok"] is True
    assert json.loads(config.read_text())["projects"][str(workspace)][
        "hasTrustDialogAccepted"] is True
    ledger = [json.loads(ln) for ln in
              (bus / "events.jsonl").read_text().splitlines() if ln.strip()]
    assert [e for e in ledger if e["event"] == "pretrust" and e["ok"]]


@pytest.mark.parametrize("launcher,stdout", LAUNCHERS,
                         ids=[name for name, _ in LAUNCHERS])
def test_a_folder_the_owner_named_is_vouched_for_through_both_launchers(
    launcher, stdout, bus, tmp_path, config, monkeypatch, osascript_is_on_path
):
    """The limit on the sweep. Widening the gate to every door must not widen
    *what* it trusts: a desk the owner pointed at his own repo is his, and the
    deck accepts no security dialog on his behalf.

    Both launchers, for the reason above: a headless hire has no window in which
    a trust dialog could ever appear, so a deck that silently accepted one on the
    box would be even harder to notice than on the Mac."""
    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: _ran(stdout)())
    theirs = tmp_path / "his-own-repo"
    theirs.mkdir()
    before = config.read_bytes()

    result = getattr(spawn, f"spawn_{launcher}")(
        roster.Desk(name="his", cwd=str(theirs), engine="claude", mission=""),
        roster_path=bus / "roster.json")

    # OWNER RULING 2026-09-30, agents never dead-end: a folder he seated a
    # desk in is vouched for through both launchers, exactly like a workspace.
    assert result["ok"] is True, "his desk still starts"
    assert result["pretrust"]["ok"] is True, result["pretrust"]
    assert json.loads(config.read_text())["projects"][str(theirs.resolve())][
        "hasTrustDialogAccepted"] is True
    assert before != config.read_bytes()
