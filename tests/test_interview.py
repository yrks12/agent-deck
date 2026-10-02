"""Hiring as a conversation: you say what you want, the agent names itself.

`POST /v1/agents` is a config form -- name, title, charter, cwd, engine, boss,
all up front. `POST /v1/agents/interview` is the other door: one sentence about
what you want, a provisional desk with a placeholder name, and a session seeded
with `onboard.interview_prompt` that asks what the job actually is and then
emits its own `YOS_DESK` line.

**The rename is the load-bearing part and it is what these tests are for.** A
desk's name is the key under which every message, every org edge, every read
cursor and every unread count is stored. If renaming placeholder -> real does
not carry all of that, the conversation the owner is having with the new hire
vanishes at the exact moment the hire names itself -- which is the one moment
the feature exists for.

Hermetic: tmp roster, tmp message log, tmp prefs. Nothing spawns -- the one
call that would open a Terminal is stubbed and its arguments are asserted on.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, onboard, roster, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {
    "name": "chief", "cwd": "/tmp", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF]}))
    return path


@pytest.fixture
def snapshot():
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0,
        }],
    }


#: The pretrust verdict both launchers publish. `muted` is always present.
VOUCHED = {"ok": True, "reason": "trusted", "detail": "", "muted": []}

#: The two channels `spawn.start` can pick, injected rather than probed.
CHANNELS = [
    spawn.Channel("terminal", "gui_session", "a logged-in Mac"),
    spawn.Channel("background", "no_osascript", "no Terminal.app"),
]


@pytest.fixture(params=CHANNELS, ids=[c.name for c in CHANNELS])
def spawned(request, monkeypatch):
    """Every spawn the surface asks for, recorded instead of performed --
    on BOTH channels.

    It used to double `spawn_terminal` alone. That is the door the owner's Mac
    takes and it is not the door his Linux box takes: `spawn.start` reads the
    machine's capabilities and picks the headless launcher there, straight past
    a double that only covers the other one. MEASURED on the box, same commit as
    the Mac's clean run: all twelve tests in this file ran the REAL launcher --
    `conftest.ScreenTaken: tried to run 'claude'` -- and each also tripped the
    bus guard, because `spawn.start` writes `approve-settings.json` and a
    pretrust record on its way there.

    PARAMETRIZED rather than pinned, and that is the point of the repair.
    Nothing this file asserts -- the placeholder desk, the seed, the rename
    carrying messages, org edges, read cursors and unread counts -- is a
    property of Terminal.app. Pinning would have gone green while leaving the
    interview door, the one door a new hire arrives through, untested on the
    only install where it takes the headless channel.

    Both launchers are doubled, so a door that took the wrong one is a recorded
    call in the wrong bucket rather than a real session on the roster.
    """
    channel = request.param
    calls: list[dict] = []

    def fake_terminal(desk, *, seed="", **kw):
        calls.append({"desk": desk, "seed": seed, "channel": "terminal", **kw})
        # The real launchers vouch for the workspace and hand the verdict back;
        # a stub that omitted it would be describing a spawner that cannot
        # exist, and the surface would 500 on the real contract.
        return {"ok": True, "detail": "stubbed", "pretrust": VOUCHED}

    def fake_background(desk, *, seed="", **kw):
        calls.append({"desk": desk, "seed": seed, "channel": "background", **kw})
        return {"ok": True, "agent_id": "0b697cee", "pretrust": VOUCHED}

    monkeypatch.setattr(api_mod.spawn, "choose_channel", lambda **kw: channel)
    monkeypatch.setattr(api_mod.spawn, "spawn_terminal", fake_terminal)
    monkeypatch.setattr(api_mod.spawn, "spawn_background", fake_background)
    return calls


@pytest.fixture
def surface(bus, roster_file, snapshot):
    return api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def interview(client, tmp_path, **extra):
    body = {"role_hint": "handle my email", "cwd": str(tmp_path), **extra}
    response = client.post("/v1/agents/interview", json=body, headers=auth())
    assert response.status_code == 201, response.text
    return response.json()


def names(path):
    return sorted(d.name for d in roster.load_roster(path))


# ── the door itself ────────────────────────────────────────────────────────


def test_the_interview_creates_a_provisional_desk_and_hands_back_its_thread(
    client, tmp_path, roster_file, spawned
):
    """The client must be able to open the conversation from this response
    alone -- a second round trip to find the thread is a blank screen."""
    body = interview(client, tmp_path)

    placeholder = body["agent"]["name"]
    assert placeholder
    assert body["provisional"] is True
    assert body["thread_id"] == f"direct:{placeholder}"
    assert body["agent"]["desk"] is True
    assert placeholder in names(roster_file)

    opened = client.get(f"/v1/threads/{body['thread_id']}/messages", headers=auth())
    assert opened.status_code == 200, opened.text


def test_the_interview_seeds_the_session_with_the_interview_prompt(
    client, tmp_path, spawned
):
    """The seed IS `onboard.interview_prompt`. If the two ever drift the new
    hire is briefed with something that never tells it to emit `YOS_DESK`,
    and it sits there forever as a session with no desk."""
    interview(client, tmp_path)

    assert len(spawned) == 1
    seed = spawned[0]["seed"]
    assert seed == onboard.interview_prompt("handle my email", "")
    assert "handle my email" in seed
    assert onboard.DESK_PREFIX in seed


def test_the_mandate_is_the_real_one_when_it_exists(client, tmp_path, bus, spawned):
    bus.joinpath("mandate.md").write_text("We read acmes and we sell it.")
    interview(client, tmp_path)
    assert "We read acmes and we sell it." in spawned[0]["seed"]


def test_no_mandate_file_means_an_empty_mandate_not_an_invented_one(
    client, tmp_path, bus, spawned
):
    assert not bus.joinpath("mandate.md").exists()
    interview(client, tmp_path)
    assert spawned[0]["seed"] == onboard.interview_prompt("handle my email", "")


def test_a_cwd_that_is_not_a_directory_leaves_no_desk_behind(
    client, roster_file, spawned
):
    response = client.post(
        "/v1/agents/interview",
        json={"role_hint": "x", "cwd": "/tmp/definitely-not-a-directory-xyz"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "no_such_cwd"
    assert names(roster_file) == ["chief"]
    assert spawned == []


def test_the_form_door_is_untouched(client, tmp_path, roster_file):
    """`POST /v1/agents` is a second door, not a replacement."""
    response = client.post(
        "/v1/agents",
        json={"name": "seeker", "cwd": str(tmp_path), "engine": "claude",
              "label": "Researcher", "charter": "Find things.",
              "reports_to": "chief"},
        headers=auth(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["agent"]["name"] == "seeker"
    assert response.json()["agent"]["boss"] == "chief"


# ── the rename carries everything ──────────────────────────────────────────


def rename(roster_file, placeholder, new, **fields):
    return onboard.apply_patch(
        roster_file, placeholder, onboard.DeskPatch(name=new, **fields)
    )


def test_a_thread_started_against_the_placeholder_reads_under_the_new_name(
    client, tmp_path, roster_file, spawned
):
    """THE test. The owner talks to the new hire *while* it is working out what
    it is; the moment it names itself that conversation must still be there."""
    body = interview(client, tmp_path)
    placeholder = body["agent"]["name"]

    said = client.post(
        f"/v1/threads/direct:{placeholder}/messages",
        json={"text": "mostly invoices and scheduling"},
        headers=auth(),
    )
    assert said.status_code == 201, said.text

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    page = client.get("/v1/threads/direct:inbox-hand/messages", headers=auth())
    assert page.status_code == 200, page.text
    # BOTH, and the first one is what he typed into the "+" door itself. It
    # used to be absent -- the interview door delivered it as the session seed
    # and recorded it nowhere -- so this list began at his second sentence and
    # the conversation the rename carried was missing its opening line. See
    # `Surface._record_opening`.
    assert [m["text"] for m in page.json()["messages"]] == [
        "handle my email",
        "mostly invoices and scheduling",
    ]


def test_the_org_edge_to_its_boss_survives_the_rename(
    client, tmp_path, roster_file, spawned
):
    body = interview(client, tmp_path, reports_to="chief")
    placeholder = body["agent"]["name"]

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    renamed = client.get("/v1/agents/inbox-hand", headers=auth())
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["boss"] == "chief"

    chief = client.get("/v1/agents/chief", headers=auth())
    assert "inbox-hand" in chief.json()["reports"]


def test_the_read_cursor_follows_the_rename(client, tmp_path, roster_file, spawned):
    """Unread is keyed on the name too. A rename that drops the read cursor
    re-marks the whole conversation unread the instant the agent names itself."""
    body = interview(client, tmp_path)
    placeholder = body["agent"]["name"]

    office.send(api_mod.OWNER, "here is what I think the job is",
                sender=placeholder)
    client.get("/v1/agents", headers=auth())
    assert client.get(f"/v1/agents/{placeholder}", headers=auth()).json()["unread"] == 1

    marked = client.post(f"/v1/agents/{placeholder}/read", json={}, headers=auth())
    assert marked.status_code == 200, marked.text
    assert marked.json()["unread"] == 0

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    after = client.get("/v1/agents/inbox-hand", headers=auth())
    assert after.status_code == 200, after.text
    assert after.json()["unread"] == 0


def test_a_name_collision_at_rename_is_refused_and_both_desks_survive(
    client, tmp_path, roster_file, spawned
):
    """Refused, never merged. `roster.upsert` keys on the name, so a rename
    onto a taken name does not error -- it overwrites, and a desk disappears."""
    body = interview(client, tmp_path)
    placeholder = body["agent"]["name"]

    with pytest.raises(onboard.OnboardError) as refused:
        rename(roster_file, placeholder, "chief")
    assert refused.value.reason in {"not_yours", "unsafe_name"}

    assert names(roster_file) == sorted(["chief", placeholder])
    chief = next(d for d in roster.load_roster(roster_file) if d.name == "chief")
    assert chief.charter == "Own the deal."


# ── the wire tells the client the rename happened ──────────────────────────


def test_a_rename_is_announced_on_the_wire_with_the_whole_row(
    client, surface, tmp_path, roster_file, spawned
):
    """The sidebar replaces the row in place rather than refetching, so a
    partial row is a flicker or a duplicate. Without this frame nothing on the
    wire triggers the rename at all and the client sits on a dead name."""
    body = interview(client, tmp_path, reports_to="chief")
    placeholder = body["agent"]["name"]
    surface.refresh()

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    announced = [e for e in surface.refresh() if e["type"] == "agent_renamed"]
    assert len(announced) == 1
    assert announced[0]["old_name"] == placeholder

    row = announced[0]["agent"]
    assert row["name"] == "inbox-hand"
    assert row["thread_id"] == "direct:inbox-hand"
    assert row["boss"] == "chief"
    assert row["label"] == "Email"
    sidebar = client.get("/v1/agents", headers=auth()).json()["agents"]
    assert set(row) == set(sidebar[0]), "not the full row the sidebar swaps in"

    assert [e for e in surface.refresh() if e["type"] == "agent_renamed"] == [], \
        "a rename is announced once, not on every tick"


def test_the_old_thread_id_still_resolves_after_the_rename(
    client, tmp_path, roster_file, spawned
):
    """A client that was mid-request when the agent named itself must not get a
    404 for the chat it has open. The old id forwards to the new thread."""
    body = interview(client, tmp_path)
    placeholder = body["agent"]["name"]
    client.post(f"/v1/threads/direct:{placeholder}/messages",
                json={"text": "mostly invoices"}, headers=auth())

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    page = client.get(f"/v1/threads/direct:{placeholder}/messages", headers=auth())
    assert page.status_code == 200, page.text
    assert page.json()["thread_id"] == "direct:inbox-hand"
    assert [m["text"] for m in page.json()["messages"]] == [
        "handle my email", "mostly invoices"]


# ── it gets its own computer ───────────────────────────────────────────────


def test_the_interview_allocates_its_own_workspace_when_no_folder_is_given(
    client, bus, roster_file, spawned
):
    """Hitting "+" must not ask for a project folder. With no `cwd` the deck
    allocates one and the session runs there -- and the directory has to exist
    *before* the spawn, or the thing that fails is the spawn."""
    response = client.post("/v1/agents/interview",
                           json={"role_hint": "handle my email"}, headers=auth())
    assert response.status_code == 201, response.text

    name = response.json()["agent"]["name"]
    workspace = bus / "workspaces" / name
    assert workspace.is_dir()
    assert oct(workspace.stat().st_mode)[-3:] == "700"

    assert spawned[0]["desk"].cwd == str(workspace)
    assert response.json()["agent"]["cwd"] == str(workspace)
    assert client.get(f"/v1/agents/{name}", headers=auth()).json()["cwd"] == str(
        workspace)


def test_the_allocated_workspace_does_not_move_when_the_agent_renames_itself(
    client, bus, roster_file, spawned
):
    """The path is allocated once. Moving a live session's cwd out from under
    it is worse than an ugly directory name."""
    response = client.post("/v1/agents/interview",
                           json={"role_hint": "handle my email"}, headers=auth())
    placeholder = response.json()["agent"]["name"]
    workspace = bus / "workspaces" / placeholder

    rename(roster_file, placeholder, "inbox-hand", label="Email",
           charter="You own the owner's inbox.")

    settings = client.get("/v1/agents/inbox-hand", headers=auth())
    assert settings.status_code == 200, settings.text
    assert settings.json()["cwd"] == str(workspace)
    assert workspace.is_dir()


def test_the_form_door_still_refuses_a_directory_that_is_not_there(client):
    """The manual path is unchanged: state a folder and it has to be real."""
    response = client.post(
        "/v1/agents",
        json={"name": "seeker", "cwd": "/tmp/definitely-not-a-directory-xyz",
              "engine": "claude"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "no_such_cwd"
