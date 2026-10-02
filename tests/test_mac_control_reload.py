"""Mac control reaches desks that are already running.

Owner, 2026-10-01: "Atlas must be able to use it the moment the owner flips the
toggle." MEASURED on the box the same day: every live desk's `mac` MCP process
was started before the control tools were deployed, so Atlas had no
mcp__mac__click at all, and it told him control was off even after the deploy.
A desk's MCP process keeps the code it started with; only a reload of the desk
(stop + resume, same conversation) gives it the new tool list.

What these pin:

* Each `mac` MCP process stamps the tool set it serves, per desk. A desk with
  no stamp (started before stamps existed) or an older tool set is stale.
* The store says once per grant that a grant is new (`take_new_grant`), so a
  long-poll every second does not reload anyone twice.
* The poll hands a new grant's scope to the deck's reload hook, once.
* Only live Claude desks the grant covers, whose mac tools are stale, reload.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import mac_api, mac_mcp, mac_nodes
from tests.test_mac_api import BEARER, TOKEN, Rig


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DECK_TOKEN", TOKEN)
    r = Rig(tmp_path)
    r.reloads = []
    app = FastAPI()
    app.include_router(mac_api.build_router(
        r.store, tell=lambda d, t: r.told.append((d, t)),
        owner_line=r.owner.append, handoffs_path=r.handoffs,
        reload_stale=r.reloads.append))
    r.client = TestClient(app)
    return r


def _control(rig, scope="all", secs=1800):
    return {"scope": scope, "until": rig.clock() + secs}


# ── the stamp ────────────────────────────────────────────────────────────────


def test_a_desk_without_a_stamp_is_stale_and_a_stamped_one_is_not(tmp_path):
    assert mac_mcp.stale("atlas", root=tmp_path) is True
    mac_mcp.write_stamp("atlas", root=tmp_path)
    assert mac_mcp.stale("atlas", root=tmp_path) is False
    assert mac_mcp.stale("scout", root=tmp_path) is True


def test_a_stamp_of_an_older_tool_set_is_stale(tmp_path):
    mac_mcp.write_stamp("atlas", root=tmp_path)
    path = mac_mcp.stamp_path("atlas", root=tmp_path)
    row = json.loads(path.read_text())
    row["tools"] = "run,read,write"
    path.write_text(json.dumps(row))
    assert mac_mcp.stale("atlas", root=tmp_path) is True


def test_the_stamp_covers_the_control_tools():
    for name in mac_mcp.CONTROL_TOOLS:
        assert name in mac_mcp.TOOLS_SIG_NAMES


def test_starting_the_mcp_process_writes_its_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_mcp, "STAMP_ROOT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert mac_mcp.main(["--desk", "atlas"]) == 0
    assert mac_mcp.stale("atlas", root=tmp_path) is False


def test_a_desk_name_cannot_write_outside_the_stamp_folder(tmp_path):
    assert mac_mcp.stamp_path("../../etc/x", root=tmp_path).parent == tmp_path


# ── one new-grant signal per grant ───────────────────────────────────────────


def _store_poll(rig, node_id, control=None):
    """The store alone: the router would take the signal itself."""
    rig.store.poll(node_id, 4, [], "ask", {}, control=control)


def test_the_store_says_a_grant_is_new_once(rig):
    node_id, _ = rig.mac()
    _store_poll(rig, node_id)
    assert rig.store.take_new_grant(node_id) is None
    grant = _control(rig, "atlas")
    _store_poll(rig, node_id, grant)
    assert rig.store.take_new_grant(node_id) == "atlas"
    _store_poll(rig, node_id, grant)
    assert rig.store.take_new_grant(node_id) is None
    _store_poll(rig, node_id, _control(rig, "all", 1700))
    assert rig.store.take_new_grant(node_id) == "all"


def test_an_expired_grant_is_never_new(rig):
    node_id, _ = rig.mac()
    _store_poll(rig, node_id, {"scope": "all", "until": rig.clock() - 1})
    assert rig.store.take_new_grant(node_id) is None


# ── the poll hands it to the deck ────────────────────────────────────────────


def test_turning_control_on_reloads_stale_desks_once(rig):
    node_id, mac = rig.mac()
    rig.poll(node_id, mac)
    assert rig.reloads == []
    grant = _control(rig)
    rig.poll(node_id, mac, control=grant)
    rig.poll(node_id, mac, control=grant)
    assert rig.reloads == ["all"]
    rig.poll(node_id, mac)                        # Stop
    rig.poll(node_id, mac, control=_control(rig, "atlas", 1500))
    assert rig.reloads == ["all", "atlas"]


# ── who reloads ──────────────────────────────────────────────────────────────


def test_only_live_stale_claude_desks_the_grant_covers_reload():
    desks = [("atlas", "claude"), ("scout", "claude"), ("oc", "opencode"),
             ("asleep", "claude"), ("fresh", "claude")]
    stale = {"atlas", "scout", "oc", "asleep"}.__contains__
    live = {"atlas", "scout", "oc", "fresh"}.__contains__
    assert mac_api.desks_to_reload("all", desks, stale=stale, live=live) == \
        ["atlas", "scout"]
    assert mac_api.desks_to_reload("atlas", desks, stale=stale, live=live) == \
        ["atlas"]
    assert mac_api.desks_to_reload("fresh", desks, stale=stale, live=live) == []


def test_the_reload_note_says_control_is_on_and_names_the_tools():
    note = mac_api.reload_note("MacBook Pro", None)
    assert "Mac control" in note and "MacBook Pro" in note
    assert "mcp__mac__click" in note and "mcp__mac__screenshot" in note


# ── the deck's wiring ────────────────────────────────────────────────────────


def test_the_deck_reloads_live_stale_desks_with_the_note(monkeypatch):
    from server import app as app_mod, roster

    class Desk:
        def __init__(self, name, engine="claude"):
            self.name, self.engine = name, engine

    class Now:   # run the detached reload inline
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    reloaded = []
    monkeypatch.setattr(roster, "load_roster",
                        lambda path: [Desk("atlas"), Desk("scout")])
    monkeypatch.setattr(mac_mcp, "stale", lambda name: name == "atlas")
    monkeypatch.setattr(app_mod.connectors_mod, "_live_job", lambda name: ("sid",))
    monkeypatch.setattr(app_mod.connectors_mod, "schedule_reload",
                        lambda name, note: reloaded.append((name, note)))
    monkeypatch.setattr(app_mod.threading, "Thread", Now)
    app_mod._mac_reload_stale("all")
    assert [n for n, _ in reloaded] == ["atlas"]
    assert "mcp__mac__click" in reloaded[0][1]
