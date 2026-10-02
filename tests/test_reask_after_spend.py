"""Detector 3: a spent `once` must not silence the next ask for 15 minutes.

THE DEAD END, found while proving the `once` grant and left unfixed. The
sequence, all inside `asking.suppressed`'s 15-minute window:

  1. the desk meets an unknown action; the deck denies and records ask `X`;
  2. the owner taps "once"; `X` is answered;
  3. the desk retries; `grant()` spends `X` and the call is allowed;
  4. the desk needs the same thing again -- a second `npm install`, a second
     push -- and asks again;
  5. `suppressed()` matches `X` on (tool, subject, cwd) and returns True, so
     `on_verdict` records NOTHING, `app._permission` finds no pending row and
     denies with "ask (unrecorded)".

The agent is stopped, the owner is never asked, and there is no row anywhere
that says why. It is safe -- it denies -- but it is a dead end nobody can see,
for up to fifteen minutes, and the owner's only symptom is a desk that has gone
quiet.

WHAT THE FIX MUST NOT DO. The guard is still right. An agent in a retry loop
asks the same thing forty times in a minute and Sam gets one message; removing
the window puts forty identical questions on his phone. So the boundary is
narrow and both sides of it are pinned below:

  * a row that was ANSWERED **and** CONSUMED is spent -- the answer it holds has
    already been handed out and cannot be handed out again, so it must not stand
    in for a question nobody has been asked. It stops suppressing.
  * a row that is still PENDING inside the window suppresses, unchanged. That is
    the flood guard doing its job, and it is the case that actually happens.
  * a row answered but NOT yet consumed also still suppresses: the grant is live
    and the retry it was for is about to be allowed by `grant()`.

THE GOOD SIGNAL is a re-ask that actually reaches `asks.json` as a live pending
question carrying an id -- never merely the absence of an error.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import asking

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


def spend_a_grant(client, deck):
    """Walk the whole loop once: ask, answer "once", retry, grant consumed."""
    first = permission(client)
    assert first["behavior"] == "deny", "precondition: the first call is a question"
    asking.answer(asks(deck), first["ask_id"], "once")
    assert permission(client)["behavior"] == "allow", "precondition: the grant fired"
    return first["ask_id"]


# ── the dead end ───────────────────────────────────────────────────────────


def test_the_same_need_after_a_spent_grant_is_asked_again(client, deck):
    """The GOOD signal. A new pending row, with its own id, that the owner can
    actually answer."""
    first_id = spend_a_grant(client, deck)

    again = permission(client)

    assert again["behavior"] == "deny", "precondition: no rule was written"
    live = asking.pending(asks(deck))
    assert len(live) == 1, (
        f"the re-ask was swallowed by the flood guard: {live}. The desk is "
        "denied, the owner is never asked, and nothing on the board says why")
    assert live[0].id != first_id, "the spent row is not a new question"
    assert again["ask_id"] == live[0].id


def test_the_denial_names_the_new_ask_instead_of_unrecorded(client, deck):
    """"(unrecorded)" is the deck telling the agent there is no question to
    chase. That sentence is what the agent reads and repeats to the owner."""
    spend_a_grant(client, deck)

    again = permission(client)

    assert again["ask_id"], "no id at all -- the message will read (unrecorded)"
    assert "(unrecorded)" not in again["message"], again["message"]
    assert again["ask_id"] in again["message"]


def test_the_owner_can_answer_the_re_ask_and_it_works(client, deck):
    """End to end, twice. The point of re-asking is that the second answer
    reaches the desk the same way the first one did."""
    spend_a_grant(client, deck)
    second = permission(client)

    asking.answer(asks(deck), second["ask_id"], "once")

    assert permission(client)["behavior"] == "allow", (
        "he answered the re-ask and the desk was still denied")


# ── the boundary: the flood guard is still doing its job ───────────────────


def test_an_unanswered_question_inside_the_window_still_suppresses(client, deck):
    """The reason the guard exists. Forty retries, one message on his phone."""
    permission(client)

    for _ in range(5):
        assert permission(client)["behavior"] == "deny"

    live = asking.pending(asks(deck))
    assert len(live) == 1, (
        f"a retry loop put {len(live)} identical questions on the board; that "
        "is the flood the window exists to stop")


def test_an_answered_but_unspent_grant_still_suppresses(deck):
    """He tapped "once" and the desk has not retried yet. The grant is live and
    the retry it was for is about to be allowed -- asking him again now would be
    asking twice for one decision."""
    path = asks(deck)
    ask = asking.record(path, agent="Acme", tool="Bash", subject="npx x",
                        cwd="/p")
    asking.answer(path, ask.id, "once")

    assert asking.suppressed(path, tool="Bash", subject="npx x",
                             cwd="/p", window=900) is True


def test_an_answered_and_spent_row_stops_suppressing(deck):
    """The unit-level statement of the fix, on the boundary itself: the only
    thing that changes is a row whose answer has already been handed out."""
    path = asks(deck)
    ask = asking.record(path, agent="Acme", tool="Bash", subject="npx x",
                        cwd="/p")
    asking.answer(path, ask.id, "once")
    assert asking.grant(path, tool="Bash", subject="npx x",
                        cwd="/p") is not None, "precondition: the grant spent"

    assert asking.suppressed(path, tool="Bash", subject="npx x",
                             cwd="/p", window=900) is False


def test_a_spent_row_does_not_unsuppress_a_different_question(deck):
    """Matching stays exact. Spending a grant for one command must not open the
    floodgates for another that is still pending."""
    path = asks(deck)
    spent = asking.record(path, agent="Acme", tool="Bash", subject="npx x",
                          cwd="/p")
    asking.answer(path, spent.id, "once")
    asking.grant(path, tool="Bash", subject="npx x", cwd="/p")
    asking.record(path, agent="Acme", tool="Bash", subject="npx y", cwd="/p")

    assert asking.suppressed(path, tool="Bash", subject="npx y",
                             cwd="/p", window=900) is True
