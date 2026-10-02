"""A PROPOSED standing approval reaches him as a needs-you item, like a card.

THE GAP: a desk's proposal (`propose_standing_approval`, or
`POST /v1/standing-approvals` with `status: "proposed"`) was only visible in
`GET /v1/standing-approvals`. Nothing told him a desk was waiting on his yes,
so a proposal sat there until he happened to open the panel.

The rule pinned here: a proposal is a needs-you item on `GET /v1/owner/alerts`
(the feed ntfy, the iPhone and the Mac all read), with what it asks for
(desk, kind, limit) and the policy id to act on. Approving or revoking it
before it is pushed clears it. An owner-made active policy, and a proposal
refused as never coverable, never buzz.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import (deck_mcp, ntfy, office, owner_alerts, push_policy,
                    standing, standing_api)
from server.sources import comms as comms_mod
from tests.test_deck_mcp import tool

TOKEN = "t-secret-not-a-real-credential"
ROSTER = [{"name": "atlas", "cwd": "/tmp/atlas", "engine": "claude",
           "mission": "run it", "reports_to": None},
          {"name": "scout", "cwd": "/tmp/scout", "engine": "claude",
           "mission": "look", "reports_to": "atlas"}]
EMAIL = {"kind": "send_email", "limits": {"count_per_day": 80,
                                          "recipients": ["@acme.com"]},
         "note": "weekly updates to Acme"}


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": ROSTER}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    monkeypatch.setattr(standing, "DEFAULT_PATH", tmp_path / "standing.json")
    return tmp_path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def surface(bus, clock):
    made = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1_756_000_100.0, "sessions": []},
        comms=comms_mod.CommsIndex(), deliver=lambda name, text: True,
        roster_path=bus / "roster.json", prefs_path=bus / "agent_prefs.json",
        asks_path=bus / "asks.json", handoffs_path=bus / "handoffs.json",
        decisions_path=bus / "decisions.json",
        push_policy=push_policy.PushPolicy(settle=30, window=120, cap=6,
                                           presence=120),
        clock=clock)
    made.refresh()
    return made


@pytest.fixture
def client(surface, bus, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    built = FastAPI()
    api_mod.register(built, surface=surface, background=False)
    # Mounted as `server/app.py` mounts it: the same file the deck tool and
    # the surface read.
    paths = standing_api.Paths(bus / "standing.json", bus / "usage.json",
                               bus / "audit.jsonl")
    built.include_router(standing_api.build_router(surface,
                                                   paths=lambda: paths))
    return TestClient(built)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def poll(client, since=None):
    params = {} if since is None else {"since": since}
    response = client.get("/v1/owner/alerts", params=params, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def advance(surface, clock, seconds, step=5):
    for _ in range(int(seconds // step)):
        clock.now += step
        surface.refresh()


def proposed(client, since, surface, clock):
    surface.refresh()
    advance(surface, clock, 40)
    return [a for a in poll(client, since)["alerts"]
            if a["source"] == "standing"]


def test_a_desk_proposal_reaches_him_with_what_it_asks_and_its_id(
        client, surface, clock):
    since = poll(client)["next_since"]
    out, error = tool("scout", "propose_standing_approval", **EMAIL)
    assert not error
    [push] = proposed(client, since, surface, clock)
    assert push["kind"] == owner_alerts.NEEDS_YOU
    assert push["card_id"] == out["id"] == push["policy_id"]
    assert push["agent"] == "scout"
    assert push["thread_id"] == "direct:scout"
    assert push["title"] == "scout needs you"
    assert push["standing"]["desk"] == "scout"
    assert push["standing"]["kind"] == "send_email"
    assert push["standing"]["limits"]["count_per_day"] == 80
    assert "80" in push["body"] and "email" in push["body"]
    assert push["approve_route"] == \
        f"/v1/standing-approvals/{out['id']}/approve"
    assert f"card={out['id']}" in ntfy.click_link(push)
    assert ntfy.payload(push, "t")["priority"] == 4


def test_a_proposal_filed_through_the_route_reaches_him_too(
        client, surface, clock):
    since = poll(client)["next_since"]
    response = client.post("/v1/standing-approvals", headers=auth(), json={
        **EMAIL, "desk": "atlas", "status": "proposed",
        "proposed_by": "atlas"})
    assert response.status_code == 200, response.text
    policy_id = response.json()["policy"]["id"]
    [push] = proposed(client, since, surface, clock)
    assert (push["policy_id"], push["agent"]) == (policy_id, "atlas")


@pytest.mark.parametrize("settle", ["approve", "revoke"])
def test_approving_or_revoking_it_clears_the_item(client, surface, clock,
                                                   settle):
    since = poll(client)["next_since"]
    out, _ = tool("scout", "propose_standing_approval", **EMAIL)
    surface.refresh()
    if settle == "approve":
        r = client.post(f"/v1/standing-approvals/{out['id']}/approve",
                        headers=auth())
    else:
        r = client.delete(f"/v1/standing-approvals/{out['id']}",
                          headers=auth())
    assert r.status_code == 200, r.text
    advance(surface, clock, 400)
    assert proposed(client, since, surface, clock) == []
    # The good signal: the next proposal still reaches him.
    tool("atlas", "propose_standing_approval", **EMAIL)
    assert len(proposed(client, since, surface, clock)) == 1


def test_an_owner_made_policy_and_a_refused_proposal_never_buzz(
        client, surface, clock):
    since = poll(client)["next_since"]
    r = client.post("/v1/standing-approvals", headers=auth(),
                    json={**EMAIL, "desk": "atlas"})
    assert r.status_code == 200 and r.json()["policy"]["status"] == "active"
    out, error = tool("atlas", "propose_standing_approval", kind="run_command",
                      pattern="rm -rf *", limits={"count_per_day": 3})
    assert error and out["reason"] == "never_coverable"
    advance(surface, clock, 400)
    assert proposed(client, since, surface, clock) == []
    assert poll(client, since)["alerts"] == []


def test_a_standing_proposal_is_a_named_needs_you_source():
    assert "standing" in owner_alerts.NEEDS_YOU_SOURCES
