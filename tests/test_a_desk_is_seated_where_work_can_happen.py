"""A desk is only ever seated where work can actually happen.

**The defect, MEASURED on the box on 2026-09-07.** Four of six desks on the
real roster were seated in `/tmp`:

    listing-closer | /tmp
    acme-lead      | /tmp
    acme-product   | /tmp
    acme-growth    | /tmp

`/tmp` is not a workspace the deck allocated, so `pretrust` correctly declines
to pre-accept Claude Code's trust dialog for it, so every one of those four
sessions opened on "Is this a project you created or one you trust?" and never
ran a single turn. The board read *"Waiting for you: workspace trust was not
pre-accepted"* on four cards at once -- four desks demanding the owner's
attention for a folder he never chose.

**Where the fault was, and where it was not.** Not in `pretrust`: refusing to
auto-trust a directory the owner named is deliberate and stays. Not in
`seat_warning` either -- that reported the consequence accurately, but a warning
published beside a desk that is already dead is not a fix. The fault was that
the deck *accepted the seat*. `POST /v1/agents` took whatever `cwd` it was
handed, and the chief -- which had to put something there -- invented `/tmp`.

**The property.** A seat is honoured only when work can happen in it:

* the stated path is inside a **git repository** -- a real project. His
  `~/Projects/acme-*` repos qualify, and a desk on a real repo must keep
  sitting in that repo. Refusing that was tried once and turned 41 tests red,
  correctly.
* or it already **is a deck workspace**, which `pretrust` will vouch for.

Anything else -- `/tmp`, a `/var/folders` scratch dir, a bare home directory --
is a scratch seat, and the deck allocates a workspace of its own instead. Not a
refusal: the caller's intent ("give this agent somewhere to work") is satisfied,
it just gets somewhere that works.

**Every seating route, not just the one that bit him.** Three callers can put a
desk in a chair -- the form door, the interview door, and an agent asking for a
colleague through `YOS_HIRE`. If two of them can seat a desk and only one
checks, the defect is still shipped; it just arrives through the other door.

The GOOD signal asserted throughout is not "the cwd is not `/tmp`". It is that
`pretrust.is_deck_workspace` returns **True** for where the desk actually
landed -- the same predicate the real spawn consults -- because that is the
thing that decides whether the session starts or stalls.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, onboard, pretrust, roster, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

#: The exact seat the four stalled desks were given.
SCRATCH = "/tmp"

CHIEF = {
    "name": "chief", "cwd": "/tmp", "engine": "claude", "mission": "run it",
    "label": "Chief", "charter": "Run the company.", "reports_to": None,
}

VOUCHED = {"ok": True, "reason": "trusted", "detail": "", "muted": []}


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
def surface(bus, roster_file):
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": []},
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


@pytest.fixture
def spawned(monkeypatch):
    """Every spawn recorded rather than performed -- the interview door opens a
    real Terminal window otherwise, and this suite must never do that."""
    calls: list[dict] = []

    def fake_terminal(desk, *, seed="", **kw):
        calls.append({"desk": desk, "seed": seed})
        return {"ok": True, "detail": "stubbed", "pretrust": VOUCHED}

    monkeypatch.setattr(
        api_mod.spawn, "choose_channel",
        lambda **kw: spawn.Channel("terminal", "gui_session", "a logged-in Mac"))
    monkeypatch.setattr(api_mod.spawn, "spawn_terminal", fake_terminal)
    return calls


@pytest.fixture
def real_repo(tmp_path):
    """A git repository, made the way one is really made. Stands in for
    `~/Projects/acme-lead`: the case the deck must leave completely alone."""
    project = tmp_path / "acme-lead"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    assert (project / ".git").exists()
    return project


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def seat_of(roster_file, name: str) -> str:
    """Where the desk is recorded as sitting -- read back off the roster, not
    off the response, so a route that answers one thing and writes another is
    caught."""
    desk = next(d for d in roster.load_roster(roster_file) if d.name == name)
    return desk.cwd


def assert_work_can_happen(cwd: str, roster_file) -> None:
    """The GOOD signal. `pretrust.is_deck_workspace` is the predicate the real
    spawn path consults; True here is the difference between a session that
    runs and one that stops on the trust dialog."""
    assert pretrust.is_deck_workspace(cwd, roster_file), (
        f"{cwd} is not a workspace this deck allocated, so a session opened "
        f"there stalls on the trust dialog")
    assert cwd != SCRATCH


# ── route 1: the form door, POST /v1/agents ─────────────────────────────────


def test_the_form_door_moves_a_scratch_seat_into_a_deck_workspace(
    client, roster_file
):
    """The measured defect, through the door the chief actually used."""
    created = client.post("/v1/agents", headers=auth(), json={
        "name": "listing-closer", "cwd": SCRATCH, "engine": "claude",
        "label": "Closer", "charter": "Close the listings."})
    assert created.status_code == 201, created.text

    landed = seat_of(roster_file, "listing-closer")
    assert_work_can_happen(landed, roster_file)
    assert created.json()["agent"]["cwd"] == landed, (
        "the row must say where the desk really went")
    assert created.json()["pretrust"]["ok"] is True, (
        "the desk it just hired must not be reported as one that cannot start")


def test_the_form_door_says_the_seat_was_moved_and_from_where(client):
    """Substitution in silence is the same class of fault as the seat itself:
    the caller asked for one place and got another and has no way to know."""
    created = client.post("/v1/agents", headers=auth(), json={
        "name": "acme-growth", "cwd": SCRATCH, "engine": "claude"})
    seat = created.json()["seat"]

    assert seat["substituted"] is True
    assert seat["stated"] == SCRATCH
    assert seat["kind"] == "deck_workspace"
    assert seat["cwd"] == created.json()["agent"]["cwd"]


def test_the_form_door_leaves_a_desk_on_a_real_repository_alone(
    client, roster_file, real_repo
):
    """A desk hired onto a real project sits in that project. This is the case
    a refusal broke 41 tests over, and it is correct."""
    created = client.post("/v1/agents", headers=auth(), json={
        "name": "acme-lead", "cwd": str(real_repo), "engine": "claude"})
    assert created.status_code == 201, created.text

    assert seat_of(roster_file, "acme-lead") == str(real_repo)
    assert created.json()["seat"] == {
        "cwd": str(real_repo), "stated": str(real_repo),
        "kind": "git_repo", "substituted": False}


def test_a_seat_deeper_inside_a_repository_is_left_alone_too(
    client, roster_file, real_repo
):
    """The rule is "inside a git repository", not "is the repository root" --
    a desk pointed at `acme-lead/server` is still pointed at a real project."""
    inner = real_repo / "server" / "sources"
    inner.mkdir(parents=True)

    created = client.post("/v1/agents", headers=auth(), json={
        "name": "acme-product", "cwd": str(inner), "engine": "claude"})
    assert created.status_code == 201, created.text
    assert seat_of(roster_file, "acme-product") == str(inner)


def test_a_seat_that_is_already_a_deck_workspace_is_left_alone(
    client, roster_file, bus
):
    """Nothing is reallocated underneath a desk that was already seated right;
    a second workspace would orphan whatever the first one contains."""
    workspace = bus / "workspaces" / "atlas"
    workspace.mkdir(parents=True)
    (workspace / "notes.md").write_text("work in progress")

    created = client.post("/v1/agents", headers=auth(), json={
        "name": "atlas", "cwd": str(workspace), "engine": "claude"})
    assert created.status_code == 201, created.text

    assert seat_of(roster_file, "atlas") == str(workspace)
    assert created.json()["seat"]["substituted"] is False
    assert (workspace / "notes.md").read_text() == "work in progress"


def test_a_stated_folder_that_is_not_there_is_still_refused(client, tmp_path):
    """Substitution is for a seat where work cannot happen, not for a typo. A
    caller that meant `~/Projects/acme-lead` and wrote `acme-led` must be told,
    not quietly moved somewhere else and left believing it got the repo."""
    response = client.post("/v1/agents", headers=auth(), json={
        "name": "ghost", "cwd": str(tmp_path / "nope"), "engine": "claude"})
    assert response.status_code == 409
    assert response.json()["reason"] == "no_such_cwd"


# ── route 2: the interview door, POST /v1/agents/interview ──────────────────


def test_the_interview_door_moves_a_scratch_seat_too(
    client, roster_file, spawned
):
    """The second door states its own cwd when it is given one -- so it could
    seat a desk in `/tmp` exactly like the first."""
    created = client.post("/v1/agents/interview", headers=auth(),
                          json={"role_hint": "close listings", "cwd": SCRATCH})
    assert created.status_code == 201, created.text

    name = created.json()["name"]
    landed = seat_of(roster_file, name)
    assert_work_can_happen(landed, roster_file)
    assert created.json()["seat"]["substituted"] is True
    assert spawned[0]["desk"].cwd == landed, (
        "the session must open where the desk was actually seated")


def test_the_interview_door_leaves_a_real_repository_alone(
    client, roster_file, real_repo, spawned
):
    created = client.post("/v1/agents/interview", headers=auth(),
                          json={"role_hint": "own the app", "cwd": str(real_repo)})
    assert created.status_code == 201, created.text

    assert seat_of(roster_file, created.json()["name"]) == str(real_repo)
    assert created.json()["seat"]["substituted"] is False


def test_the_interview_door_with_no_folder_still_gets_its_own_workspace(
    client, roster_file, spawned
):
    """The path that already worked must keep working -- and must report
    itself as a workspace, not as a substitution of nothing."""
    created = client.post("/v1/agents/interview", headers=auth(),
                          json={"role_hint": "handle my email"})
    assert created.status_code == 201, created.text

    landed = seat_of(roster_file, created.json()["name"])
    assert_work_can_happen(landed, roster_file)
    assert created.json()["seat"]["stated"] == ""


# ── route 3: an agent asking for a colleague (YOS_HIRE -> apply_hire) ───────


def test_an_agent_asking_for_a_colleague_cannot_seat_it_in_scratch(roster_file):
    """The third door, and the one with no HTTP layer to check on its way in.
    A manager writing `YOS_HIRE` invents a cwd the same way the chief did."""
    desk = onboard.apply_hire(
        roster_file, "chief",
        onboard.HireRequest(name="acme-growth", label="Growth",
                            charter="Grow it.", cwd=SCRATCH))

    assert_work_can_happen(desk.cwd, roster_file)
    assert seat_of(roster_file, "acme-growth") == desk.cwd


def test_an_agent_can_still_seat_a_colleague_in_a_real_repository(
    roster_file, real_repo
):
    desk = onboard.apply_hire(
        roster_file, "chief",
        onboard.HireRequest(name="acme-lead", label="Lead",
                            charter="Own it.", cwd=str(real_repo)))
    assert desk.cwd == str(real_repo)
