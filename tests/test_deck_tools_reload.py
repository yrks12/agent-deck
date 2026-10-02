"""A deploy that changes the deck's tools reaches every live desk by itself.

MEASURED 2026-10-02 on the box: `call_owner` shipped, the owner switched on
"Call me now" and told Atlas "call once, I wanna see if it works". Atlas:
"I can't call you right now: the calling tool isn't loaded in my session...
It'll probably appear after the next restart of my desk." The same class bit
`set_my_look`, `history`/`chronicle`, `send_file` and the Mac control tools
(that one fixed alone, tests/test_mac_control_reload.py).

* The sweep reloads every LIVE Claude desk whose deck (or mac) tools are
  stale -- through the connectors' reloader, which waits for the turn to end
  and respawns in place -- once per tool set, never twice in a row.
* A desk's `--mcp-config` file written by older code (a new server, a new
  flag) is rewritten and that desk reloaded too: a respawn re-reads the file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server import deck_mcp, mac_mcp, tool_reload, tool_stamp


def test_the_note_names_the_tools_the_old_process_lacked(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_mcp, "STAMP_ROOT", tmp_path)
    old = [t for t in deck_mcp.TOOLS if t["name"] != "call_owner"]
    tool_stamp.write("deck", "atlas", tool_stamp.signature(old),
                     names=[t["name"] for t in old], root=tmp_path)
    assert tool_reload.missing_tools("atlas") == ["call_owner"]
    assert "call_owner" in tool_reload.note_for("atlas")


# ── the sweep ────────────────────────────────────────────────────────────────


class Rig:
    def __init__(self, now=1000.0):
        self.now = now
        self.desks = [SimpleNamespace(name="atlas", engine="claude"),
                      SimpleNamespace(name="scout", engine="claude"),
                      SimpleNamespace(name="sleepy", engine="claude"),
                      SimpleNamespace(name="oc", engine="opencode")]
        self.live = {"atlas", "scout", "oc"}
        self.stale = {"atlas": "deck:new", "sleepy": "deck:new", "oc": "deck:new"}
        self.reloads: list[tuple[str, str]] = []

    def sweeper(self, **kw):
        return tool_reload.Sweeper(
            desks=lambda: self.desks, live=lambda n: n in self.live,
            checks=[lambda n: self.stale.get(n)],
            reload=lambda n, note: self.reloads.append((n, note)),
            clock=lambda: self.now, **kw)


def test_only_live_claude_desks_with_stale_tools_reload():
    rig = Rig()
    assert rig.sweeper().tick() == ["atlas"]
    assert [n for n, _ in rig.reloads] == ["atlas"]
    note = rig.reloads[0][1]
    assert "restart" not in note.lower() or "never" in note.lower()


def test_one_reload_per_tool_set_not_one_per_sweep():
    rig = Rig()
    sweep = rig.sweeper(retry=1800.0)
    sweep.tick()
    rig.now += 60
    assert sweep.tick() == []
    rig.now += 1800
    assert sweep.tick() == ["atlas"]       # still stale after the wait: again
    rig.stale["atlas"] = "deck:newer"     # another deploy: at once
    rig.now += 1
    assert sweep.tick() == ["atlas"]


def test_a_desk_that_never_heals_is_not_respawned_forever():
    rig = Rig()
    sweep = rig.sweeper(retry=10.0, max_tries=3)
    for _ in range(10):
        sweep.tick()
        rig.now += 11
    assert len(rig.reloads) == 3


def test_a_fresh_desk_is_left_alone():
    rig = Rig()
    rig.stale = {}
    assert rig.sweeper().tick() == []


# ── the --mcp-config file ────────────────────────────────────────────────────


def test_a_config_file_from_older_code_is_rewritten_and_counts_as_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_mcp, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(deck_mcp, "config", lambda desk, ledger_path=None:
                        json.dumps({"mcpServers": {"deck": {}, "mac": {}}}))
    path = deck_mcp.config_path("atlas")
    assert tool_reload.config_drift("atlas") is None   # no file: never spawned here
    path.write_text(json.dumps({"mcpServers": {"deck": {}}}))
    assert tool_reload.config_drift("atlas") is not None
    assert json.loads(path.read_text()) == {"mcpServers": {"deck": {}, "mac": {}}}
    assert tool_reload.config_drift("atlas") is None


# ── the default checks cover deck and mac ────────────────────────────────────


def test_the_default_checks_see_a_stale_deck_or_mac_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_mcp, "STAMP_ROOT", tmp_path / "deck")
    monkeypatch.setattr(mac_mcp, "STAMP_ROOT", tmp_path / "mac")
    monkeypatch.setattr(deck_mcp, "CONFIG_DIR", tmp_path / "cfg")
    deck_mcp.write_stamp("atlas")
    mac_mcp.write_stamp("atlas")
    assert [c("atlas") for c in tool_reload.default_checks()] == [None, None, None]
    tool_stamp.write("deck", "atlas", "old", root=tmp_path / "deck")
    assert tool_reload.default_checks()[0]("atlas") == f"deck:{deck_mcp.TOOLS_SIG}"




def test_the_loop_waits_for_boot_then_sweeps_and_survives_a_failed_tick():
    sleeps, ticks, logs = [], [], []

    class Boom(Exception):
        pass

    class S:
        def tick(self):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("roster unreadable")
            return ["atlas"]

    def sleep(secs):
        sleeps.append(secs)
        if len(ticks) >= 2:
            raise Boom

    with pytest.raises(Boom):
        tool_reload.sweep_forever(S(), sleep=sleep, log=logs.append)
    assert sleeps[0] == tool_reload.BOOT_SECONDS
    assert "sweep failed" in logs[0] and "atlas" in logs[1]
