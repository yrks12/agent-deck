"""The seam that makes an "ask" verdict into a question Sam can actually answer.

`autoreview.evaluate` has always been able to say `ask`, and `asking.record` has
always been able to write one down -- but nothing joined them, so the queue the
approval card reads was empty forever and the "asks once, then never again"
promise died at step one. `server/ask_recorder.on_verdict` is that join, and
these tests are written before it exists.

Two of them are the whole brief:

* an `allow` verdict must record NOTHING. A board that fills with things nobody
  needs to answer is worse than an empty one -- he stops reading it, and then the
  one question that mattered goes unread too. The same test also asserts that an
  `ask` on the same call DOES record, so a function that never records anything
  cannot pass by doing nothing.
* forty identical asks inside the window produce exactly ONE row, and a
  different tool in the same window still gets its own. Suppression that
  suppresses everything is not suppression; it is a swallowed question, and an
  agent stuck forever with nobody knowing why.
"""

import pytest

from server import ask_recorder, asking
from server.autoreview import (
    ALWAYS_ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    Rule,
    evaluate,
    load_rules,
)

ACME = "/Users/samcarter/Projects/acme"
T0 = 1_700_000_000.0

#: A rule that asks about everything. These tests used to pass `[]` and lean on
#: "no rule" meaning "ask", which it no longer does: an empty rule set is now
#: `abstain` -- the deck holding no opinion and letting Claude Code's own engine
#: decide -- because answering "ask" for every call it had no rule about is what
#: put thirteen `pwd`-shaped cards on the owner's board.
#:
#: The subject under test is unchanged. `on_verdict` is still being asked "what
#: do you do with an ASK verdict", and it still has to be a real verdict from a
#: real `evaluate`. Only the way this file gets one has moved: state the rule
#: instead of relying on silence.
ASKS = [Rule(id="ask-everything", kind=REQUIRE_APPROVAL, tool="*",
             pattern="*", cwd="**")]


def verdict_for(rules, *, tool_name="Bash", tool_input=None, cwd=ACME):
    """The real evaluate(), never a hand-built Verdict.

    The seam has to key off what the engine actually returns, so these tests
    drive it through `autoreview.evaluate` rather than faking its output.
    """
    return evaluate(
        rules,
        tool_name=tool_name,
        tool_input={"command": "npm test"} if tool_input is None else tool_input,
        cwd=cwd,
    )


def record(path, rules, *, tool_name="Bash", tool_input=None, cwd=ACME,
           agent="acme-growth", session_id="sess-1", now=T0):
    tool_input = {"command": "npm test"} if tool_input is None else tool_input
    return ask_recorder.on_verdict(
        path,
        verdict=verdict_for(rules, tool_name=tool_name,
                            tool_input=tool_input, cwd=cwd),
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=cwd,
        session_id=session_id,
        agent=agent,
        now=now,
    )


# ── 1. only questions that need answering reach the board ──────────────────


def test_allow_records_nothing_but_ask_on_the_same_call_records(tmp_path):
    """The queue holds questions, not a log of every tool call.

    Both halves matter. If only the first were asserted, a `on_verdict` that
    returned None unconditionally -- the exact bug this module exists to fix --
    would pass.
    """
    path = tmp_path / "asks.json"
    allowed = [Rule(id="npm", kind=ALWAYS_ALLOW, tool="Bash",
                    pattern="npm test*", cwd=ACME)]

    assert record(path, allowed) is None
    assert asking.pending(path) == []

    ask = record(path, ASKS)         # same call, a rule that asks -> "ask"
    assert ask is not None
    live = asking.pending(path)
    assert [a.id for a in live] == [ask.id]
    assert live[0].tool == "Bash"
    assert live[0].subject == "npm test"
    assert live[0].cwd == ACME
    assert live[0].agent == "acme-growth"


def test_a_deny_verdict_records_nothing_because_it_was_already_decided(tmp_path):
    path = tmp_path / "asks.json"
    denied = [Rule(id="no-force", kind=DENY, tool="Bash",
                   pattern="git push*", cwd=ACME)]
    assert record(path, denied,
                  tool_input={"command": "git push origin main"}) is None
    assert asking.pending(path) == []

    # ...and the board is not broken: a require_approval rule on the same tool
    # still lands, so "records nothing" is about the decision, not the file.
    guarded = [Rule(id="guard", kind=REQUIRE_APPROVAL, tool="Bash",
                    pattern="git push*", cwd=ACME)]
    ask = record(path, guarded, tool_input={"command": "git push origin main"})
    assert ask is not None
    assert [a.subject for a in asking.pending(path)] == ["git push origin main"]


# ── 2. the flood guard, and what it must NOT swallow ───────────────────────


def test_forty_identical_asks_are_one_row_and_another_tool_still_gets_its_own(tmp_path):
    """An agent in a retry loop costs him one message, not forty.

    The second half is the guard on the guard: suppression keyed too widely
    (by session, by agent, by "recently asked anything") would swallow a
    different question entirely, and a swallowed question is an agent stalled
    forever with nobody knowing why.
    """
    path = tmp_path / "asks.json"

    for n in range(40):
        ask_recorder.on_verdict(
            path,
            verdict=verdict_for(ASKS),
            tool_name="Bash",
            tool_input={"command": "npm test"},
            cwd=ACME,
            session_id="sess-1",
            agent="acme-growth",
            now=T0 + n,       # all well inside the window
        )

    rows = asking.pending(path)
    assert [a.subject for a in rows] == ["npm test"]

    other = record(path, ASKS,tool_name="Write",
                   tool_input={"file_path": f"{ACME}/notes.md"},
                   now=T0 + 41)
    assert other is not None
    assert sorted(a.tool for a in asking.pending(path)) == ["Bash", "Write"]

    # A different *subject* on the same tool is a different question too.
    third = record(path, ASKS,tool_input={"command": "npm run build"},
                   now=T0 + 42)
    assert third is not None
    assert sorted(a.subject for a in asking.pending(path)) == [
        f"{ACME}/notes.md", "npm run build", "npm test",
    ]


def test_the_same_question_asks_again_once_the_window_has_passed(tmp_path):
    """Suppression is a window, not a permanent mute.

    He answered nothing, the agent came back an hour later, and he has to be
    told again -- otherwise the flood guard quietly becomes a deny.
    """
    path = tmp_path / "asks.json"
    first = record(path, ASKS,now=T0)
    later = record(path, ASKS,now=T0 + 901)
    assert first is not None and later is not None
    assert first.id != later.id
    assert len(asking.pending(path)) == 2


def test_the_suppression_window_is_configurable_per_call(tmp_path):
    path = tmp_path / "asks.json"
    assert ask_recorder.on_verdict(
        path, verdict=verdict_for(ASKS), tool_name="Bash",
        tool_input={"command": "npm test"}, cwd=ACME,
        session_id="s", agent="a", now=T0, window=10,
    ) is not None
    again = ask_recorder.on_verdict(
        path, verdict=verdict_for(ASKS), tool_name="Bash",
        tool_input={"command": "npm test"}, cwd=ACME,
        session_id="s", agent="a", now=T0 + 11, window=10,
    )
    assert again is not None
    assert len(asking.pending(path)) == 2


# ── the handoff floor is recorded, and flagged ─────────────────────────────


def test_a_handoff_ask_is_recorded_and_flagged(tmp_path):
    """A credential still reaches him, and is flagged as the floor's question.

    `evaluate` answers "ask" for it unless this desk already said "always" to
    its class, so it must appear on the board like any other question. The
    flag is what makes `answer` refuse a GLOBAL always for it; the message
    offers the desk-scoped one (tests/test_always_means_always.py).
    """
    path = tmp_path / "asks.json"
    ask = record(path, ASKS,tool_input={"command": "cat .env"})

    assert ask is not None
    assert [a.id for a in asking.pending(path)] == [ask.id]
    assert ask_recorder.is_handoff_ask(ask) is True
    assert "this desk only" in asking.compose(ask)

    ordinary = record(path, ASKS,tool_input={"command": "npm run lint"},
                      now=T0 + 5)
    assert ask_recorder.is_handoff_ask(ordinary) is False
    assert "this desk only" in asking.compose(ordinary)


# ── the point of the whole feature: the SECOND time ────────────────────────


def test_a_recorded_ask_answered_always_stops_the_engine_asking(tmp_path):
    """End to end, because this is the claim the feature makes.

    ask -> row -> one word -> rule -> the same call is allowed next time. If the
    subject written down is not the one `evaluate` matches against, the rule
    built from it never fires and the agent asks forever.
    """
    path = tmp_path / "asks.json"
    rules_path = tmp_path / "autoreview.json"

    call = {"command": "gh pr create --fill"}
    ask = record(path, ASKS,tool_input=call)
    assert ask is not None

    settled, rule = asking.answer(path, ask.id, "always", rules_path)
    assert settled.answered == "always"
    assert rule is not None

    assert evaluate(load_rules(rules_path), tool_name="Bash",
                    tool_input=call, cwd=ACME).decision == "allow"
    # And the seam now stays quiet about it, which is the whole promise.
    assert record(path, load_rules(rules_path), tool_input=call,
                  now=T0 + 5000) is None


# ── attribution ────────────────────────────────────────────────────────────


def test_a_nameless_session_is_still_identifiable_on_the_phone(tmp_path):
    """He has to know WHO is asking, or the message is unanswerable.

    Nothing guarantees a desk name reaches the hook, so a blank agent falls back
    to the session id rather than an empty bold line.
    """
    path = tmp_path / "asks.json"
    ask = record(path, ASKS,agent="", session_id="abc123def456")
    assert ask is not None
    assert "abc123" in ask.agent
    assert "abc123" in asking.compose(ask)


def test_a_dict_verdict_from_the_endpoint_is_understood(tmp_path):
    """The HTTP layer speaks JSON, so the seam accepts the dict shape too."""
    path = tmp_path / "asks.json"
    ask = ask_recorder.on_verdict(
        path,
        verdict={"decision": "ask", "rule_id": None, "reason": "no rule"},
        tool_name="Bash",
        tool_input={"command": "npm test"},
        cwd=ACME,
        session_id="s",
        agent="acme-growth",
        now=T0,
    )
    assert ask is not None
    assert [a.subject for a in asking.pending(path)] == ["npm test"]


def test_a_broken_tool_input_still_produces_an_answerable_question(tmp_path):
    """A future tool with an input shape we have never seen must not vanish.

    Losing the question means the agent sits on its own prompt with nothing on
    his board explaining why, which is the failure this module exists to end.
    """
    path = tmp_path / "asks.json"
    ask = record(path, ASKS,tool_name="FutureTool", tool_input="not-a-dict")
    assert ask is not None
    assert asking.pending(path)[0].tool == "FutureTool"
    assert asking.pending(path)[0].subject == "not-a-dict"
