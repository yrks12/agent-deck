"""What POST /api/approve promises.

`hooks/cc-approve.js` already calls this endpoint before *every* tool call in
*every* session running under the hook. Three properties therefore matter more
than the feature itself:

1. It answers with a real decision and names the rule that granted it -- "no
   error was raised" is not the signal; `{"decision": "allow", "rule_id": "r1"}`
   is.
2. It never widens permission by accident. A broken rule file, an unknown tool
   or a missing rule all come back "ask".
3. It is not on the event loop and does not re-read the rule file per call. A
   hundred milliseconds of blocking I/O here is a hundred milliseconds added to
   every tool call on the machine.

Everything is hermetic: rules, the ask ledger and the bus live in tmp_path,
never in the real ~/.claude/agent-bus.

That sentence used to be false, and it was false in the way that matters most:
it named the two paths this file redirects and read as a guarantee about all
three. `/api/approve` writes the question down through `app.ASKS_PATH`, which
nothing here patched, so four of these tests recorded an ask into the owner's
LIVE approval board on every single run -- `sid-a` wanting to `git push origin
main` in `~/Projects/deck`, a folder that does not exist on this machine, plus
one row from the malformed-body test with an empty tool, an empty subject and
an empty cwd, whose `always` option offers a rule matching everything
everywhere. `asking.MAX_ASKS` is 50, so those did not sit beside his real
questions; they evicted them.
"""

import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import autoreview


def write_rules(path, rules):
    path.write_text(json.dumps({"version": 1, "rules": rules}))


ALLOW_GIT_STATUS = {
    "id": "r1",
    "kind": "always_allow",
    "tool": "Bash",
    "pattern": "git status*",
    "cwd": "**",
    "note": "read-only",
}


@pytest.fixture
def bus(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(app_mod, "BUS_FILE", path)
    return path


@pytest.fixture
def rules(tmp_path, monkeypatch):
    path = tmp_path / "autoreview.json"
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", path)
    write_rules(path, [ALLOW_GIT_STATUS])
    return path


@pytest.fixture
def asks(tmp_path, monkeypatch):
    """The ask ledger. Its own fixture, so forgetting it is visible.

    `/api/approve` records the question as well as answering it, and the
    constant it records through is `app.ASKS_PATH`. Patching the rules and the
    event ledger and not this one is what put four fixture rows on the owner's
    live approval board every run.
    """
    path = tmp_path / "asks.json"
    monkeypatch.setattr(app_mod, "ASKS_PATH", path)
    return path


@pytest.fixture
def client(bus, rules, asks):
    return TestClient(app_mod.app)


def call(client, **over):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "session_id": "sid-a",
        "cwd": "/Users/samcarter/Projects/deck",
    }
    payload.update(over)
    res = client.post("/api/approve", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def lines(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# --- the good signal: a real allow, naming the rule that granted it ----------


def test_a_matching_always_allow_rule_allows_and_names_the_rule(client):
    body = call(client)
    assert body["decision"] == "allow"
    assert body["rule_id"] == "r1", "the granting rule must be named, not implied"
    assert "r1" in body["reason"]


def test_require_approval_beats_a_broader_always_allow(client, rules):
    write_rules(
        rules,
        [
            {"id": "wide", "kind": "always_allow", "tool": "Bash",
             "pattern": "*", "cwd": "**"},
            {"id": "guard", "kind": "require_approval", "tool": "Bash",
             "pattern": "git push*", "cwd": "**"},
        ],
    )
    body = call(client, tool_input={"command": "git push origin main"})
    assert body["decision"] == "ask"
    assert body["rule_id"] == "guard"


def test_a_deny_rule_denies(client, rules):
    write_rules(
        rules,
        [{"id": "no", "kind": "deny", "tool": "Bash",
          "pattern": "shutdown*", "cwd": "**"}],
    )
    body = call(client, tool_input={"command": "shutdown -h now"})
    assert body["decision"] == "deny"
    assert body["rule_id"] == "no"


def test_nothing_matching_abstains_rather_than_allowing(client):
    """"Nothing matched" is the deck having no opinion, not a reason to stop
    somebody. It hands the call to Claude Code's own engine -- whose own floor
    is to prompt -- and the thing that must never happen, an `allow` conjured
    out of silence, still does not."""
    body = call(client, tool_input={"command": "rm build/out"})
    assert body["decision"] == "abstain"
    assert body["decision"] != "allow"
    assert body["rule_id"] is None


def test_an_unreadable_rule_file_abstains_rather_than_allowing(client, rules):
    rules.write_text("{ not json at all")
    assert call(client)["decision"] == "abstain"


def test_a_missing_rule_file_abstains_rather_than_allowing(client, rules):
    rules.unlink()
    assert call(client)["decision"] == "abstain"


# --- the audit line, in the shape hooks/cc-bus.js writes ---------------------


def test_every_decision_appends_one_approval_line_to_the_bus(client, bus):
    call(client)
    written = lines(bus)
    assert len(written) == 1, "exactly one line per decision"
    line = written[0]
    assert set(line) == {"ts", "event", "session_id", "tool", "decision", "rule_id"}
    assert line["event"] == "approval"
    assert line["session_id"] == "sid-a"
    assert line["tool"] == "Bash"
    assert line["decision"] == "allow"
    assert line["rule_id"] == "r1"


def test_the_bus_timestamp_is_seconds_like_every_other_hook_line(client, bus):
    """cc-bus.js writes `Date.now() / 1000`. Milliseconds here would sort every
    approval to the far future and break the reader's window maths."""
    call(client)
    ts = lines(bus)[0]["ts"]
    assert isinstance(ts, float)
    assert abs(ts - time.time()) < 60


def test_an_abstention_is_logged_too_not_just_the_allows(client, bus):
    """Every verdict leaves an audit line, including the one that says nothing.
    A call the deck stood aside for is exactly the call somebody will later want
    to know the deck saw."""
    call(client, tool_input={"command": "rm build/out"})
    assert [x["decision"] for x in lines(bus)] == ["abstain"]


# --- the hot path -----------------------------------------------------------


def test_the_decision_is_made_off_the_event_loop(client, monkeypatch):
    """Blocking here blocks every SSE client and every other request.

    `asyncio.get_running_loop()` succeeds only on the loop thread, so its
    failure inside the evaluator is the proof that the work was offloaded.
    """
    seen = {}
    real = autoreview.evaluate

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(*args, **kwargs)

    monkeypatch.setattr(autoreview, "evaluate", spy)
    call(client)
    assert seen == {"on_loop": False}


def test_the_rule_file_is_read_once_not_once_per_tool_call(client, monkeypatch):
    reads = []
    real = autoreview.load_rules

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(autoreview, "load_rules", counting)
    for _ in range(5):
        assert call(client)["decision"] == "allow"
    assert len(reads) == 1, f"rule file parsed {len(reads)} times for 5 calls"


def test_a_changed_rule_file_is_picked_up_without_a_restart(client, rules, monkeypatch):
    assert call(client)["decision"] == "allow"

    write_rules(
        rules,
        [{"id": "r9", "kind": "require_approval", "tool": "Bash",
          "pattern": "git status*", "cwd": "**"}],
    )
    bump = time.time() + 5
    os.utime(rules, (bump, bump))

    body = call(client)
    assert body["decision"] == "ask", "the cache went stale and never reloaded"
    assert body["rule_id"] == "r9"


def test_it_answers_well_under_a_second_per_call(client):
    started = time.monotonic()
    for _ in range(25):
        call(client)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"25 approvals took {elapsed:.2f}s"


def test_a_malformed_body_still_gets_a_decision_not_a_500(client):
    res = client.post("/api/approve", json={"tool_input": "not a dict"})
    assert res.status_code == 200
    assert res.json()["decision"] in ("ask", "abstain")
    assert res.json()["decision"] != "allow"
