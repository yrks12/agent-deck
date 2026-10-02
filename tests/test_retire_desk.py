"""A desk retires its own reports, and asleep desks hold no seat.

MEASURED on the box, 2026-10-01 19:19 UTC: Atlas wanted to hire YES NetOps,
believed every seat was full, filed a decision card naming three desks to
close, and then told the owner "I can't remove desks myself: right-click the
desk in the sidebar, then Delete". A dead end. `retire_desk` is the tool it did
not have: archive (never delete) one of its OWN reports, with the owner's tap
on a card naming that desk as the approval.

Hermetic: tmp bus, no daemon, no docker -- the machinery stop is a seam.
"""

import json

import pytest

from server import deck_mcp, decisions, hire, office, roster


def _desk(name, boss, cwd):
    return {"name": name, "cwd": str(cwd), "engine": "claude",
            "mission": name, "reports_to": boss}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    monkeypatch.setattr(decisions, "DEFAULT_PATH", tmp_path / "decisions.json")
    work = tmp_path / "workspaces" / "scout"
    work.mkdir(parents=True)
    (work / "transcript.jsonl").write_text("{\"said\": \"kept\"}\n")
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [
        _desk("atlas", None, tmp_path), _desk("scout", "atlas", work),
        _desk("deep", "scout", tmp_path), _desk("qa", "atlas", tmp_path)]}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", path)
    stopped = []
    monkeypatch.setattr(deck_mcp, "_stop_machinery", stopped.append)
    return tmp_path, path, stopped


def call(desk, **args):
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 1,
                                   "method": "tools/call",
                                   "params": {"name": "retire_desk",
                                              "arguments": args}})
    return json.loads(reply["result"]["content"][0]["text"])


def names(path):
    return {d.name for d in roster.load_roster(path)}


def approve(desk, answer):
    card = decisions.create(desk, "Which desk should I close?",
                            [f"Close {answer}", "Keep everyone"])
    decisions.answer(card["id"], f"Close {answer}")


def test_retire_archives_the_desk_and_deletes_nothing(bus):
    tmp, path, stopped = bus
    approve("atlas", "qa")
    out = call("atlas", name="qa", reason="its branding check is done")
    assert out["ok"] and out["retired"] is True, out
    assert "qa" not in names(path)
    archived = json.loads((tmp / "retired.json").read_text())["desks"]
    (row,) = [r for r in archived if r["name"] == "qa"]
    assert row["reports_to"] == "atlas" and row["retired_by"] == "atlas"
    assert row["reason"] == "its branding check is done"
    events = [json.loads(l) for l in (tmp / "events.jsonl").read_text().splitlines()]
    assert any(e["event"] == "retire" and e["name"] == "qa" for e in events)
    assert stopped == ["qa"], "the session and the browser must be stopped"


def test_a_retired_desk_keeps_its_workspace_and_transcript(bus):
    tmp, path, _ = bus
    roster.save_roster(path, [d for d in roster.load_roster(path) if d.name != "deep"])
    approve("atlas", "scout")
    assert call("atlas", name="scout", reason="done")["retired"] is True
    kept = tmp / "workspaces" / "scout" / "transcript.jsonl"
    assert kept.read_text() == "{\"said\": \"kept\"}\n"


def test_a_desk_retires_only_its_own_reports(bus):
    _, path, stopped = bus
    approve("atlas", "deep")
    out = call("atlas", name="deep", reason="not mine")
    assert out["ok"] is False and out["reason"] == "not_your_report"
    out = call("scout", name="atlas", reason="mutiny")
    assert out["reason"] == "not_your_report"
    assert call("atlas", name="atlas", reason="me")["reason"] == "not_your_report"
    assert names(path) == {"atlas", "scout", "deep", "qa"} and stopped == []


def test_without_approval_it_raises_one_card_and_retires_nobody(bus):
    _, path, stopped = bus
    first = call("atlas", name="qa", reason="idle all week")
    assert first["ok"] and first["retired"] is False and first["pending_approval"]
    again = call("atlas", name="qa", reason="idle all week")
    assert again["decision_id"] == first["decision_id"], "one card, not two"
    card = decisions.find(first["decision_id"])
    assert card["desk"] == "atlas" and card["state"] == "open"
    assert any("qa" in o["value"] for o in card["options"])
    assert "qa" in names(path) and stopped == []


def test_his_tap_on_the_card_is_the_approval(bus):
    _, path, _ = bus
    pending = call("atlas", name="qa", reason="idle")
    card = decisions.find(pending["decision_id"])
    yes = next(o["value"] for o in card["options"] if o["style"] == "danger")
    decisions.answer(card["id"], yes)
    assert call("atlas", name="qa", reason="idle")["retired"] is True
    assert "qa" not in names(path)


def test_keeping_the_desk_is_not_an_approval(bus):
    _, path, _ = bus
    card = decisions.create("atlas", "Close qa?", ["Retire qa", "Keep qa"])
    decisions.answer(card["id"], "Keep qa")
    assert call("atlas", name="qa", reason="idle")["retired"] is False
    assert "qa" in names(path)


def _seated(n, tmp):
    desks = [roster.Desk(name="atlas", cwd=str(tmp), engine="claude", mission="m")]
    desks += [roster.Desk(name=f"d{i}", cwd=str(tmp), engine="claude",
                          mission="m", reports_to="atlas") for i in range(n)]
    cards = [{"name": d.name, "session_id": f"s-{d.name}", "state": "IDLE"}
             for d in desks]
    return desks, cards


def test_a_freed_seat_lets_the_hire_succeed(bus, monkeypatch):
    tmp, path, _ = bus
    monkeypatch.setattr(hire, "mem_available_mb", lambda: None)
    desks, cards = _seated(hire.MAX_LIVE - 1, tmp)
    roster.save_roster(path, desks)
    live = roster.live_desks(roster.load_roster(path), cards)
    with pytest.raises(hire.HireError) as refused:
        hire.hire(path, name="netops", label="NetOps", charter="c", cwd=str(tmp),
                  engine="claude", reports_to="atlas", live_count=live)
    assert refused.value.reason == "too_many_live"
    approve("atlas", "d0")
    assert call("atlas", name="d0", reason="make room")["retired"] is True
    live = roster.live_desks(roster.load_roster(path), cards)
    hired = hire.hire(path, name="netops", label="NetOps", charter="c",
                      cwd=str(tmp), engine="claude", reports_to="atlas",
                      live_count=live)
    assert hired.name in names(path)


def test_asleep_desks_hold_no_seat(tmp_path):
    desks, cards = _seated(hire.MAX_LIVE + 6, tmp_path)
    asleep = cards[:3] + [{**c, "session_id": None, "state": "OFFLINE"}
                          for c in cards[3:]]
    live = roster.live_desks(desks, asleep)
    assert live == 3
    hire.can_hire(desks, boss="atlas", live_count=live, available_mb=8000)


def test_the_seat_cap_follows_free_ram_not_only_a_desk_count(tmp_path):
    desks, _ = _seated(2, tmp_path)
    with pytest.raises(hire.HireError) as refused:
        hire.can_hire(desks, boss="atlas", live_count=2,
                      available_mb=hire.DESK_MB + hire.RESERVE_MB - 1)
    assert refused.value.reason == "too_many_live"
    assert "MB" in refused.value.detail
    hire.can_hire(desks, boss="atlas", live_count=2,
                  available_mb=hire.DESK_MB + hire.RESERVE_MB)


def test_the_box_as_measured_has_room_for_one_more(tmp_path):
    """MEASURED on the box, 2026-10-01 19:55 UTC: 13 desks seated (most IDLE,
    waiting for the CLI to retire them after an hour) and 2046 MB available.
    RAM is the limit the owner named; a fixed count of ten refused this hire."""
    desks, _ = _seated(13, tmp_path)
    hire.can_hire(desks, boss="atlas", live_count=13, available_mb=2046)


def test_mem_available_reads_proc_meminfo(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemTotal:  8131784 kB\nMemAvailable:  4938308 kB\n")
    assert hire.mem_available_mb(info) == 4822
    assert hire.mem_available_mb(tmp_path / "missing") is None
