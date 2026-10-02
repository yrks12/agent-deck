"""The deck's own tools, inside a desk's session (K5): say, ask, message_desk.

The defects this closes:

* A desk spoke only at the end of a turn (`harvest` posts turn-final prose),
  so a three-minute turn was three minutes of silence -- no "on it".
* A question was prose; there was no way to put buttons in front of him.
* A desk could reach a sleeping peer only through Claude's own SendMessage,
  which MEASURED on the box (docs/wake.md #6) errors on a retired session
  rather than queueing: "No agent named 'wake-sink' is reachable."

Same shape as `server/computer_mcp.py`: a stdio MCP server whose desk name is
written into the spawn argv, so no tool takes a desk field and nothing the
model sends can speak as another desk.

Hermetic: tmp bus dir, no daemon, no CLI, injected socket and waker.
"""

import json
from pathlib import Path

import pytest

from server import computer_mcp, deck_mcp, decisions, office, spawn, wake
from server.roster import Desk

CHIEF = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
         "reports_to": None}
JUNIOR = {"name": "scout", "cwd": "/tmp/p", "engine": "claude",
          "mission": "look", "reports_to": "atlas"}
PROBE = {"name": "wake-probe", "cwd": "/tmp/p", "engine": "claude",
         "mission": "probe", "reports_to": "atlas"}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1,
                                  "agents": [CHIEF, JUNIOR, PROBE]}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    return tmp_path


def records(bus):
    text = (bus / "messages.jsonl").read_text() if (
        bus / "messages.jsonl").exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def tool(desk, which, **args):
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 7,
                                   "method": "tools/call",
                                   "params": {"name": which,
                                              "arguments": args}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


# ── wiring: one --mcp-config, both servers, the name bound in argv ──────────


def _configs(argv):
    # The value is a FILE (tests/test_connectors_reload.py): its content.
    return [Path(argv[i + 1]).read_text() for i, a in enumerate(argv)
            if a == "--mcp-config"]


def test_a_claude_desk_gets_one_mcp_config_with_computer_and_deck():
    argv = spawn.build_argv(Desk(name="atlas", cwd="/tmp", engine="claude",
                                 mission="m"), background=True, seed="hi")
    (config,) = _configs(argv)
    servers = json.loads(config)["mcpServers"]
    assert set(servers) == {"computer", "deck", "mac"}
    deck = servers["deck"]
    assert deck["type"] == "stdio"
    assert deck["args"][:2] == ["-m", "server.deck_mcp"]
    assert deck["args"][-2:] == ["--desk", "atlas"]
    # `--mcp-config` is variadic: a flag must follow it, the seed stays last.
    i = argv.index("--mcp-config")
    assert argv[i + 2].startswith("--") and argv[-1] == "hi"


def test_a_name_no_container_can_carry_still_gets_the_deck_tools():
    argv = spawn.build_argv(Desk(name="travel scout", cwd="/tmp",
                                 engine="claude", mission="m"),
                            background=True)
    (config,) = _configs(argv)
    servers = json.loads(config)["mcpServers"]
    assert set(servers) == {"deck", "mac"}
    assert servers["deck"]["args"][-1] == "travel scout"


def test_the_computer_config_comes_from_the_shared_builder():
    alone = json.loads(computer_mcp.config("atlas"))["mcpServers"]
    both = json.loads(deck_mcp.config("atlas"))["mcpServers"]
    assert alone["computer"] == both["computer"]


def test_no_deck_tool_takes_a_desk_field():
    listed = deck_mcp.handle("atlas", {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert set(tools) == {"say", "ask", "call_owner", "message_desk", "store_search",
                          "store_install", "set_my_look", "save_lesson",
                          "save_skill", "retire_desk", "send_file",
                          "history", "chronicle", "log_money", "my_money",
                          "propose_standing_approval"}
    for spec in tools.values():
        assert "desk" not in spec["inputSchema"]["properties"]


def test_initialize_names_the_deck_server():
    reply = deck_mcp.handle("atlas", {"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize", "params": {}})
    assert reply["result"]["serverInfo"]["name"] == "deck"
    assert deck_mcp.handle("atlas", {"jsonrpc": "2.0",
                                     "method": "notifications/initialized"}
                           ) is None


# ── say ──────────────────────────────────────────────────────────────────────


def test_the_chief_says_into_the_owner_thread_now(bus):
    out, err = tool("atlas", "say", text="On it -- checking Acme first.")
    assert out == {"ok": True} and not err
    (rec,) = records(bus)
    assert (rec["to"], rec["from"]) == ("owner", "atlas")
    assert rec["text"] == "On it -- checking Acme first."


def test_a_junior_says_to_its_boss(bus):
    out, _ = tool("scout", "say", text="Found three leads, writing up.")
    assert out["ok"]
    (rec,) = records(bus)
    assert (rec["to"], rec["from"]) == ("atlas", "scout")


@pytest.mark.parametrize("text,reason", [("", "empty_text"),
                                          ("x" * 601, "too_long")])
def test_say_is_one_short_line(bus, text, reason):
    out, err = tool("atlas", "say", text=text)
    assert err and out == {"ok": False, "reason": reason,
                           "detail": out["detail"]}
    assert records(bus) == []


# ── ask ──────────────────────────────────────────────────────────────────────


def test_the_chief_asks_and_a_decision_card_lands(bus):
    out, err = tool("atlas", "ask", prompt="Headline?",
                    options=[{"label": "Bold", "style": "primary"},
                             {"label": "Calm"}],
                    help="Two drafts.", allow_custom=False)
    assert not err and out["ok"]
    card = decisions.find(out["decision_id"], path=bus / "decisions.json")
    assert card["desk"] == "atlas" and card["allow_custom"] is False
    (rec,) = records(bus)
    assert rec["kind"] == "decision" and rec["from"] == "atlas"
    assert rec["decision"]["id"] == out["decision_id"]


def test_a_junior_is_told_to_ask_its_boss(bus):
    out, err = tool("scout", "ask", prompt="Which?", options=["a", "b"])
    assert out["ok"] is False and out["reason"] == "ask_your_boss"
    assert records(bus) == []


def test_a_bad_card_comes_back_with_its_reason(bus):
    out, err = tool("atlas", "ask", prompt="Which?", options=["only one"])
    assert err and out["reason"] == "bad_options"


# ── message_desk ─────────────────────────────────────────────────────────────


def test_a_peer_with_a_live_socket_gets_it_now(bus, monkeypatch):
    sent, woken = [], []
    monkeypatch.setattr(deck_mcp, "_inject_live",
                        lambda name, text: sent.append((name, text)) or True)
    monkeypatch.setattr(deck_mcp, "_wake", lambda name, reason: woken.append(
        (name, reason)))
    out, err = tool("atlas", "message_desk", name="scout", text="status?")
    assert not err
    assert out == {"ok": True, "delivered": True, "woke": False,
                   "state": "live"}
    rec, ack = records(bus)
    assert (rec["to"], rec["from"], rec["text"]) == ("scout", "atlas",
                                                     "status?")
    assert ack == {"ts": ack["ts"], "ack": rec["id"]}
    # Framed as a peer, the way the office hook would frame it.
    assert sent[0][0] == "scout" and "status?" in sent[0][1]
    assert sent[0][1].startswith(office.MARK)
    assert woken == []


def test_a_sleeping_peer_is_woken_as_a_peer_message(bus, monkeypatch):
    monkeypatch.setattr(deck_mcp, "_inject_live", lambda name, text: False)
    calls = []

    def fake_wake(name, reason):
        calls.append((name, reason))
        return wake.WakeResult("woken", "de8b1457")

    monkeypatch.setattr(deck_mcp, "_wake", fake_wake)
    out, err = tool("atlas", "message_desk", name="wake-probe",
                    text="reply to atlas with pong")
    assert not err
    assert out == {"ok": True, "delivered": False, "woke": True,
                   "state": "woken"}
    assert calls == [("wake-probe", "peer_message")]
    (rec,) = records(bus)  # queued; the wake carries it in its seed
    assert rec["to"] == "wake-probe" and rec["from"] == "atlas"


def test_a_refused_wake_says_why_and_the_message_stays_queued(
        bus, monkeypatch):
    monkeypatch.setattr(deck_mcp, "_inject_live", lambda name, text: False)
    monkeypatch.setattr(deck_mcp, "_wake", lambda name, reason:
                        wake.WakeResult("refused", "", "oauth_expired: x"))
    out, _ = tool("atlas", "message_desk", name="scout", text="hi")
    assert out["ok"] is True and out["woke"] is False
    assert out["state"] == "refused" and out["detail"].startswith(
        "oauth_expired")
    assert len(records(bus)) == 1


@pytest.mark.parametrize("name,reason", [("nobody", "unknown_desk"),
                                         ("atlas", "that_is_you")])
def test_message_desk_refuses_a_stranger_and_itself(bus, monkeypatch, name,
                                                    reason):
    monkeypatch.setattr(deck_mcp, "_inject_live", lambda n, t: True)
    out, err = tool("atlas", "message_desk", name=name, text="hi")
    assert err and out["reason"] == reason
    assert records(bus) == []


def test_the_peer_message_shows_in_both_threads(bus, monkeypatch):
    """What the owner sees: the dispatch in the peer thread, relayed."""
    from fastapi import FastAPI  # noqa: F401  (surface only)
    from server import api as api_mod
    from server.sources import comms as comms_mod

    monkeypatch.setattr(deck_mcp, "_inject_live", lambda n, t: False)
    monkeypatch.setattr(deck_mcp, "_wake", lambda n, r:
                        wake.WakeResult("live", "s"))
    tool("atlas", "message_desk", name="scout", text="status?")
    surface = api_mod.Surface(snapshot=lambda: {"sessions": []},
                              comms=comms_mod.CommsIndex(),
                              roster_path=bus / "roster.json",
                              prefs_path=bus / "prefs.json")
    surface.refresh()
    assert [m["text"] for m in surface.messages_for("peer:atlas|scout")] == [
        "status?"]
    assert [m["text"] for m in surface.messages_for("direct:scout")] == [
        "status?"]


@pytest.mark.parametrize("first", ["server.spawn", "server.wake",
                                   "server.deck_mcp"])
def test_no_import_order_cycles(first):
    """spawn -> deck_mcp -> (wake -> spawn): whichever loads first, a fresh
    interpreter must import it. A cycle here is a desk that cannot start."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-c", f"import {first}"], cwd=root,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-400:]


def test_the_deck_tools_run_without_an_approval_card():
    """MEASURED on the box, 2026-09-30, fresh `wake-probe` under auto mode:
    its first `mcp__deck__say` and `mcp__deck__ask` each raised an approval
    card for the owner (asks x22qu, cxytn) -- auto mode does not clear a
    desk's own MCP tools. A desk asking him permission to say "on it" is the
    stall this whole slice exists to remove. The deck's own server is allowed
    by name, and ONLY it: the computer's tools keep whatever the classifier
    and the approver decide."""
    from server import approval
    permissions = approval.settings_document("/usr/bin/node")["permissions"]
    assert permissions["allow"] == ["mcp__deck"]
    assert not any("computer" in rule for rule in permissions["allow"])


# -- set_my_look: a desk changes its own avatar -------------------------------


def _desks(bus):
    from server.roster import load_roster
    return {d.name: d for d in load_roster(bus / "roster.json")}


def test_set_my_look_persists_on_the_roster(bus):
    out, err = tool("scout", "set_my_look", shape="cloud", color=7,
                    label="Scout the Bold", voice={"id": "", "rate": 1.2})
    assert not err and out["ok"] is True
    desk = _desks(bus)["scout"]
    assert desk.avatar_look == {"shape": "cloud", "color": 7}
    assert desk.label == "Scout the Bold"
    assert desk.voice == {"id": "", "rate": 1.2}


def test_set_my_look_shows_in_v1_agents(bus):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server import api as api_mod
    from server.sources import comms as comms_mod
    surface = api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=bus / "roster.json",
        prefs_path=bus / "prefs.json")
    import os
    os.environ[api_mod.TOKEN_ENV] = "t"
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    client = TestClient(app)
    tool("scout", "set_my_look", shape="wedge", color=3)
    rows = client.get("/v1/agents",
                      headers={"Authorization": "Bearer t"}).json()["agents"]
    scout = next(r for r in rows if r["name"] == "scout")
    assert scout["avatar_look"] == {"shape": "wedge", "color": 3}


def test_set_my_look_rejects_bad_values_and_lists_the_options(bus):
    out, err = tool("scout", "set_my_look", shape="dodecahedron", color=1)
    assert err and out["reason"] == "bad_look"
    assert "hexagon" in out["detail"] and "0-11" in out["detail"]
    out, err = tool("scout", "set_my_look", shape="blob", color=99)
    assert err and out["reason"] == "bad_look"
    assert _desks(bus)["scout"].avatar_look is None


def test_set_my_look_needs_something_to_change(bus):
    out, err = tool("scout", "set_my_look")
    assert err and out["reason"] == "nothing_to_change"


def test_a_desk_cannot_change_another_desk(bus):
    out, err = tool("scout", "set_my_look", name="atlas", shape="blob",
                    color=1)
    assert err and out["reason"] == "not_your_report"
    assert _desks(bus)["atlas"].avatar_look is None


def test_the_chief_may_change_a_report(bus):
    out, err = tool("atlas", "set_my_look", name="scout", shape="pebble",
                    color=2)
    assert not err
    assert _desks(bus)["scout"].avatar_look == {"shape": "pebble", "color": 2}


def test_the_chief_may_not_change_a_stranger(bus):
    out, err = tool("atlas", "set_my_look", name="nobody", shape="blob",
                    color=1)
    assert err and out["reason"] == "unknown_desk"
