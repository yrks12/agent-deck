"""Detector 3: "once" has to actually let the thing through, exactly once.

THE DEAD END. `asking.answer` writes a rule for `always` and for `never` and
nothing at all for `once` -- the docstring says so in as many words: "`once` is
not a rule and never becomes one." That was harmless while the only consumer
was `PreToolUse`, where the agent was already sitting on its own prompt and the
human answering "once" answered it at the keyboard.

It stopped being harmless when `/api/permission` started DENYING at prompt time.
Now the sequence is:

  1. the desk meets an unknown action; the deck denies it in a sentence and
     records ask `ab3de`;
  2. the owner taps "once";
  3. `answer()` marks the ask answered and writes no rule;
  4. the desk retries; `/api/permission` re-evaluates, finds no rule, denies.

He tapped approve and nothing happened. This is the last dead end in the loop.

WHAT THE FIX HAS TO GET RIGHT, and both halves are asserted below:

* a grant that can be replayed is a permission leak -- "once" must not become
  "forever" because the agent retried twice, or because two retries raced, or
  because the daemon restarted and re-read the file;
* a grant that expires before the retry is the bug being fixed wearing a
  different hat.

The signal is the presence of the grant working -- the retry going through, the
second retry not going through -- never the absence of an error.
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import asking
from server.expiry import DEFAULT_TTL

UNKNOWN = {
    "tool_name": "Bash",
    "tool_input": {"command": "npx some-brand-new-thing --init"},
    "session_id": "sid-hired",
    "cwd": "/Users/samcarter/Projects/acme",
    "agent": "Acme",
}


@pytest.fixture
def deck(tmp_path, monkeypatch):
    rules = tmp_path / "autoreview.json"
    rules.write_text(json.dumps({"version": 1, "rules": []}))
    monkeypatch.setattr(app_mod, "AUTOREVIEW_PATH", rules)
    monkeypatch.setattr(app_mod, "ASKS_PATH", tmp_path / "asks.json")
    monkeypatch.setattr(app_mod, "BUS_FILE", tmp_path / "events.jsonl")
    return tmp_path


@pytest.fixture
def client(deck):
    return TestClient(app_mod.app)


def permission(client, **over):
    res = client.post("/api/permission", json={**UNKNOWN, **over})
    assert res.status_code == 200, res.text
    return res.json()


def asks(deck):
    return deck / "asks.json"


def say_once(deck, ask_id):
    return asking.answer(asks(deck), ask_id, "once")[0]


# ── the dead end itself ────────────────────────────────────────────────────


def test_answering_once_lets_the_retry_through(client, deck):
    """The GOOD signal: he taps approve, and the thing he approved happens."""
    first = permission(client)
    assert first["behavior"] == "deny"

    say_once(deck, first["ask_id"])

    retry = permission(client)
    assert retry["behavior"] == "allow", (
        "the owner answered 'once' and the agent was denied again -- he tapped "
        "approve and nothing happened")


def test_the_grant_is_spent_exactly_once(client, deck):
    """A grant that can be replayed is a permission leak. Three retries, one
    allow."""
    first = permission(client)
    say_once(deck, first["ask_id"])

    behaviours = [permission(client)["behavior"] for _ in range(3)]
    assert behaviours == ["allow", "deny", "deny"], behaviours


def test_a_spent_grant_stays_spent_across_a_restart(client, deck):
    """The exhaustion has to be on disk, not in the process. A daemon restart
    between the retry and the next one must not refill it."""
    first = permission(client)
    say_once(deck, first["ask_id"])
    assert permission(client)["behavior"] == "allow"

    # Everything this endpoint knows is re-read from `asks.json` each call, so
    # a fresh client is a restart for the purposes of this claim.
    assert permission(TestClient(app_mod.app))["behavior"] == "deny"


def test_two_retries_racing_spend_one_grant_between_them(client, deck):
    """Two threads, one grant. The read-modify-write on the ask file is not
    atomic by itself, and "the agent retried twice quickly" is the ordinary
    case, not the exotic one."""
    first = permission(client)
    say_once(deck, first["ask_id"])

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(6, timeout=10)

    def hit():
        barrier.wait()
        behavior = permission(client)["behavior"]
        with lock:
            results.append(behavior)

    threads = [threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a retry blocked on the grant"

    assert results.count("allow") == 1, results


# ── and it does not leak sideways ──────────────────────────────────────────


def test_a_grant_does_not_cover_a_different_command(client, deck):
    first = permission(client)
    say_once(deck, first["ask_id"])
    other = permission(client, tool_input={"command": "npx something-else"})
    assert other["behavior"] == "deny"


def test_a_grant_does_not_cover_another_folder(client, deck):
    first = permission(client)
    say_once(deck, first["ask_id"])
    assert permission(client, cwd="/Users/samcarter/Projects/other")[
        "behavior"] == "deny"


def test_a_grant_does_not_cover_another_tool(client, deck):
    first = permission(client)
    say_once(deck, first["ask_id"])
    assert permission(client, tool_name="Write",
                      tool_input={"file_path": "npx some-brand-new-thing --init"}
                      )["behavior"] == "deny"


def test_an_unanswered_ask_grants_nothing(client, deck):
    """The row exists the moment the question is asked. Its mere presence must
    never be readable as consent."""
    permission(client)
    assert permission(client)["behavior"] == "deny"


def test_never_grants_nothing(client, deck):
    first = permission(client)
    asking.answer(asks(deck), first["ask_id"], "never")
    assert permission(client)["behavior"] == "deny"


# ── the other half: it must not expire before the retry ────────────────────


def test_the_grant_survives_a_realistic_gap_before_the_retry(client, deck):
    """He answers on a phone, minutes later, and the desk retries after that.
    A grant that has already lapsed by then is the bug being fixed."""
    first = permission(client)
    settled = say_once(deck, first["ask_id"])
    assert settled.answered_at is not None, (
        "without an answered-at there is no clock to expire the grant on")

    # Half an hour after he answered.
    later = settled.answered_at + 1800
    assert asking.grant(asks(deck), tool="Bash",
                        subject="npx some-brand-new-thing --init",
                        cwd=UNKNOWN["cwd"], now=later) is not None


def test_a_grant_does_not_sit_there_forever(client, deck):
    """It ages out on the same clock everything else in the ask loop does. An
    open-ended grant is a permission left lying around."""
    first = permission(client)
    settled = say_once(deck, first["ask_id"])
    stale = settled.answered_at + DEFAULT_TTL + 1
    assert asking.grant(asks(deck), tool="Bash",
                        subject="npx some-brand-new-thing --init",
                        cwd=UNKNOWN["cwd"], now=stale) is None


# ── the floor still holds ──────────────────────────────────────────────────


def test_once_lifts_the_secure_handoff_floor_exactly_once(client, deck):
    """The handoff floor's rule is "asks every time", not "never". He is the
    owner and he answered this specific question, so it goes through -- and
    the next one asks again, which is what the floor is for."""
    creds = {"command": "cat ~/.env"}
    first = permission(client, tool_input=creds)
    assert first["behavior"] == "deny"
    say_once(deck, first["ask_id"])
    assert permission(client, tool_input=creds)["behavior"] == "allow"
    assert permission(client, tool_input=creds)["behavior"] == "deny"


def test_the_ledger_says_a_one_shot_grant_was_what_allowed_it(client, deck):
    """An allow that came from a tap, not from a rule, has to be legible as
    that afterwards -- otherwise the audit trail shows a permission nobody can
    find the rule for."""
    first = permission(client)
    say_once(deck, first["ask_id"])
    body = permission(client)
    assert body["behavior"] == "allow"
    assert first["ask_id"] in str(body.get("rule_id") or "")

    rows = [json.loads(x) for x in
            (deck / "events.jsonl").read_text().splitlines() if x.strip()]
    granted = [r for r in rows if r.get("event") == "permission_request"
               and r.get("behavior") == "allow"]
    assert granted and first["ask_id"] in str(granted[-1].get("ask_id") or "")


# ── the unit underneath ────────────────────────────────────────────────────


def test_grant_marks_the_ask_consumed_on_disk(deck):
    path = asks(deck)
    ask = asking.record(path, agent="Acme", tool="Bash", subject="ls -la",
                        cwd="/w")
    asking.answer(path, ask.id, "once")

    got = asking.grant(path, tool="Bash", subject="ls -la", cwd="/w")
    assert got is not None and got.id == ask.id
    assert asking.find(path, ask.id).consumed_at is not None
    assert asking.grant(path, tool="Bash", subject="ls -la", cwd="/w") is None


def test_grant_matches_the_redacted_subject(deck):
    """Subjects are stored redacted. Comparing a raw one would never match for
    exactly the commands that carry a credential -- the same trap `suppressed`
    documents."""
    path = asks(deck)
    raw = "curl -H 'Authorization: Bearer " + "sk-ant-abcdefghijkl'"
    ask = asking.record(path, agent="Acme", tool="Bash", subject=raw, cwd="/w")
    asking.answer(path, ask.id, "once")
    assert asking.grant(path, tool="Bash", subject=raw, cwd="/w") is not None


def test_grant_on_a_missing_file_is_none_not_a_crash(tmp_path):
    assert asking.grant(tmp_path / "nope.json", tool="Bash", subject="ls",
                        cwd="/w") is None
