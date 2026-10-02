"""The grader must not grade itself green.

`bin/deck-acceptance` is the only instrument that answers "did this actually
happen". Two of its claims are now the headline ones -- an agent naming itself,
and an agent hiring another agent -- and until this file existed neither had a
step, so a run could report 8/8 while neither had ever occurred.

A grading instrument that is wrong is worse than no instrument, so each step is
pinned in BOTH directions: it must find real evidence, and it must refuse
evidence that only looks like it. The refusals are the point. `hire` events
carry `by`, and the owner hiring is the ordinary case -- a step that counted
those would read green from day one and mean nothing.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def accept(tmp_path, monkeypatch):
    """The script, loaded as a module, with its ledger pointed at a temp file."""
    spec = importlib.util.spec_from_loader(
        "deck_acceptance",
        importlib.machinery.SourceFileLoader(
            "deck_acceptance", str(ROOT / "bin" / "deck-acceptance")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    events = tmp_path / "events.jsonl"
    events.write_text("")
    monkeypatch.setattr(mod, "EVENTS", events, raising=False)
    mod._events_path = events
    return mod


def _write(mod, *records):
    import time
    now = time.time()
    mod._events_path.write_text(
        "\n".join(json.dumps({"ts": now, **r}) for r in records) + "\n")


# ---------------------------------------------------------------- named itself

def test_naming_itself_needs_the_agent_to_have_chosen_the_name(accept):
    """THE test. The agent was hired as one name and took another."""
    _write(accept, {"event": "desk", "actor": "new-hire-fe0513",
                    "name": "stall-watch", "result": "patched"})
    ok, detail = accept.step_named_itself()
    assert ok, detail
    assert "stall-watch" in detail


def test_a_desk_that_kept_its_hired_name_is_not_evidence(accept):
    """The refusal that makes the test above mean something: the deck patching
    a desk under the name it was hired with is ordinary bookkeeping, not an
    agent choosing who it is."""
    _write(accept, {"event": "desk", "actor": "acme", "name": "acme",
                    "result": "patched"})
    ok, _ = accept.step_named_itself()
    assert not ok


def test_a_refused_naming_line_is_not_evidence(accept):
    """The exact defect this project just fixed: the agent believed it had
    taken the desk and the board disagreed. A refusal must never grade green."""
    _write(accept, {"event": "desk", "actor": "new-hire-fe0513",
                    "name": "new-hire-fe0513", "result": "refused",
                    "reason": "YOS_DESK: Expecting ',' delimiter"})
    ok, _ = accept.step_named_itself()
    assert not ok


# ------------------------------------------------------------- agent hires

def test_an_agent_hiring_is_evidence_and_names_who_did_it(accept):
    """THE test for the claim the whole product rests on: the team grows
    without the owner."""
    _write(accept, {"event": "hire", "name": "intern", "by": "acme",
                    "reports_to": "acme"})
    ok, detail = accept.step_agent_hired()
    assert ok, detail
    assert "acme" in detail and "intern" in detail


def test_the_owner_hiring_is_not_an_agent_hiring(accept):
    """The refusal. Every hire so far has been `by=owner`; counting those
    would have read green before the harvester was ever wired."""
    _write(accept, {"event": "hire", "name": "new-hire-fe0513", "by": "owner",
                    "reports_to": None})
    ok, _ = accept.step_agent_hired()
    assert not ok


def test_a_refused_hire_is_not_a_hire(accept):
    """The caps refusing an agent's hire is the guard working, not the
    capability being exercised."""
    _write(accept, {"event": "hire_refused", "name": "intern", "by": "acme",
                    "reason": "too_deep"})
    ok, _ = accept.step_agent_hired()
    assert not ok


# --------------------------------------------------------- reported upward
#
# This step had no test, and it was broken in a way no run could survive: an
# edge's `from`/`to` are endpoint OBJECTS and it compared them as dict KEYS, so
# the first edge raised `TypeError: unhashable type: 'dict'` the moment any
# desk had a boss. It could never have gone green, whatever happened.


class _Comms:
    """Stands in for `curl .../api/comms`, in the shape the real endpoint
    returns: endpoints are objects, and `status` says whether it landed."""

    def __init__(self, edges):
        self.stdout = json.dumps({"edges": edges})

    @classmethod
    def patch(cls, mod, monkeypatch, tmp_path, *, desks, edges):
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"version": 1, "agents": desks}))
        monkeypatch.setattr(mod, "ROSTER", roster, raising=False)
        stub = type("S", (), {"run": staticmethod(lambda *a, **k: cls(edges))})
        monkeypatch.setattr(mod, "subprocess", stub, raising=False)


def _edge(sender, target, status):
    return {"from": {"session_id": "s1", "name": sender, "pid": 1},
            "to": {"session_id": "s2", "name": target, "pid": 2},
            "status": status, "text": "reporting in"}


DESKS = [{"name": "acme", "reports_to": None},
         {"name": "intern", "reports_to": "acme"}]


def test_a_delivered_message_to_its_boss_is_evidence(accept, monkeypatch, tmp_path):
    """THE test. The junior said something and the boss's own transcript has it."""
    _Comms.patch(accept, monkeypatch, tmp_path, desks=DESKS,
                 edges=[_edge("intern", "acme", "delivered")])
    ok, detail = accept.step_reported_upward()
    assert ok, detail
    assert "intern" in detail and "acme" in detail


def test_a_message_that_never_arrived_is_not_evidence(accept, monkeypatch, tmp_path):
    """The refusal that makes the test above mean something.

    `status: "sent"` is written as soon as the SENDER attempts a SendMessage --
    it is the sender's own transcript, not the receiver's. A desk that renamed
    itself is not reachable under its new name until it respawns, so the tool
    fails, and the edge shows up anyway. Grading that green scores "work
    reported to the boss" on a report nobody ever got.
    """
    _Comms.patch(accept, monkeypatch, tmp_path, desks=DESKS,
                 edges=[_edge("intern", "acme", "sent")])
    ok, detail = accept.step_reported_upward()
    assert not ok
    assert "never arrived" in detail


def test_traffic_that_is_not_upward_does_not_crash_the_check(accept, monkeypatch,
                                                             tmp_path):
    """The regression for the TypeError: unrelated edges must read as a clean
    'not yet', never as a broken check."""
    _Comms.patch(accept, monkeypatch, tmp_path, desks=DESKS,
                 edges=[_edge("acme", "intern", "delivered"),
                        _edge("intern", "stranger", "delivered")])
    ok, detail = accept.step_reported_upward()
    assert not ok
    assert "no message from a desk to its boss yet" in detail


# --------------------------------------------------------------- registered

def test_both_new_steps_are_actually_in_the_run(accept):
    """A step nothing runs grades nothing. This project has been bitten three
    times by a capability that existed and was called by nobody."""
    labels = [label for label, _ in accept.STEPS]
    assert "it named itself" in labels
    assert "an agent hired another agent" in labels
