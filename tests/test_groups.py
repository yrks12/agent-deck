"""Groups: several agents in one conversation about one project.

The smallest honest version. A group is a named set of desk names with its own
thread; a message to it **fans out through the existing `office.send` path**,
one queued record per member, so a member that is offline gets it queued
exactly as a direct message would be. No new delivery mechanism -- inventing
one is how you end up with a second, subtly different set of rules for when a
message is actually delivered.

The two failures pinned here are the ones a fanout gets wrong:

* **exactly once** -- a member on two lists, or a retry, must not get the text
  twice, and every member must get it at all;
* **offline is not dropped** -- the member with nobody at its desk is the whole
  reason the office queue exists.

Hermetic: tmp roster, tmp message log, tmp groups file. Nothing spawns.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import groups as groups_mod
from server import office
from server import onboard
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"

CHIEF = {"name": "chief", "cwd": "/tmp", "engine": "claude", "mission": "run it",
         "label": "Negotiator", "charter": "Own the deal.", "reports_to": None}
HEMINGWAY = {"name": "hemingway", "cwd": "/tmp", "engine": "claude",
             "mission": "write", "label": "Researcher", "charter": "Own the words.",
             "reports_to": "chief"}
SEEKER = {"name": "seeker", "cwd": "/tmp", "engine": "claude", "mission": "find",
          "label": "Researcher", "charter": "Find things.", "reports_to": "chief"}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def roster_file(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [CHIEF, HEMINGWAY, SEEKER]}))
    return path


@pytest.fixture
def snapshot():
    """Only `chief` has a live session. The other two desks are OFFLINE."""
    return {"generated_at": 1_756_000_100.0, "sessions": [{
        "session_id": "sid-chief", "pid": 4242, "name": "chief", "cwd": "/tmp",
        "project": "p", "state": "WORKING", "state_since": 1_756_000_000.0}]}


@pytest.fixture
def delivered():
    return []


@pytest.fixture
def surface(bus, roster_file, snapshot, delivered):
    def deliver(name, text):
        """Only a live session takes bytes now; everyone else is queued."""
        if name != "chief":
            return False
        delivered.append((name, text))
        return True

    return api_mod.Surface(
        snapshot=lambda: snapshot, comms=comms_mod.CommsIndex(),
        roster_path=roster_file, prefs_path=bus / "agent_prefs.json",
        deliver=deliver,
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def make_group(client, name="acme-launch", members=("chief", "hemingway", "seeker")):
    response = client.post("/v1/groups",
                           json={"name": name, "members": list(members)},
                           headers=auth())
    assert response.status_code == 201, response.text
    return response.json()["group"]


def queued(bus):
    return [json.loads(line) for line in
            (bus / "messages.jsonl").read_text().splitlines() if line.strip()]


# ── the fanout ─────────────────────────────────────────────────────────────


def test_a_group_message_reaches_every_member_exactly_once(client, bus):
    group = make_group(client)

    sent = client.post(f"/v1/threads/{group['thread_id']}/messages",
                       json={"text": "ship the acme reader on friday"},
                       headers=auth())
    assert sent.status_code == 201, sent.text

    recipients = [r["to"] for r in queued(bus)
                  if r.get("text") == "ship the acme reader on friday"]
    assert sorted(recipients) == ["chief", "hemingway", "seeker"]
    assert len(recipients) == len(set(recipients))


def test_a_member_with_no_live_session_still_gets_it_queued(client, bus, delivered):
    """`chief` is live and takes the bytes now; the other two are OFFLINE and
    the office queue holds it for their next turn -- exactly a direct send."""
    group = make_group(client)
    client.post(f"/v1/threads/{group['thread_id']}/messages",
                json={"text": "ship the acme reader on friday"}, headers=auth())

    # Under his mark, because the socket skips `hooks/cc-office.js` where the
    # framing otherwise happens. The two members on the queue get theirs framed
    # by the hook on their next turn, so all three read the same sender.
    assert delivered == [
        ("chief", office.attribute("ship the acme reader on friday", "owner"))]
    assert office.pending_counts() == {"hemingway": 1, "seeker": 1}


def test_the_group_thread_shows_the_message_once_not_once_per_member(client):
    group = make_group(client)
    client.post(f"/v1/threads/{group['thread_id']}/messages",
                json={"text": "ship the acme reader on friday"}, headers=auth())

    page = client.get(f"/v1/threads/{group['thread_id']}/messages", headers=auth())
    assert page.status_code == 200, page.text
    assert [m["text"] for m in page.json()["messages"]] == [
        "ship the acme reader on friday"]
    assert page.json()["read_only"] is False


def test_a_message_to_an_unknown_group_is_refused(client):
    response = client.post("/v1/threads/group:nobody/messages",
                           json={"text": "hello"}, headers=auth())
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_group"


# ── the roster of groups ───────────────────────────────────────────────────


def test_a_group_is_listed_with_its_members_and_its_thread(client):
    made = make_group(client)
    assert made["members"] == ["chief", "hemingway", "seeker"]
    assert made["thread_id"] == "group:acme-launch"

    listed = client.get("/v1/groups", headers=auth())
    assert listed.status_code == 200, listed.text
    assert [g["name"] for g in listed.json()["groups"]] == ["acme-launch"]


def test_the_group_thread_names_its_members_as_participants(client):
    make_group(client, members=("chief", "hemingway"))
    threads = client.get("/v1/threads", headers=auth()).json()["threads"]
    thread = next(t for t in threads if t["id"] == "group:acme-launch")
    assert thread["kind"] == "group"
    assert set(thread["participants"]) >= {"chief", "hemingway"}


def test_a_group_naming_a_desk_that_does_not_exist_is_refused(client):
    response = client.post("/v1/groups",
                           json={"name": "ghosts", "members": ["chief", "nobody"]},
                           headers=auth())
    assert response.status_code == 409
    assert response.json()["reason"] == "unknown_member"
    assert client.get("/v1/groups", headers=auth()).json()["groups"] == []


def test_a_group_with_no_members_is_refused(client):
    response = client.post("/v1/groups", json={"name": "empty", "members": []},
                           headers=auth())
    assert response.status_code == 400
    assert response.json()["reason"] == "no_members"


def test_two_groups_cannot_share_a_name(client):
    make_group(client)
    response = client.post("/v1/groups",
                           json={"name": "acme-launch", "members": ["chief"]},
                           headers=auth())
    assert response.status_code == 409
    assert response.json()["reason"] == "name_taken"


def test_deleting_a_group_removes_it_and_leaves_every_desk_alone(client, roster_file):
    make_group(client)
    response = client.delete("/v1/groups/acme-launch", headers=auth())
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "name": "acme-launch"}

    assert client.get("/v1/groups", headers=auth()).json()["groups"] == []
    assert client.get("/v1/agents/chief", headers=auth()).status_code == 200
    assert client.delete("/v1/groups/acme-launch",
                         headers=auth()).status_code == 404


# ── the module underneath ──────────────────────────────────────────────────


def test_groups_are_persisted_beside_the_roster(client, roster_file):
    make_group(client)
    reloaded = groups_mod.load_groups(groups_mod.groups_path(roster_file))
    assert [g.name for g in reloaded] == ["acme-launch"]
    assert reloaded[0].members == ("chief", "hemingway", "seeker")


def test_an_unreadable_groups_file_reads_as_no_groups(tmp_path):
    """Same rule as `roster.load_roster`: a corrupt file is not an exception
    thrown at a request, it is an empty list and a board that still paints."""
    path = tmp_path / "groups.json"
    path.write_text("{not json")
    assert groups_mod.load_groups(path) == []


# ── a member that renamed itself ───────────────────────────────────────────


def test_a_group_message_still_reaches_a_member_that_renamed_itself(
    client, bus, roster_file
):
    """`Group.members` holds the names the group was created WITH. A member
    that names itself during its interview stops matching that tuple, and
    `office.send` will happily queue for a name nobody occupies -- no error, no
    log line, the message just goes nowhere and the member never hears from the
    group again.

    Resolution has to happen at SEND time, not at write time: the stored group
    keeps the name it was created with, so a rename after the fact must still
    land. Asserts the GOOD signal -- the queued record's recipient is the NEW
    name and the member's pending count goes up -- never the absence of an
    error, which is also what nothing-ran looks like.
    """
    group = make_group(client)
    onboard.apply_patch(
        roster_file, "hemingway",
        onboard.DeskPatch(name="voice", label="Researcher",
                          charter="Own the words."),
    )

    sent = client.post(f"/v1/threads/{group['thread_id']}/messages",
                       json={"text": "ship the acme reader on friday"},
                       headers=auth())
    assert sent.status_code == 201, sent.text

    recipients = sorted(r["to"] for r in queued(bus)
                        if r.get("text") == "ship the acme reader on friday")
    assert recipients == ["chief", "seeker", "voice"], \
        "the renamed member was sent to under a name nobody occupies"
    assert office.pending_counts().get("voice") == 1
    assert sorted(sent.json()["queued"]) == ["seeker", "voice"]


def test_two_member_names_that_now_point_at_one_desk_get_one_copy(bus):
    """Exactly-once is this module's contract, and resolving names can make two
    entries into one. The dedupe has to happen AFTER resolution or the desk
    reads the same instruction twice."""
    group = groups_mod.Group(name="g", members=("hemingway", "voice"))
    rows = groups_mod.broadcast(
        group, "hello", resolve=lambda n: "voice" if n == "hemingway" else n)

    assert [r["to"] for r in rows] == ["voice"]
    assert office.pending_counts() == {"voice": 1}


def test_a_group_with_nobody_renamed_sends_to_exactly_the_stored_names(bus):
    """The no-op case: resolution must not perturb the ordinary fanout."""
    group = groups_mod.Group(name="g", members=("chief", "seeker"))
    rows = groups_mod.broadcast(group, "hello")
    assert [r["to"] for r in rows] == ["chief", "seeker"]
