"""The secure handoff: what happens when only a human can do the next step.

These tests are written before server/handoff.py exists. Each one pins a
behaviour that, if it regressed, would make the feature actively harmful
rather than merely absent:

  * a handoff with no state line makes Sam open his laptop in a panic,
  * a `skipped` that reads like a `done` turns into an infinite retry loop,
  * a one-time code in a message that syncs to his other devices is the exact
    leak this feature exists to prevent.
"""

import pytest

from server import handoff as H


def _raise(path, **over):
    kw = dict(
        agent="acme-growth",
        kind="payment",
        needs="Confirm the card payment on the Google Ads publish screen.",
        state="Campaign is built and saved as a draft - £20/day, UK + Israel, "
              "Search only. Nothing is live and nothing has spent.",
        where="https://ads.google.com/campaigns",
    )
    kw.update(over)
    return H.raise_handoff(path, **kw)


# ── the store ──────────────────────────────────────────────────────────────


def test_raise_then_waiting_returns_it(tmp_path):
    """The good signal: a raised handoff is actually queued for the human."""
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    assert h.status == "waiting"
    assert h.agent == "acme-growth"
    assert h.kind == "payment"

    queued = H.waiting(path)
    assert [q.id for q in queued] == [h.id]
    assert queued[0].state == h.state


def test_ids_are_unique_and_phone_typeable(tmp_path):
    path = tmp_path / "handoffs.json"
    ids = {_raise(path).id for _ in range(12)}
    assert len(ids) == 12
    for hid in ids:
        assert 3 <= len(hid) <= 8
        # Nothing a thumb has to guess at: no case-shift, no 0/O, no 1/l/I.
        assert set(hid) <= set(H.ID_ALPHABET)


def test_kind_must_be_one_of_KINDS(tmp_path):
    path = tmp_path / "handoffs.json"
    with pytest.raises(ValueError):
        _raise(path, kind="vibes")
    for kind in H.KINDS:
        assert _raise(path, kind=kind).kind == kind


def test_a_handoff_without_state_is_rejected(tmp_path):
    """The load-bearing rule, enforced at the door as well as in compose()."""
    path = tmp_path / "handoffs.json"
    with pytest.raises(ValueError):
        _raise(path, state="   ")


def test_store_is_capped_and_keeps_the_newest(tmp_path):
    path = tmp_path / "handoffs.json"
    made = [_raise(path, needs=f"step {i}") for i in range(H.MAX_HANDOFFS + 7)]
    kept = H.load(path)
    assert len(kept) == H.MAX_HANDOFFS
    assert kept[-1].id == made[-1].id          # the newest survived
    assert made[0].id not in {k.id for k in kept}


def test_a_missing_file_is_an_empty_queue_not_a_crash(tmp_path):
    assert H.load(tmp_path / "nope.json") == []
    assert H.waiting(tmp_path / "nope.json") == []


def test_resolve_moves_it_out_of_waiting(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    done = H.resolve(path, h.id, "done")
    assert done.status == "done"
    assert done.id == h.id
    assert H.waiting(path) == []
    assert H.load(path)[0].status == "done"    # persisted, not just returned


def test_resolve_rejects_an_unknown_outcome_and_an_unknown_id(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    with pytest.raises(ValueError):
        H.resolve(path, h.id, "sort-of")
    with pytest.raises(KeyError):
        H.resolve(path, "zzzz", "done")


def test_taken_over_stays_open_because_the_work_is_not_finished(tmp_path):
    """"Take over" means Sam is at the keyboard now, not that the step is
    done. It must not silently disappear from the queue as if it were."""
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    H.resolve(path, h.id, "taken_over")
    assert H.load(path)[0].status == "taken_over"


# ── staleness ──────────────────────────────────────────────────────────────


def test_a_handoff_whose_agent_died_goes_stale(tmp_path):
    """Otherwise the queue fills with blocks nobody can unblock."""
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    changed = H.mark_stale(path, alive=set(), now=h.ts + 10_000)
    assert [c.id for c in changed] == [h.id]
    assert H.load(path)[0].status == "stale"
    assert H.waiting(path) == []


def test_a_live_agents_handoff_survives(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    assert H.mark_stale(path, alive={"acme-growth"}, now=h.ts + 10_000) == []
    assert [w.id for w in H.waiting(path)] == [h.id]


def test_a_just_raised_handoff_is_not_swept_by_a_stale_liveness_set(tmp_path):
    """A desk takes a moment to appear in the live set. Sweeping inside that
    window would kill the handoff before the phone even buzzed."""
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    assert H.mark_stale(path, alive=set(), now=h.ts) == []
    assert [w.id for w in H.waiting(path)] == [h.id]


def test_stale_only_touches_waiting_ones(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    H.resolve(path, h.id, "done")
    assert H.mark_stale(path, alive=set(), now=h.ts + 10_000) == []
    assert H.load(path)[0].status == "done"


# ── compose(): what lands on the phone ─────────────────────────────────────


def test_compose_carries_the_five_things_in_order(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    msg = H.compose(h)

    assert "acme-growth" in msg
    assert "Confirm the card payment" in msg
    assert "Nothing is live" in msg
    assert "ads.google.com/campaigns" in msg
    assert h.id in msg

    order = [
        msg.index("acme-growth"),
        msg.index("Confirm the card payment"),
        msg.index("Nothing is live"),
        msg.index("ads.google.com/campaigns"),
    ]
    assert order == sorted(order), "agent, need, state, where - in that order"


def test_compose_always_states_the_state_of_the_work(tmp_path):
    """THE bug this feature exists to prevent: a handoff that tells Sam he is
    needed without telling him whether anything already went live."""
    path = tmp_path / "handoffs.json"
    h = _raise(path, state="Deploy is half-applied: web is on the new build, "
                           "the worker is still on the old one.")
    msg = H.compose(h)
    assert "Deploy is half-applied" in msg
    assert "the worker is still on the old one" in msg


def test_compose_offers_the_three_replies(tmp_path):
    path = tmp_path / "handoffs.json"
    msg = H.compose(_raise(path)).lower()
    assert "take over" in msg
    assert "done" in msg
    assert "skip" in msg


def test_compose_fits_a_phone(tmp_path):
    path = tmp_path / "handoffs.json"
    msg = H.compose(_raise(path))
    assert len(msg) <= H.PHONE_LIMIT
    assert len(msg.splitlines()) <= 12


def test_compose_never_leaks_a_one_time_code_or_a_token(tmp_path):
    """A 2FA code in a message that syncs to his other devices is precisely
    the thing a secure handoff is supposed to protect."""
    path = tmp_path / "handoffs.json"
    h = _raise(
        path,
        kind="2fa",
        needs="Enter the code 417293 that Google just texted you.",
        state="Draft saved, not live. The session token is "
              "a3f9c1d0e2b4a6f8c0d2e4b6a8f0c2d4 and it still works.",
        where="https://user:hunter2@ads.google.com/verify?token=" + "abc123XYZdef456",
    )
    msg = H.compose(h)

    assert H.REDACTED in msg              # the good signal: it did redact
    assert "417293" not in msg
    assert "hunter2" not in msg
    assert "a3f9c1d0e2b4a6f8c0d2e4b6a8f0c2d4" not in msg
    assert "abc123XYZdef456" not in msg
    # ...and the parts he needs to act on survived the scrubbing.
    assert "ads.google.com" in msg
    assert "Draft saved, not live" in msg


def test_redact_leaves_an_ordinary_handoff_alone(tmp_path):
    """Redaction that eats normal prose is a worse bug than the leak."""
    plain = "Campaign is a draft at £20/day, UK + Israel, Search only, not live."
    assert H.redact(plain) == plain


# ── resume_message(): what the agent is told afterwards ────────────────────
#
# The real bug lives here. If `done` and `skipped` collapse into the same
# instruction, a skipped step becomes an infinite retry loop.


def test_done_tells_the_agent_to_verify_rather_than_assume(tmp_path):
    path = tmp_path / "handoffs.json"
    msg = H.resume_message(_raise(path), "done").lower()
    assert "verify" in msg
    assert "do not assume" in msg


def test_skipped_tells_the_agent_to_abandon_and_report(tmp_path):
    path = tmp_path / "handoffs.json"
    msg = H.resume_message(_raise(path), "skipped").lower()
    assert "do not retry" in msg
    assert "abandon" in msg
    assert "report" in msg


def test_done_and_skipped_are_materially_different_instructions(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    done = H.resume_message(h, "done").lower()
    skipped = H.resume_message(h, "skipped").lower()

    assert done != skipped
    # Each carries its own distinguishing instruction, not merely other words.
    assert "verify" in done and "do not assume" in done
    assert "abandon" in skipped and "do not retry" in skipped


def test_neither_resume_message_is_a_bare_continue(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    for outcome in ("done", "skipped", "taken_over"):
        msg = H.resume_message(h, outcome)
        assert len(msg.split()) >= 12, f"{outcome} is too thin to act on"


def test_taken_over_tells_the_agent_to_stand_down(tmp_path):
    """Sam is on the keyboard. Two hands on the same screen is the failure."""
    path = tmp_path / "handoffs.json"
    msg = H.resume_message(_raise(path), "taken_over").lower()
    assert "wait" in msg
    assert "do not touch" in msg or "hands off" in msg


def test_resume_message_recalls_what_was_blocked(tmp_path):
    """The agent may be resumed hours later with the block long out of context."""
    path = tmp_path / "handoffs.json"
    h = _raise(path)
    msg = H.resume_message(h, "done")
    assert "Confirm the card payment" in msg
    assert h.id in msg


def test_resume_message_rejects_an_unknown_outcome(tmp_path):
    path = tmp_path / "handoffs.json"
    with pytest.raises(ValueError):
        H.resume_message(_raise(path), "maybe")


def test_resume_message_is_redacted_too(tmp_path):
    path = tmp_path / "handoffs.json"
    h = _raise(path, kind="2fa",
               needs="Type the SMS code 908171 into the prompt.",
               state="Nothing published yet.")
    msg = H.resume_message(h, "done")
    assert "908171" not in msg
    assert H.REDACTED in msg


# ── evidence ───────────────────────────────────────────────────────────────


def test_evidence_is_the_tail_not_the_head(tmp_path):
    lines = [f"line {i}" for i in range(200)]
    ev = H.evidence_from_transcript(lines, limit=5)
    assert "line 199" in ev
    assert "line 0\n" not in ev
    assert len(ev.splitlines()) == 5


def test_evidence_redacts_secrets_with_the_same_rule(tmp_path):
    ev = H.evidence_from_transcript([
        "connecting...",
        "password: hunter2",
        "one-time code 553412",
    ])
    assert "hunter2" not in ev
    assert "553412" not in ev
    assert "connecting" in ev
    assert H.REDACTED in ev


def test_evidence_of_nothing_is_empty(tmp_path):
    assert H.evidence_from_transcript([]) == ""
    assert H.evidence_from_transcript(["", "   "]) == ""


def test_evidence_survives_a_round_trip_through_the_store(tmp_path):
    path = tmp_path / "handoffs.json"
    ev = H.evidence_from_transcript(["waiting on 2FA prompt", "timed out"])
    h = _raise(path, evidence=ev)
    assert H.load(path)[0].evidence == ev


def test_default_path_lives_on_the_bus(tmp_path):
    from server.paths import BUS_DIR
    assert H.DEFAULT_PATH == BUS_DIR / "handoffs.json"
