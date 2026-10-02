"""A desk whose live session lost its computer tools is respawned in place.

MEASURED on the box, 2026-10-01, desk atlas: after a deploy its computer MCP
server died and Claude reconnected a fresh one but dropped every
`mcp__computer__*` tool from the session's list. Nothing noticed; the desk
told the owner its browser was gone for an hour.

The session transcript records it: each `deferred_tools_delta` attachment
lists the tools added, removed and re-added, and which MCP servers are
pending or failed. Replaying them says whether the tools are there NOW.
`claude respawn` (same job, same session, no copy) puts them back; it is done
between turns, never in the middle of one.
"""

import json

from server import computer_heal as heal

T0 = 1_000_000.0
TOOLS = ["mcp__computer__click", "mcp__computer__navigate"]


def delta(at, *, added=(), removed=(), readded=(), pending=(), failed=()):
    return {"at": at, "added": list(added), "removed": list(removed),
            "readded": list(readded), "pending": list(pending),
            "failed": list(failed)}


# ── the detector ────────────────────────────────────────────────────────────


def test_tools_present_after_the_first_delta_are_not_lost():
    assert heal.lost([delta(T0, added=TOOLS)], now=T0 + 60) is None


def test_a_session_with_no_deltas_yet_is_not_judged():
    assert heal.lost([], now=T0) is None


def test_the_server_marked_failed_is_lost():
    why = heal.lost([delta(T0, added=TOOLS),
                     delta(T0 + 5, removed=TOOLS, failed=["computer"])],
                    now=T0 + 60)
    assert why == "server_failed"


def test_tools_removed_and_never_readded_are_lost():
    why = heal.lost([delta(T0, added=TOOLS),
                     delta(T0 + 5, removed=TOOLS, pending=["computer", "deck"]),
                     delta(T0 + 9, added=["mcp__deck__ask"])], now=T0 + 600)
    assert why == "tools_missing"


def test_a_reconnect_in_progress_is_given_time():
    records = [delta(T0, added=TOOLS),
               delta(T0 + 5, removed=TOOLS, pending=["computer"])]
    assert heal.lost(records, now=T0 + 20) is None
    assert heal.lost(records, now=T0 + heal.PENDING_MAX + 10) == "tools_missing"


def test_tools_readded_after_a_reconnect_are_healthy():
    records = [delta(T0, added=TOOLS),
               delta(T0 + 5, removed=TOOLS, pending=["computer"]),
               delta(T0 + 9, readded=TOOLS)]
    assert heal.lost(records, now=T0 + 900) is None


def test_a_failed_server_that_came_back_is_healthy():
    records = [delta(T0, failed=["computer"]),
               delta(T0 + 30, added=TOOLS)]
    assert heal.lost(records, now=T0 + 900) is None


def test_other_servers_failing_is_not_ours_to_fix():
    assert heal.lost([delta(T0, added=TOOLS, failed=["mac"])],
                     now=T0 + 60) is None


def test_records_are_read_from_a_transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [
        {"type": "user"},
        {"type": "attachment", "timestamp": "1970-01-12T13:46:40.000Z",
         "attachment": {"type": "deferred_tools_delta", "addedNames": TOOLS,
                        "removedNames": [], "readdedNames": [],
                        "pendingMcpServers": [], "failedMcpServers": []}},
        {"type": "attachment", "timestamp": "1970-01-12T13:46:50.000Z",
         "attachment": {"type": "deferred_tools_delta", "addedNames": [],
                        "removedNames": TOOLS, "readdedNames": [],
                        "pendingMcpServers": [],
                        "failedMcpServers": ["computer"]}},
        {"type": "attachment", "attachment": {"type": "skill_listing"}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    records = heal.read_records(path)
    assert [r["failed"] for r in records] == [[], ["computer"]]
    assert records[0]["at"] == 1_000_000.0
    assert heal.lost(records, now=T0 + 60) == "server_failed"


# ── when it acts ────────────────────────────────────────────────────────────


class Box:
    def __init__(self, *, state="idle", job_state="idle", records=None,
                 flight=None):
        self.job = {"name": "atlas", "state": job_state, "sessionId": "sid-1",
                    "daemonShort": "short1", "inFlight": flight or {}}
        self.desk_state = state
        self.records = records if records is not None else [
            delta(T0, added=TOOLS), delta(T0 + 5, removed=TOOLS,
                                          failed=["computer"])]
        self.did = []

    def heal(self, now=T0 + 3600, memory=None):
        return heal.sweep(
            now=now,
            jobs=lambda: [self.job],
            records=lambda job: self.records,
            desk_state=lambda name: self.desk_state,
            respawn=lambda short: self.did.append(short) or True,
            memory={} if memory is None else memory)


def test_a_desk_between_turns_with_lost_tools_is_respawned():
    box = Box()
    assert box.heal() == {"atlas": "respawned"}
    assert box.did == ["short1"]


def test_a_desk_mid_turn_is_never_respawned():
    for state in ("WORKING", "NEEDS_YOU"):
        box = Box(state=state)
        assert box.heal() == {}
        assert box.did == []


def test_a_job_that_is_not_idle_or_done_is_left_alone():
    for job_state in ("working", "blocked", "stopped"):
        box = Box(job_state=job_state)
        assert box.heal() == {} and box.did == []


def test_a_desk_with_a_task_in_flight_is_left_alone():
    box = Box(flight={"tasks": 3, "drainableMonitors": 1})
    assert box.heal() == {} and box.did == []


def test_background_monitors_alone_do_not_block_a_respawn():
    box = Box(flight={"tasks": 4, "drainableMonitors": 4})
    assert box.heal() == {"atlas": "respawned"}


def test_a_healthy_desk_is_left_alone():
    box = Box(records=[delta(T0, added=TOOLS)])
    assert box.heal() == {} and box.did == []


def test_one_respawn_per_cooldown_not_a_loop():
    box, memory = Box(), {}
    box.heal(now=T0 + 3600, memory=memory)
    assert box.heal(now=T0 + 3700, memory=memory) == {}
    assert box.did == ["short1"]
    assert box.heal(now=T0 + 3600 + heal.COOLDOWN + 1,
                    memory=memory) == {"atlas": "respawned"}


def test_a_respawn_that_fails_is_reported_and_retried_after_the_cooldown():
    box = Box()
    out = heal.sweep(now=T0 + 3600, jobs=lambda: [box.job],
                     records=lambda job: box.records,
                     desk_state=lambda n: box.desk_state,
                     respawn=lambda short: False, memory={})
    assert out == {"atlas": "respawn_failed"}


def test_the_heal_never_uses_the_forking_resume():
    import inspect
    src = inspect.getsource(heal)
    assert "resume_background" not in src
    assert "--resume" not in src


def test_the_daemon_runs_the_heal_on_its_own_loop():
    import inspect

    from server import api
    assert "computer_heal.sweep" in inspect.getsource(api.register)
