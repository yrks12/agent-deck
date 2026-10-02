"""Hermetic (runs in default pytest): the probe filter never grades stale replies."""
from probe_filter import replies_to_probes, require_chief


def m(i, role, text="x"):
    return {"id": i, "role": role, "text": text}


def test_old_replies_before_our_probes_are_never_graded():
    thread = [m("o1", "owner"), m("r0", "agent", "OLD"), m("p1", "engineer"), m("r1", "agent", "NEW")]
    assert [r["text"] for r in replies_to_probes(thread, ["p1"])] == ["NEW"]


def test_each_probe_gets_only_its_own_first_reply():
    thread = [m("p1", "owner"), m("a", "agent", "1a"), m("b", "agent", "1b"),
              m("p2", "owner"), m("c", "agent", "2a")]
    assert [r["text"] for r in replies_to_probes(thread, ["p1", "p2"])] == ["1a", "2a"]


def test_unanswered_probe_contributes_nothing_and_deck_lines_are_skipped():
    thread = [m("p1", "owner"), m("d", "agent", "[Agent Deck] hi"), m("p2", "owner")]
    assert replies_to_probes(thread, ["p1", "p2"]) == []


def test_no_chief_configured_means_none_not_atlas():
    assert require_chief({}) is None
    assert require_chief({"DECK_CHIEF_DESK": " "}) is None
    assert require_chief({"DECK_CHIEF_DESK": "qa-chief"}) == "qa-chief"
