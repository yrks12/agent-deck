"""Pending things must expire — and expiring must never mean "yes".

Written before `server/expiry.py` exists, and before `asking.expire` /
`handoff.expire` exist.

The test that matters here is `test_an_expired_ask_grants_nothing`. An ask is a
question the agent is *still sitting on* — its own permission prompt is drawn
and waiting. Timing that question out has to remove it from Sam's board and
nothing else. The day expiry starts writing an `always_allow` rule, or reading
as an allow, this feature has handed the machine to whichever agent asked the
scariest question at 2am and then waited four hours.

Every "it is gone" assertion below is paired with a live record that is still
there, because an empty list is what a broken sweep looks like too.
"""

from server import asking, expiry, handoff
from server.autoreview import evaluate, load_rules

ACME = "/Users/samcarter/Projects/acme"


def raise_one(tmp_path, **kw):
    """One handoff with sensible defaults. Returns (path, handoff)."""
    path = tmp_path / "handoffs.json"
    fields = {
        "agent": "acme-growth",
        "kind": "payment",
        "needs": "Confirm the £99 payment in the browser.",
        "state": "Card entered, nothing charged yet.",
        "where": "https://dashboard.stripe.com",
    }
    fields.update(kw)
    return path, handoff.raise_handoff(path, **fields)


def age(path, handoff_id, seconds, *, loader, saver):
    """Backdate one record's ts so a sweep can see it as old."""
    from dataclasses import replace

    rows = loader(path)
    rows = [replace(r, ts=r.ts - seconds) if r.id == handoff_id else r
            for r in rows]
    saver(path, rows)


# ── the shared window ──────────────────────────────────────────────────────


def test_both_queues_share_one_window():
    """One constant, one meaning. Two windows drifting apart is the bug."""
    assert expiry.DEFAULT_TTL > 0
    assert asking.DEFAULT_TTL == expiry.DEFAULT_TTL
    assert handoff.DEFAULT_TTL == expiry.DEFAULT_TTL


# ── asks ───────────────────────────────────────────────────────────────────


def test_an_old_ask_expires_and_a_fresh_one_survives(tmp_path):
    path = tmp_path / "asks.json"
    now = 900_000.0
    old = asking.record(path, agent="acme-growth", tool="Bash",
                        subject="gh pr create", cwd=ACME,
                        ts=now - expiry.DEFAULT_TTL - 60)
    fresh = asking.record(path, agent="acme-growth", tool="Bash",
                          subject="npm test", cwd=ACME, ts=now - 60)

    changed = asking.expire(path, now=now)

    assert [a.id for a in changed] == [old.id]
    assert changed[0].status == "expired"

    still = asking.pending(path)
    assert [a.id for a in still] == [fresh.id]
    assert still[0].subject == "npm test"
    assert still[0].status == "pending"


def test_an_expired_ask_grants_nothing(tmp_path):
    """THE test. Expiry denies by omission; it never becomes permission.

    The agent's own prompt is still on its screen unanswered. Nothing here may
    write a rule, and the same call must still evaluate to `ask` afterwards.
    """
    path = tmp_path / "asks.json"
    rules_path = tmp_path / "autoreview.json"
    ask = asking.record(path, agent="acme-growth", tool="Bash",
                        subject="gh pr create", cwd=ACME, ts=1000.0)

    changed = asking.expire(path, now=1000.0 + expiry.DEFAULT_TTL + 1)
    assert [a.id for a in changed] == [ask.id]

    # No rule file was conjured, and none is in it.
    assert not rules_path.exists()
    assert load_rules(rules_path) == []

    # And the engine still grants nothing for the very thing that expired.
    # `abstain`, not `ask`: expiry wrote no rule, so the deck has no opinion --
    # which is the point. What it must never have become is permission.
    verdict = evaluate(load_rules(rules_path), tool_name="Bash",
                       tool_input={"command": "gh pr create"}, cwd=ACME)
    assert verdict.decision == "abstain"
    assert verdict.decision != "allow"

    # The record says expired — not "once", not "always".
    row = next(a for a in asking._load(path) if a.id == ask.id)
    assert row.status == "expired"
    assert row.answered is None


def test_an_answered_ask_is_never_re_marked_expired(tmp_path):
    path = tmp_path / "asks.json"
    ask = asking.record(path, agent="acme-growth", tool="Bash",
                        subject="npm test", cwd=ACME, ts=1000.0)
    asking.answer(path, ask.id, "always", tmp_path / "autoreview.json")

    changed = asking.expire(path, now=1000.0 + expiry.DEFAULT_TTL * 10)

    assert changed == []
    row = next(a for a in asking._load(path) if a.id == ask.id)
    assert row.answered == "always"
    assert row.status == "answered"


def test_expiring_twice_reports_the_change_once(tmp_path):
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme-growth", tool="Bash",
                  subject="gh pr create", cwd=ACME, ts=1000.0)
    now = 1000.0 + expiry.DEFAULT_TTL + 1

    first = asking.expire(path, now=now)
    second = asking.expire(path, now=now + 5)

    assert len(first) == 1
    assert second == []


# ── handoffs ───────────────────────────────────────────────────────────────


def test_an_old_handoff_expires_and_a_fresh_one_still_waits(tmp_path):
    path, old = raise_one(tmp_path, needs="Confirm the £99 payment.")
    _, fresh = raise_one(tmp_path, needs="Type the 2FA code.", kind="2fa")
    age(path, old.id, expiry.DEFAULT_TTL + 60,
        loader=handoff.load, saver=handoff.save)

    import time
    changed = handoff.expire(path, now=time.time())

    assert [h.id for h in changed] == [old.id]
    assert changed[0].status == "expired"

    still = handoff.waiting(path)
    assert [h.id for h in still] == [fresh.id]
    assert still[0].needs == "Type the 2FA code."


def test_an_expired_handoff_is_not_an_outcome(tmp_path):
    """A payment nobody confirmed did not happen. It must not read as done."""
    path, block = raise_one(tmp_path)
    age(path, block.id, expiry.DEFAULT_TTL + 60,
        loader=handoff.load, saver=handoff.save)

    import time
    changed = handoff.expire(path, now=time.time())

    assert changed[0].status == "expired"
    assert changed[0].status not in handoff.OUTCOMES
    assert "expired" in handoff.STATUSES


def test_a_resolved_handoff_is_never_re_marked_expired(tmp_path):
    path, block = raise_one(tmp_path)
    handoff.resolve(path, block.id, "done")
    age(path, block.id, expiry.DEFAULT_TTL * 10,
        loader=handoff.load, saver=handoff.save)

    import time
    changed = handoff.expire(path, now=time.time())

    assert changed == []
    assert handoff.load(path)[0].status == "done"


def test_a_taken_over_handoff_is_never_re_marked_expired(tmp_path):
    """He has the keyboard. A long job is not an abandoned one."""
    path, block = raise_one(tmp_path)
    handoff.resolve(path, block.id, "taken_over")
    age(path, block.id, expiry.DEFAULT_TTL * 10,
        loader=handoff.load, saver=handoff.save)

    import time
    assert handoff.expire(path, now=time.time()) == []
    assert handoff.load(path)[0].status == "taken_over"


def test_expire_on_a_missing_file_is_quiet(tmp_path):
    assert asking.expire(tmp_path / "nope.json", now=1e9) == []
    assert handoff.expire(tmp_path / "nope.json", now=1e9) == []
