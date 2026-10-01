"""An install must TAKE EFFECT: the desk's MCP config is a file it re-reads.

MEASURED on the box (Claude Code 2.1.286, 2026-09-30, desk wake-probe):

* A running desk does not re-read its MCP config per message.
* `claude --bg --resume <sid>` re-applies the job's SAVED options. With
  `--mcp-config <inline JSON>` that is the old config forever. With
  `--mcp-config <file>`, a server renamed in the file between `claude stop`
  and `--resume` came back under its new name, same session id, and answered
  a real query.

So the spawn argv carries a FILE, and a reload is: wait for the desk's turn to
end, stop the job, resume the same session with a one-line note. A desk still
on an inline config (spawned before this) can only get the new config from a
fresh start, which the ordinary start path does (conversation replayed).
"""

from __future__ import annotations

import json
from pathlib import Path

from server import deck_mcp, paths, spawn
from server.roster import Desk
from tests.test_connectors import connectors

DESK = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m")


def test_the_spawn_argv_carries_the_mcp_config_as_a_file():
    argv = spawn.build_argv(DESK, background=True, seed="hi")
    value = argv[argv.index("--mcp-config") + 1]
    path = Path(value)
    assert path.is_absolute(), "an inline config is frozen into the job forever"
    assert json.loads(path.read_text()) == json.loads(deck_mcp.config("atlas"))
    # Outside the agent-bus: it is derived from the ledger, not product state.
    assert paths.BUS_DIR not in path.parents
    assert argv[argv.index("--mcp-config") + 2].startswith("--")
    assert argv[-1] == "hi"


def test_the_config_file_is_rewritten_from_the_ledger(tmp_path, monkeypatch):
    assert connectors is not None
    monkeypatch.setattr(connectors, "INSTALLED_PATH", tmp_path / "installed.json")
    path = Path(deck_mcp.config_file("atlas"))
    before = json.loads(path.read_text())["mcpServers"]
    (tmp_path / "installed.json").write_text(json.dumps({"version": 1, "desks": {
        "atlas": {"mcp:x/y": {"id": "mcp:x/y", "kind": "connector", "server": "y",
                              "recipe": {"type": "http", "url": "https://y.example/mcp"}}}}}))
    deck_mcp.config_file("atlas")
    after = json.loads(path.read_text())["mcpServers"]
    assert set(after) - set(before) == {"y"}
    assert after["y"] == {"type": "http", "url": "https://y.example/mcp"}


# ── what a reload does, per desk ─────────────────────────────────────────────

FILE_FLAGS = ["--name", "atlas", "--mcp-config", "/home/x/.claude/deck-mcp/atlas.json"]
INLINE_FLAGS = ["--name", "atlas", "--mcp-config", '{"mcpServers": {}}']


def test_the_plan_per_desk_state():
    assert connectors is not None
    plan = connectors.plan_reload
    assert plan("claude", live=True, flags=FILE_FLAGS) == "after_turn"
    assert plan("claude", live=True, flags=INLINE_FLAGS) == "restart_after_turn"
    assert plan("claude", live=False, flags=FILE_FLAGS) == "next_wake"
    assert plan("claude", live=False, flags=INLINE_FLAGS) == "restarting"
    assert plan("claude", live=False, flags=None) == "next_wake"
    assert plan("opencode", live=True, flags=None) == "none"


class Box:
    """The impure edges of a reload, scripted."""

    def __init__(self, states, flags=FILE_FLAGS, live=True, still_running=0):
        self.states = list(states)
        self.still_running = still_running
        self.flags = flags
        self.live = live
        self.did: list[tuple] = []

    def reloader(self):
        return connectors.Reloader(
            desk_of=lambda name: DESK,
            live_job=lambda name: (("sid-1", "short1", "/tmp", self.flags)
                                   if self.live else None),
            last_flags=lambda name: self.flags,
            state=lambda name: self.states.pop(0) if self.states else "IDLE",
            stop=lambda short: self.did.append(("stop", short)) or True,
            resume=lambda sid, cwd, seed: self.did.append(("resume", sid, seed)) or "short2",
            restart=lambda desk: self.did.append(("restart", desk.name)) or {},
            sleep=lambda s: self.did.append(("sleep",)),
            running=self._running,
            wait_max=60, poll=1)


    def _running(self, sid):
        self.did.append(("running?", sid))
        if self.still_running > 0:
            self.still_running -= 1
            return True
        return False


def test_a_live_desk_is_resumed_as_itself_only_after_its_turn_ends():
    box = Box(["WORKING", "WORKING", "IDLE"])
    out = box.reloader().run("atlas", "Connector added: mslearn")
    acts = [d[0] for d in box.did if d[0] != "running?"]
    assert acts == ["sleep", "sleep", "stop", "resume"]
    assert box.did[-1][1] == "sid-1"
    assert "mslearn" in box.did[-1][2]
    assert out["action"] == "resumed"


def test_a_live_desk_on_an_inline_config_is_restarted_after_its_turn():
    box = Box(["WORKING", "DONE"], flags=INLINE_FLAGS)
    out = box.reloader().run("atlas", "note")
    assert [d[0] for d in box.did] == ["sleep", "restart"]
    assert out["action"] == "restarted"


def test_a_sleeping_desk_is_left_to_its_next_wake():
    box = Box([], live=False)
    out = box.reloader().run("atlas", "note")
    assert box.did == [] and out["action"] == "next_wake"


def test_a_desk_that_never_goes_idle_is_not_cut_off_mid_turn():
    box = Box(["WORKING"] * 500)
    out = box.reloader().run("atlas", "note")
    assert "stop" not in [d[0] for d in box.did]
    assert out["action"] == "gave_up"


def test_the_resume_waits_until_the_stopped_session_has_really_exited():
    """MEASURED on the box, 2026-09-30: `--resume` one second after `claude
    stop` found the session still running and "started a copy" -- a new
    session id, an auto name ("brand-guidelines install") and
    `--permission-mode default`, i.e. a desk the board could no longer see.
    The resume must wait for the old process to be gone."""
    box = Box(["IDLE"], still_running=3)
    out = box.reloader().run("atlas", "note")
    acts = [d[0] for d in box.did]
    assert acts.index("stop") < acts.index("resume")
    between = acts[acts.index("stop") + 1:acts.index("resume")]
    assert between.count("running?") == 4 and between.count("sleep") == 3
    assert out["action"] == "resumed"


def test_a_session_that_will_not_exit_is_not_resumed_into_a_copy():
    box = Box(["IDLE"], still_running=10_000)
    out = box.reloader().run("atlas", "note")
    assert "resume" not in [d[0] for d in box.did]
    assert out["action"] == "stopped"
