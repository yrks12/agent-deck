"""A desk moved to another Claude account is told what it left behind.

MEASURED on the box 2026-10-01: after the main -> work failover,
a social desk's dashboard write answered "no such artifact or no access".
The artifact was main's; work read it as a 404; main could not share it
(PATCH /api/frame/perm: 403 "this org type may only set read.mode=owner").
The desk only found out on its next write and logged the events locally.

Detector: the mover reads the session's Artifact tool calls and tells the desk,
at the move, which artifacts stayed behind and what to do -- and the brief
carries the same rule for a desk that hits it later.
"""

from __future__ import annotations

import json

from server import accounts, acct_scoped, features
from server.paths import slug_for
from tests.test_accounts_move import SID, Rig, reg  # noqa: F401 - fixture

OLD = "https://claude.ai/artifact/NwvJhgK9RkhUJnF85AP3q4"
MADE = "https://claude.ai/artifact/SZyCHw7WxUo3LwKUWWxYeQ"
SAID = "https://claude.ai/artifact/PCbG5gMf43wjmx2Hv8g4g9"


def _line(content: list) -> str:
    return json.dumps({"message": {"role": "assistant", "content": content}})


def _transcript(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        _line([{"type": "tool_use", "id": "t1", "name": "ArtifactData",
                "input": {"action": "set", "url": OLD, "collection": "events"}}]),
        _line([{"type": "tool_use", "id": "t2", "name": "Artifact",
                "input": {"action": "publish", "file_path": "/w/x.html"}}]),
        _line([{"type": "tool_result", "tool_use_id": "t2",
                "content": f"Published: {MADE}"}]),
        _line([{"type": "text", "text": f"the owner said look at {SAID}"}]),
        _line([{"type": "tool_use", "id": "t3", "name": "ArtifactData",
                "input": {"action": "query", "url": OLD}}]),
        "not json",
    ]))


def test_only_links_in_artifact_tool_calls_count_first_seen_first(tmp_path):
    t = tmp_path / "s.jsonl"
    _transcript(t)
    assert acct_scoped.artifacts_touched(t) == [OLD, MADE]
    assert acct_scoped.artifacts_touched(tmp_path / "missing.jsonl") == []


def _account(tmp_path, ident, mcp):
    acct = accounts.Account(ident, ident.title(), "subscription", tmp_path / ident)
    acct.config_dir.mkdir(parents=True)
    accounts.dirs(acct).global_config.write_text(json.dumps({"mcpServers": mcp}))
    return acct


def test_the_note_names_each_artifact_and_the_user_mcp_left_behind(tmp_path):
    src = _account(tmp_path, "old", {"gmail": {}, "deck": {}})
    dst = _account(tmp_path, "work", {"deck": {}})
    _transcript(accounts.dirs(src).projects / slug_for("/srv/atlas") / f"{SID}.jsonl")
    note = acct_scoped.note_for(src, dst, "/srv/atlas", SID)
    assert OLD in note and MADE in note and SAID not in note
    assert "no such artifact or no access" in note and "NEW artifact" in note
    assert "servers gmail are" in note
    assert acct_scoped.note_for(dst, src, "/srv/atlas", SID) == ""


def test_a_move_tells_the_desk_what_stayed_behind(reg):
    rig = Rig()
    told = []
    rig.m.scoped = lambda src, dst, cwd, sid: f"left {src.id}->{dst.id} {sid}"
    rig.m.tell = lambda name, text: told.append((name, text))
    assert rig.m.move("atlas", "work")["moved"] is True
    assert told == [("atlas", f"left main->work {SID}")]
    assert [e["event"] for e in rig.events] == ["account_move", "account_scoped"]


def test_an_asleep_move_tells_it_too_and_nothing_left_says_nothing(reg):
    rig = Rig(live=False)
    told = []
    rig.m.scoped = lambda *a: "left"
    rig.m.tell = lambda name, text: told.append(name)
    assert rig.m.move("atlas", "work")["moved"] is True
    assert told == ["atlas"]
    quiet = Rig()
    quiet.m.scoped = lambda *a: ""
    quiet.m.tell = lambda name, text: told.append("again")
    quiet.m.move("atlas", "work")
    assert told == ["atlas"] and [e["event"] for e in quiet.events] == ["account_move"]


def test_a_broken_scan_never_fails_a_move_that_happened(reg):
    rig = Rig()
    rig.m.scoped = lambda *a: 1 / 0
    assert rig.m.move("atlas", "work")["moved"] is True
    assert rig.saved == {"atlas": "work"}


def test_the_brief_tells_every_desk_what_to_do_on_no_access():
    text = features.section()
    assert "no such artifact or no access" in text
    assert "NEW artifact" in text and "tell your boss the new link" in text
