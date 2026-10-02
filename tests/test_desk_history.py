"""A desk can read its own record since day one, whatever its session forgot.

MEASURED 2026-10-01: Atlas's thread holds 1,222 messages back to 2026-09-06;
its restart replayed the newest 77 and it told the owner the rest was gone.
`server/history.py` reads the deck's own record instead. Hermetic: tmp bus.
"""

import json

import pytest

from server import history, learning, office

DAY1 = 1788700000.0          # 2026-09-06
DAY2 = DAY1 + 86400 * 20
ROSTER = [
    {"name": "atlas", "cwd": "/tmp/a", "engine": "claude", "mission": "run",
     "reports_to": None},
    {"name": "scout", "cwd": "/tmp/s", "engine": "claude", "mission": "look",
     "reports_to": "atlas"},
]


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(learning, "TEAM_DIR", tmp_path / "team-memory")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1, "agents": ROSTER}))
    monkeypatch.setattr(history, "ROSTER_PATH", roster)
    (tmp_path / "desk_aliases.json").write_text(json.dumps(
        {"version": 1, "renames": {"new-hire-77": "portfolio-lead",
                                   "portfolio-lead": "atlas"}}))
    msgs = [
        {"ts": DAY1, "id": "a", "to": "new-hire-77", "from": "owner",
         "text": "Run the real estate video software"},
        {"ts": DAY1 + 60, "id": "b", "to": "owner", "from": "new-hire-77",
         "text": "On it: acme first"},
        {"ts": DAY1 + 120, "ack": "a"},
        {"ts": DAY2, "id": "c", "to": "scout", "from": "atlas",
         "text": "Check the acme signup"},
        {"ts": DAY2 + 60, "id": "d", "to": "owner", "from": "scout",
         "text": "Signup works"},
    ]
    office.MESSAGES_FILE.write_text("".join(json.dumps(m) + "\n" for m in msgs))
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": DAY1 - 10, "event": "hire", "name": "new-hire-77",
         "reports_to": None, "by": "owner"},
        {"ts": DAY2 - 10, "event": "hire", "name": "scout",
         "reports_to": "atlas", "by": "atlas"},
        {"ts": DAY2, "event": "approval", "session_id": "x"},
    ]))
    return tmp_path


def texts(out):
    return [e["text"] for e in out["entries"]]


def test_the_chief_reads_back_to_day_one_through_its_renames(bus):
    out = history.search("atlas", since="2026-09-01")
    assert out["first"] == "2026-09-06"
    assert texts(out)[:3] == ["hired new-hire-77 (reports to the owner)",
                              "Run the real estate video software",
                              "On it: acme first"]
    assert "Signup works" in texts(out)          # the whole team


def test_a_desk_reads_only_its_own(bus):
    out = history.search("scout")
    assert set(texts(out)) == {"hired scout (reports to atlas)",
                               "Check the acme signup", "Signup works"}
    with pytest.raises(history.HistoryError) as exc:
        history.search("scout", desk="atlas")
    assert exc.value.reason == "own_only"


def test_query_kind_and_paging(bus):
    assert texts(history.search("atlas", query="ACME signup")) == [
        "Check the acme signup"]
    assert texts(history.search("atlas", kind="team")) == [
        "hired new-hire-77 (reports to the owner)",
        "hired scout (reports to atlas)"]
    first = history.search("atlas", since=0, limit=2)
    assert first["more"] and len(first["entries"]) == 2
    rest = history.search("atlas", since=first["next"]["since"], limit=50)
    assert len(first["entries"]) + len(rest["entries"]) == first["matched"]
    newest = history.search("atlas", limit=1)
    assert texts(newest) == ["Signup works"] and "until" in newest["next"]


def test_lessons_are_part_of_the_record(bus):
    learning._write_lesson(bus / "team-memory", "Do it now", "Never defer",
                           "He said so", "Answer in the same reply", "scout",
                           "team")
    out = history.search("scout", kind="lesson")
    assert texts(out) and texts(out)[0].startswith("Do it now")


def test_the_desk_tools_serve_it(bus, monkeypatch):
    from server import chronicle, deck_mcp
    monkeypatch.setattr(chronicle, "PATH", bus / "chronicle.json")

    def tool(desk, which, **args):
        reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/call",
                                       "params": {"name": which,
                                                  "arguments": args}})
        return json.loads(reply["result"]["content"][0]["text"])

    out = tool("atlas", "history", since="2026-09-06", limit=2)
    assert out["ok"] and out["first"] == "2026-09-06" and out["more"]
    assert tool("atlas", "chronicle")["first"] == "2026-09-06"
    refused = tool("scout", "history", about="atlas")
    assert refused == {"ok": False, "reason": "own_only",
                       "detail": refused["detail"]}
