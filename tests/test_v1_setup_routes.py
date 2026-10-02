"""The /v1 routes that let the app *set things up*, not just watch.

Reading and sending was the whole client surface: you could see the agents and
talk to them, and nothing else. Everything that makes the deck useful -- hiring
a desk, answering the question an agent is stuck on, scheduling a routine --
was only reachable from a browser on the Mac. These are those three, and the
three failures that would actually cost something:

* `test_reports_to_is_taken_on_hire_and_still_refused_as_a_setting` -- the org
  chart has exactly one door. `POST` walks through it (that IS hiring); `PATCH`
  is still 409. The asymmetry lives in one test so it reads as deliberate.
* `test_two_clients_creating_the_same_name_get_one_desk_and_one_refusal` -- a
  double-tap on "+" must not write two desks or a torn roster.
* `test_always_on_a_handoff_subject_is_refused_and_writes_no_rule` -- the
  secure-handoff floor is not routable-around. A credential is asked about
  every time, by design.

Hermetic: a tmp bus (roster, asks, rules, routines, prefs, message log), a
hand-written snapshot, no daemon, no network, and nothing spawned -- the one
test that opens a real Terminal window is marked `live` and is deselected by
pytest.ini's default addopts.
"""

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import asking, autoreview, hire, office, roster, routines, spawn
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {
    "name": "chief", "cwd": "/tmp/p", "engine": "claude", "mission": "run it",
    "label": "Negotiator", "charter": "Own the deal.", "reports_to": None,
}
HEMINGWAY = {
    "name": "hemingway", "cwd": "/tmp/p", "engine": "claude", "mission": "write",
    "label": "Researcher", "charter": "Own the words.", "reports_to": "chief",
}

# A folder under $HOME so the option summary reads the way the phone message
# does ("~/Projects/acme"). Nothing is written there; an ask carries a string.
ACME = str(Path.home() / "Projects" / "acme")


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF, HEMINGWAY]}))
    return path


@pytest.fixture
def snapshot():
    return {
        "generated_at": 1_756_000_100.0,
        "sessions": [{
            "session_id": "sid-chief", "pid": 4242, "name": "chief",
            "cwd": "/tmp/p", "project": "p", "state": "WORKING",
            "state_since": 1_756_000_000.0,
        }],
    }


@pytest.fixture
def surface(bus, roster_file, snapshot):
    return api_mod.Surface(
        snapshot=lambda: snapshot,
        comms=comms_mod.CommsIndex(),
        roster_path=roster_file,
        prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json",
        routines_path=bus / "routines.json",
    )


@pytest.fixture
def app(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    return built


@pytest.fixture
def client(app):
    return TestClient(app)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def new_desk(tmp_path, **over):
    body = {"name": "seeker", "label": "Researcher", "charter": "Find things.",
            "cwd": str(tmp_path), "engine": "claude"}
    body.update(over)
    return body


# -- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("post", "/v1/agents"),
    ("delete", "/v1/agents/chief"),
    ("post", "/v1/agents/chief/start"),
    ("get", "/v1/approvals"),
    ("post", "/v1/approvals/abc"),
    ("get", "/v1/routines"),
    ("post", "/v1/routines"),
    ("patch", "/v1/routines/r1"),
    ("delete", "/v1/routines/r1"),
])
def test_every_setup_route_needs_the_bearer_token(client, method, path):
    """A write route that forgot `_authorise` is the whole point of the token."""
    response = client.request(method.upper(), path, json={})
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"


# -- hiring -------------------------------------------------------------------


def test_creating_an_agent_returns_the_row_the_sidebar_would_have_shown(
    client, tmp_path
):
    """201 carries a real row, so the "+" button inserts without a refetch."""
    created = client.post("/v1/agents", headers=auth(),
                          json=new_desk(tmp_path, reports_to="chief"))
    assert created.status_code == 201
    row = created.json()["agent"]
    assert row["name"] == "seeker"
    assert row["label"] == "Researcher"
    assert row["state"] == "OFFLINE", "a hire is not a spawn; nobody is seated"
    assert row["desk"] is True
    assert row["boss"] == "chief"
    assert row["thread_id"] == "direct:seeker"
    assert row["unread"] == 0

    listed = client.get("/v1/agents", headers=auth()).json()["agents"]
    assert row in listed, "the row must be exactly what GET /v1/agents returns"

    settings = client.get("/v1/agents/seeker", headers=auth()).json()
    assert settings["charter"] == "Find things."
    assert settings["mission"] == "Find things.", "the charter IS the mission"
    assert settings["engine"] == "claude"
    # `tmp_path` is a `/var/folders` scratch directory -- neither a repository
    # nor a deck workspace -- so `hire.hire` seats this desk in a workspace of
    # its own instead. See `server/seat.py`. What the route owes the caller is
    # that the row and the settings agree on where the desk really went, and
    # that `seat` names the path that was asked for.
    assert settings["cwd"] == row["cwd"]
    assert created.json()["seat"] == {
        "cwd": row["cwd"], "stated": str(tmp_path),
        "kind": "deck_workspace", "substituted": True}


def test_a_name_already_on_the_roster_is_refused_as_name_taken(
    client, roster_file, tmp_path
):
    response = client.post("/v1/agents", headers=auth(),
                           json=new_desk(tmp_path, name="chief"))
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "name_taken",
        "detail": "a desk named 'chief' already exists",
    }
    assert [d.name for d in roster.load_roster(roster_file)] == [
        "chief", "hemingway"], "a refused hire writes nothing"


def test_no_such_boss_keeps_its_own_slug(client, tmp_path):
    response = client.post("/v1/agents", headers=auth(),
                           json=new_desk(tmp_path, reports_to="nobody"))
    assert response.status_code == 409
    assert response.json()["reason"] == "no_such_boss"


def test_too_deep_keeps_its_own_slug(client, tmp_path):
    """Three levels is the cap; a fourth is refused as `too_deep`, not as a 500."""
    mid = client.post("/v1/agents", headers=auth(),
                      json=new_desk(tmp_path, name="mid", reports_to="hemingway"))
    assert mid.status_code == 201, mid.text
    response = client.post("/v1/agents", headers=auth(),
                           json=new_desk(tmp_path, name="deep", reports_to="mid"))
    assert response.status_code == 409
    assert response.json()["reason"] == "too_deep"


def test_a_mac_full_of_unrelated_sessions_does_not_block_a_hire(
    client, tmp_path, snapshot
):
    """MAX_LIVE caps the size of the org, not how many Claude Code windows this
    Mac happens to have open. `roster_file` seats exactly one desk (`chief`,
    already live in `snapshot`); the rest of these session cards belong to
    nobody's desk -- subagent worktrees, other terminals, scratch windows --
    and must not count against the cap. GOOD signal: the hire still succeeds
    with 201 even though the raw session count alone is past MAX_LIVE."""
    snapshot["sessions"] = snapshot["sessions"] + [
        {"session_id": f"sid-scratch-{i}", "pid": 1000 + i, "name": f"scratch-{i}",
         "cwd": "/tmp/p", "project": "p", "state": "WORKING",
         "state_since": 1_756_000_000.0}
        for i in range(hire.MAX_LIVE)
    ]
    response = client.post("/v1/agents", headers=auth(), json=new_desk(tmp_path))
    assert response.status_code == 201, response.text


def test_too_many_live_desks_keeps_its_own_slug(client, tmp_path, roster_file, snapshot):
    """The guard the cap actually protects: MAX_LIVE desks each seated with a
    live session still refuses the next hire. GOOD signal, the other half --
    a fix that just raised or deleted the cap would pass the test above and
    fail this one."""
    desks = [CHIEF] + [
        {"name": f"desk-{i}", "cwd": "/tmp/p", "engine": "claude", "mission": "x",
         "label": "", "charter": "", "reports_to": None}
        for i in range(hire.MAX_LIVE - 1)
    ]
    roster_file.write_text(json.dumps({"version": 1, "agents": desks}))
    snapshot["sessions"] = [
        {"session_id": f"sid-{i}", "pid": 1000 + i, "name": d["name"],
         "cwd": "/tmp/p", "project": "p", "state": "WORKING",
         "state_since": 1_756_000_000.0}
        for i, d in enumerate(desks)
    ]
    response = client.post("/v1/agents", headers=auth(), json=new_desk(tmp_path))
    assert response.status_code == 409
    assert response.json()["reason"] == "too_many_live"


def test_a_cwd_that_is_not_a_folder_keeps_its_own_slug(client, tmp_path):
    response = client.post("/v1/agents", headers=auth(),
                           json=new_desk(tmp_path, cwd=str(tmp_path / "nope")))
    assert response.status_code == 409
    assert response.json()["reason"] == "no_such_cwd"


@pytest.mark.parametrize("missing", ["name", "cwd", "engine"])
def test_a_hire_without_the_fields_it_needs_names_the_field(client, tmp_path, missing):
    body = new_desk(tmp_path)
    body[missing] = ""
    response = client.post("/v1/agents", headers=auth(), json=body)
    assert response.status_code == 400
    assert response.json()["reason"] == "missing_field"
    assert missing in response.json()["detail"]


def test_an_engine_nothing_can_start_is_refused_before_the_desk_exists(
    client, tmp_path
):
    response = client.post("/v1/agents", headers=auth(),
                           json=new_desk(tmp_path, engine="gpt"))
    assert response.status_code == 400
    assert response.json()["reason"] == "unknown_engine"
    assert client.get("/v1/agents/seeker", headers=auth()).status_code == 404


def test_reports_to_is_taken_on_hire_and_still_refused_as_a_setting(
    client, tmp_path
):
    """The org chart has one door, and it is hiring.

    Creating a desk under a named boss IS the org chart being written -- that is
    what hiring means. Changing it afterwards from the settings panel is not,
    and stays a 409 with the documented reason. Both halves are here so the
    asymmetry is visible on one screen.
    """
    created = client.post("/v1/agents", headers=auth(),
                          json=new_desk(tmp_path, reports_to="chief"))
    assert created.status_code == 201
    assert created.json()["agent"]["boss"] == "chief"

    refused = client.patch("/v1/agents/seeker", headers=auth(),
                           json={"reports_to": "hemingway"})
    assert refused.status_code == 409
    assert refused.json()["reason"] == "reports_to_is_not_a_setting"
    assert client.get("/v1/agents/seeker", headers=auth()).json()["boss"] == "chief"


def test_two_clients_creating_the_same_name_get_one_desk_and_one_refusal(
    app, roster_file, tmp_path, monkeypatch
):
    """A double-tap on "+" writes one desk, not two and not a torn roster.

    `roster.upsert` is atomic on its own; what is not atomic is
    read-the-roster / check-the-name / write-it-back. The sleep widens that
    window rather than inventing one -- two real requests land inside it on a
    busy machine.
    """
    real_upsert = hire.upsert

    def slow_upsert(path, desk):
        time.sleep(0.15)
        real_upsert(path, desk)

    monkeypatch.setattr(hire, "upsert", slow_upsert)
    body = new_desk(tmp_path, name="twin")

    async def both():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as http:
            return await asyncio.gather(
                http.post("/v1/agents", headers=auth(), json=body),
                http.post("/v1/agents", headers=auth(), json=body),
            )

    first, second = asyncio.run(both())
    codes = sorted([first.status_code, second.status_code])
    assert codes == [201, 409], f"{first.text} / {second.text}"
    refused = first if first.status_code == 409 else second
    assert refused.json()["reason"] == "name_taken"

    desks = roster.load_roster(roster_file)
    assert [d.name for d in desks].count("twin") == 1
    assert len(desks) == 3


def test_firing_a_desk_takes_it_off_the_roster(client, tmp_path, monkeypatch):
    from server import sandbox, spawn
    monkeypatch.setattr(spawn, "jobs_of_desk", lambda name: [])
    monkeypatch.setattr(sandbox, "stop", lambda desk: None)
    assert client.post("/v1/agents", headers=auth(),
                       json=new_desk(tmp_path)).status_code == 201
    response = client.delete("/v1/agents/seeker", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "name": "seeker"}
    assert "seeker" not in [
        a["name"] for a in client.get("/v1/agents", headers=auth()).json()["agents"]
    ]


def test_firing_a_manager_who_still_has_reports_is_refused(client):
    response = client.delete("/v1/agents/chief", headers=auth())
    assert response.status_code == 409
    assert response.json()["reason"] == "has_reports"
    assert "hemingway" in response.json()["detail"]
    assert client.get("/v1/agents/chief", headers=auth()).status_code == 200


def test_firing_a_name_that_is_not_a_desk_is_404(client):
    response = client.delete("/v1/agents/ghost", headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"


def _must_not_spawn(desk, **kw):
    raise AssertionError("the default suite must never start a real session")


#: The two channels `spawn.start` can pick. Swept, not pinned: `POST
#: /v1/agents/{name}/start` is the door an agent uses to start the colleague it
#: just hired, and on the box that reaches the headless launcher. A pin here
#: would test the only door that matters on exactly the install where it has
#: never once worked.
CHANNELS = [
    spawn.Channel("terminal", "gui_session", "a logged-in Mac"),
    spawn.Channel("background", "no_osascript", "no Terminal.app"),
]
CHANNEL_IDS = [channel.name for channel in CHANNELS]

#: `pretrust` is not optional. `spawn.start` folds the launcher's answer into
#: the one result shape both channels share, and a double that omits it is
#: describing a launcher that cannot exist.
VOUCHED = {"ok": True, "reason": "", "detail": "", "muted": []}


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_starting_a_desk_that_does_not_exist_never_reaches_a_launcher(
    channel, client, monkeypatch
):
    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)
    monkeypatch.setattr(spawn, "spawn_terminal", _must_not_spawn)
    monkeypatch.setattr(spawn, "spawn_background", _must_not_spawn)
    response = client.post("/v1/agents/ghost/start", headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_a_refused_spawn_comes_back_with_its_own_reason(channel, client,
                                                        monkeypatch):
    """The refusal has to survive whichever launcher raised it -- a route that
    only forwarded the Mac launcher's slug would answer the box with a 500."""
    def refuse(desk, **kw):
        raise spawn.SpawnError("spawn_refused", "the launcher said no")

    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)
    monkeypatch.setattr(spawn, "spawn_terminal", refuse)
    monkeypatch.setattr(spawn, "spawn_background", refuse)
    response = client.post("/v1/agents/chief/start", headers=auth())
    assert response.status_code == 409
    assert response.json()["reason"] == "spawn_refused"
    assert response.json()["detail"] == "the launcher said no"


@pytest.mark.parametrize("channel", CHANNELS, ids=CHANNEL_IDS)
def test_starting_a_desk_hands_spawn_the_desk_it_named(channel, client,
                                                       monkeypatch):
    seen = {}

    def fake_terminal(desk, **kw):
        seen["desk"] = desk
        return {"ok": True, "detail": "tab 1", "pretrust": VOUCHED}

    def fake_background(desk, **kw):
        seen["desk"] = desk
        return {"ok": True, "agent_id": "0b697cee", "pretrust": VOUCHED}

    monkeypatch.setattr(spawn, "choose_channel", lambda **kw: channel)
    monkeypatch.setattr(spawn, "spawn_terminal", fake_terminal)
    monkeypatch.setattr(spawn, "spawn_background", fake_background)
    response = client.post("/v1/agents/hemingway/start", headers=auth())
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["channel"] == channel.name
    assert seen["desk"].name == "hemingway"
    assert seen["desk"].charter == "Own the words."


@pytest.mark.live
def test_start_really_opens_a_terminal_window(client, tmp_path):
    """Opens a REAL Terminal window. `-m live` only, never the default suite."""
    assert client.post("/v1/agents", headers=auth(),
                       json=new_desk(tmp_path)).status_code == 201
    response = client.post("/v1/agents/seeker/start", headers=auth())
    assert response.status_code == 200
    assert response.json()["ok"] is True


# -- approvals ----------------------------------------------------------------


@pytest.fixture
def ask(bus):
    """One live question. Recorded at `now`, because a question older than
    `expiry.DEFAULT_TTL` is not pending -- it has timed out, and that is a
    different test."""
    return asking.record(bus / "asks.json", agent="chief", tool="Bash",
                         subject="gh pr create --title x", cwd=ACME)


def test_a_pending_ask_carries_the_exact_rule_each_reply_would_create(client, ask):
    """"Always allow" without saying what it allows is how you grant the machine.

    The card has to be able to read *"always -> allow `gh pr*` in
    ~/Projects/acme"* before he taps it, so every option ships the rule it
    would write, not just its name.
    """
    body = client.get("/v1/approvals", headers=auth()).json()
    row = body["approvals"][0]
    assert row["id"] == ask.id
    assert row["agent"] == "chief"
    assert row["tool"] == "Bash"
    assert row["subject"] == "gh pr create --title x"
    assert row["cwd"] == ACME
    assert row["cwd_short"] == "~/Projects/acme"
    assert row["ts"] == ask.ts
    assert row["status"] == "pending"

    options = {o["reply"]: o for o in row["options"]}
    assert [o["reply"] for o in row["options"]] == ["once", "always", "never"]

    assert options["once"]["rule"] is None
    assert options["once"]["available"] is True

    always = options["always"]
    assert always["available"] is True
    assert always["summary"] == (
        "allow `gh pr*` in ~/Projects/acme, never ask again"
    )
    assert always["rule"] == asdict(asking.rule_from(ask, "always"))
    assert always["rule"]["kind"] == autoreview.ALWAYS_ALLOW
    assert always["rule"]["tool"] == "Bash"
    assert always["rule"]["pattern"] == "gh pr*", "widened to the verb, no further"
    assert always["rule"]["cwd"] == ACME

    never = options["never"]
    assert never["rule"]["kind"] == autoreview.DENY
    assert never["summary"] == "refuse `gh pr*` in ~/Projects/acme from now on"


def test_approvals_are_newest_first(client, bus):
    for index in range(3):
        asking.record(bus / "asks.json", agent="chief", tool="Bash",
                      subject=f"echo {index}", cwd=ACME,
                      ts=time.time() + index)
    rows = client.get("/v1/approvals", headers=auth()).json()["approvals"]
    assert [r["subject"] for r in rows] == ["echo 2", "echo 1", "echo 0"]


def test_a_credential_shaped_subject_is_redacted_before_it_leaves_the_machine(
    client, bus
):
    asking.record(bus / "asks.json", agent="chief", tool="Bash",
                  subject="curl -H 'Authorization: Bearer " + "sk-abcdef0123456789'",
                  cwd=ACME)
    row = client.get("/v1/approvals", headers=auth()).json()["approvals"][0]
    assert "sk-abcdef0123456789" not in json.dumps(row)
    assert asking.MASK in row["subject"]


def test_always_writes_the_rule_and_hands_it_straight_back(client, bus, ask):
    response = client.post(f"/v1/approvals/{ask.id}", headers=auth(),
                           json={"reply": "always"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["ask"]["answered"] == "always"
    assert body["ask"]["status"] == "answered"
    # Keyed by the DESK that asked, not the session: see
    # tests/test_always_means_always.py for why a desk-less rule never fired.
    assert body["rule"] == asdict(asking.desk_rules(ask, "chief")[0])
    assert body["rules"] == [asdict(r) for r in asking.desk_rules(ask, "chief")]

    saved = autoreview.load_rules(bus / "autoreview.json")
    assert [r.pattern for r in saved] == ["gh pr*"]
    assert saved[0].kind == autoreview.ALWAYS_ALLOW
    assert saved[0].desk == "chief"
    assert client.get("/v1/approvals", headers=auth()).json()["approvals"] == []


def test_once_creates_no_rule_and_says_so(client, bus, ask):
    response = client.post(f"/v1/approvals/{ask.id}", headers=auth(),
                           json={"reply": "once"})
    assert response.status_code == 200
    assert response.json()["rule"] is None
    assert response.json()["ask"]["answered"] == "once"
    assert autoreview.load_rules(bus / "autoreview.json") == []


def test_answering_the_same_ask_twice_is_a_distinct_refusal(client, ask):
    assert client.post(f"/v1/approvals/{ask.id}", headers=auth(),
                       json={"reply": "never"}).status_code == 200
    again = client.post(f"/v1/approvals/{ask.id}", headers=auth(),
                        json={"reply": "always"})
    assert again.status_code == 409
    assert again.json()["reason"] == "already_answered"
    assert "never" in again.json()["detail"]


def test_answering_an_ask_that_timed_out_is_a_distinct_refusal(client, bus):
    stale = asking.record(bus / "asks.json", agent="chief", tool="Bash",
                          subject="echo hi", cwd=ACME,
                          ts=time.time() - 5 * 60 * 60)
    listed = client.get("/v1/approvals", headers=auth()).json()["approvals"]
    assert listed == [], "a question he can no longer act on leaves the board"

    response = client.post(f"/v1/approvals/{stale.id}", headers=auth(),
                           json={"reply": "always"})
    assert response.status_code == 409
    assert response.json()["reason"] == "expired"
    assert autoreview.load_rules(bus / "autoreview.json") == [], (
        "an expired ask grants nothing")


def test_always_on_a_handoff_subject_no_desk_can_be_named_for_is_refused(
        client, bus):
    """The secure-handoff floor is not routable-around -- globally.

    A credential, a payment or something irreversible that the deck cannot
    pin on a desk still asks every time. The route refuses `always` outright
    rather than quietly downgrading it, so the card can say why -- and the ask
    stays live, because he has not actually decided anything yet.
    """
    secret = asking.record(bus / "asks.json",
                           agent="11111111-2222-3333-4444-555555555555",
                           tool="Bash", subject="cat .env", cwd="/nowhere")
    row = client.get("/v1/approvals", headers=auth()).json()["approvals"][0]
    always = next(o for o in row["options"] if o["reply"] == "always")
    assert always["available"] is False
    assert always["rule"] is None
    assert always["summary"] == asking.HANDOFF_NOTE

    response = client.post(f"/v1/approvals/{secret.id}", headers=auth(),
                           json={"reply": "always"})
    assert response.status_code == 409
    assert response.json()["reason"] == "always_not_available"
    assert autoreview.load_rules(bus / "autoreview.json") == []
    assert asking.pending(bus / "asks.json")[0].id == secret.id

    allowed = client.post(f"/v1/approvals/{secret.id}", headers=auth(),
                          json={"reply": "once"})
    assert allowed.status_code == 200
    assert allowed.json()["ask"]["answered"] == "once"


def test_always_on_a_handoff_subject_from_a_desk_is_honoured_for_that_desk(
        client, bus):
    """The owner's answer, per desk (2026-09-30 bypass ruling): once he has
    read the class on the card and said never ask again, the floor stops
    asking THAT desk for THAT class -- and nobody else."""
    secret = asking.record(bus / "asks.json", agent="chief", tool="Bash",
                           subject="cat .env", cwd=ACME)
    response = client.post(f"/v1/approvals/{secret.id}", headers=auth(),
                           json={"reply": "always"})
    assert response.status_code == 200
    saved = autoreview.load_rules(bus / "autoreview.json")
    assert [(r.desk, r.pattern) for r in saved] == [("chief", "cat .env*")]


def test_a_reply_that_is_not_an_answer_is_refused(client, bus, ask):
    response = client.post(f"/v1/approvals/{ask.id}", headers=auth(),
                           json={"reply": "maybe later"})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_reply"
    assert [a.id for a in asking.pending(bus / "asks.json")] == [ask.id]


def test_answering_an_ask_that_does_not_exist_is_404(client):
    response = client.post("/v1/approvals/zzzzz", headers=auth(),
                           json={"reply": "once"})
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_ask"


# -- routines -----------------------------------------------------------------


def cron(spec="0 9 * * *", tz="Europe/London"):
    return {"kind": "cron", "spec": spec, "tz": tz}


def _fires(spec, at, tz="Europe/London"):
    """Both answers next_fire can give across the clock this test spans.

    The route computes from its own `time.time()`, microseconds after the test
    read one. Pinning a single value would fail once a day, inside the minute
    the spec names."""
    return {routines.next_fire(spec, tz, at),
            routines.next_fire(spec, tz, time.time())}


def test_creating_a_routine_computes_the_next_run_from_the_spec(client):
    before = time.time()
    response = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "morning check", "trigger": cron(),
    })
    assert response.status_code == 201
    row = response.json()["routine"]
    assert row["id"]
    assert row["agent"] == "chief"
    assert row["prompt"] == "morning check"
    assert row["trigger"] == cron()
    assert row["enabled"] is True
    assert row["runs"] == []
    assert row["next_run_at"] in _fires("0 9 * * *", before), (
        "next_run_at must come from next_fire, not from the client")
    assert row["next_run_at"] > before, "a routine must never be born overdue"


def test_the_panel_lists_a_routine_with_its_last_few_runs(client, bus):
    created = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "morning check", "trigger": cron(),
    }).json()["routine"]
    for index in range(4):
        routines.record_run(bus / "routines.json", created["id"],
                            ts=1_756_000_000.0 + index, ok=index != 1,
                            detail="delivered" if index != 1 else "no session")

    rows = client.get("/v1/routines", headers=auth()).json()["routines"]
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]
    assert rows[0]["next_run_at"] == created["next_run_at"]
    assert [r["ts"] for r in rows[0]["runs"]] == [
        1_756_000_003.0, 1_756_000_002.0, 1_756_000_001.0,
    ], "newest first, and only the last few"
    assert rows[0]["runs"][2]["ok"] is False


def test_a_client_supplied_next_run_is_refused_on_create_and_on_edit(client):
    """A next-run from the client is how a routine fires in the past, forever."""
    refused = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(),
        "next_run_at": 1.0,
    })
    assert refused.status_code == 409
    assert refused.json()["reason"] == "next_run_at_is_computed"
    assert client.get("/v1/routines", headers=auth()).json()["routines"] == []

    created = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(),
    }).json()["routine"]
    edited = client.patch(f"/v1/routines/{created['id']}", headers=auth(),
                          json={"next_run_at": 1.0})
    assert edited.status_code == 409
    assert edited.json()["reason"] == "next_run_at_is_computed"
    rows = client.get("/v1/routines", headers=auth()).json()["routines"]
    assert rows[0]["next_run_at"] == created["next_run_at"]


def test_changing_the_trigger_recomputes_the_next_run(client):
    created = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(),
    }).json()["routine"]
    at = time.time()
    edited = client.patch(f"/v1/routines/{created['id']}", headers=auth(),
                          json={"trigger": cron("30 18 * * *")})
    assert edited.status_code == 200
    row = edited.json()["routine"]
    assert row["trigger"]["spec"] == "30 18 * * *"
    assert row["next_run_at"] in _fires("30 18 * * *", at)
    assert row["next_run_at"] != created["next_run_at"]


def test_disabling_a_routine_keeps_its_schedule_and_its_runs(client, bus):
    created = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(),
    }).json()["routine"]
    routines.record_run(bus / "routines.json", created["id"], ts=1.0, ok=True,
                        detail="delivered")

    edited = client.patch(f"/v1/routines/{created['id']}", headers=auth(),
                          json={"enabled": False, "prompt": "quieter"})
    assert edited.status_code == 200
    row = edited.json()["routine"]
    assert row["enabled"] is False
    assert row["prompt"] == "quieter"
    assert row["next_run_at"] == created["next_run_at"]
    assert [r["ts"] for r in row["runs"]] == [1.0], "editing must not lose history"


def test_an_unparseable_cron_spec_is_a_400_that_names_the_field(client):
    response = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron("every friday"),
    })
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_cron"
    assert "trigger.spec" in response.json()["detail"]
    assert client.get("/v1/routines", headers=auth()).json()["routines"] == [], (
        "a routine the scheduler could never fire is not written")


def test_an_unknown_timezone_is_a_400_that_names_the_field(client):
    response = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(tz="Mars/Olympus"),
    })
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_cron"
    assert "trigger.tz" in response.json()["detail"]


def test_a_routine_for_a_name_that_is_not_on_the_board_is_404(client):
    response = client.post("/v1/routines", headers=auth(), json={
        "agent": "ghost", "prompt": "p", "trigger": cron(),
    })
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"


@pytest.mark.parametrize("missing", ["agent", "prompt"])
def test_a_routine_without_the_fields_it_needs_names_the_field(client, missing):
    body = {"agent": "chief", "prompt": "p", "trigger": cron()}
    body[missing] = ""
    response = client.post("/v1/routines", headers=auth(), json=body)
    assert response.status_code == 400
    assert response.json()["reason"] == "missing_field"
    assert missing in response.json()["detail"]


def test_deleting_a_routine_takes_it_off_the_panel(client):
    created = client.post("/v1/routines", headers=auth(), json={
        "agent": "chief", "prompt": "p", "trigger": cron(),
    }).json()["routine"]
    response = client.delete(f"/v1/routines/{created['id']}", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": created["id"]}
    assert client.get("/v1/routines", headers=auth()).json()["routines"] == []


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_touching_a_routine_that_does_not_exist_is_404(client, method):
    response = client.request(method, "/v1/routines/nope", headers=auth(),
                              json={"enabled": False})
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_routine"


def test_the_daemon_needs_no_extra_wiring_for_these_files():
    """Mounted with nothing but a snapshot, /v1 still finds the real files.

    Every path here defaults to the module that owns it, so `register(app,
    snapshot=...)` in the daemon reads the same asks, rules and routines the
    hooks and the scheduler already write -- and a test that passes its own
    paths (as this file does) never touches Sam's live agent-bus.
    """
    default = api_mod.Surface()
    assert default.asks_path == asking.DEFAULT_PATH
    assert default.rules_path == autoreview.DEFAULT_PATH
    assert default.routines_path == routines.DEFAULT_PATH
