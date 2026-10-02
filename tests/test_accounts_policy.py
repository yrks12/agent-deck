"""S9a: which account a desk should be on -- a pure decision.

Owner ruling 2026-10-01: auto-failover ON for his box (`failover_allowed =
true`, `threshold_pct = 90`), told the terms are unclear. Default for every
other install: OFF. `choose` never moves a desk off an account that is under
the threshold, and never onto one that is over it, spent, or signed out. The
app's toggle writes `mode` into a deck-owned file that overrides deck.toml --
but never past `failover_allowed`.
"""

from __future__ import annotations

import json
import stat

import pytest

from server import deckconfig, policy


def _meter(session=10.0, weekly=10.0, *, available=True, reason=None, stale=False):
    return {"available": available, "stale": stale, "reason_code": reason,
            "limits": [{"key": "session", "percent": session},
                       {"key": "weekly_all", "percent": weekly},
                       {"key": "weekly_scoped:Opus", "percent": 99.0}]}


POL = {"mode": "failover", "threshold_pct": 90, "failover_order": ["main", "work"],
       "api_account": "", "failover_allowed": True}


def test_fixed_never_moves():
    meters = {"main": _meter(99), "work": _meter(1)}
    assert policy.choose("main", meters, {**POL, "mode": "fixed"}) is None


def test_under_the_threshold_stays():
    assert policy.choose("main", {"main": _meter(89.9, 50), "work": _meter(1)}, POL) is None


@pytest.mark.parametrize("main", [_meter(90, 10), _meter(10, 95)])
def test_at_the_threshold_on_either_window_moves_to_the_next_in_order(main):
    assert policy.choose("main", {"main": main, "work": _meter(5, 5)}, POL) == "work"


def test_a_scoped_weekly_window_does_not_count():
    """Only the 5-hour and the all-models weekly windows gate every desk."""
    assert policy.choose("main", {"main": _meter(10, 10), "work": _meter()}, POL) is None


@pytest.mark.parametrize("work", [
    _meter(95, 5),
    _meter(available=False, reason="token_expired"),
    _meter(available=False, reason="no_login"),
    _meter(available=False, reason="meter_off"),
])
def test_never_onto_an_account_that_is_spent_or_signed_out(work):
    assert policy.choose("main", {"main": _meter(95), "work": work}, POL) is None


def test_an_idle_login_is_still_a_place_to_go():
    """`idle_token`: its daemon is not running, the login is good; the move
    itself starts the daemon that refreshes it."""
    work = _meter(5, 5, available=False, reason="idle_token", stale=True)
    assert policy.choose("main", {"main": _meter(95), "work": work}, POL) == "work"


def test_a_desk_on_work_goes_back_to_main_only_when_work_is_spent():
    meters = {"main": _meter(5), "work": _meter(50)}
    assert policy.choose("work", meters, POL) is None
    meters["work"] = _meter(92)
    assert policy.choose("work", meters, POL) == "main"


def test_failover_api_falls_through_to_the_api_account():
    pol = {**POL, "mode": "failover_api", "api_account": "api"}
    meters = {"main": _meter(95), "work": _meter(95),
              "api": _meter(available=False, reason="no_login")}
    assert policy.choose("main", meters, pol) == "api"
    assert policy.choose("main", meters, {**pol, "mode": "failover"}) is None


def test_failover_not_allowed_means_fixed_whatever_the_mode_says():
    assert policy.choose("main", {"main": _meter(99), "work": _meter(1)},
                         {**POL, "failover_allowed": False}) is None


# -- the effective policy and the app's toggle --------------------------------


def _cfg(**kw):
    return deckconfig.DeckConfig(accounts=deckconfig.AccountsSection(**kw))


def test_effective_is_deck_toml_when_nothing_was_toggled(tmp_path):
    got = policy.effective(_cfg(policy="failover", failover_allowed=True),
                           tmp_path / "accounts-policy.json")
    assert got["mode"] == "failover" and got["failover_allowed"] is True


def test_the_toggle_overrides_deck_toml_and_is_private(tmp_path):
    path = tmp_path / "accounts-policy.json"
    cfg = _cfg(policy="fixed", failover_allowed=True)
    assert policy.set_mode(cfg, "failover", path)["mode"] == "failover"
    assert policy.effective(cfg, path)["mode"] == "failover"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert policy.set_mode(cfg, "fixed", path)["mode"] == "fixed"


def test_the_toggle_cannot_pass_failover_allowed(tmp_path):
    path = tmp_path / "accounts-policy.json"
    with pytest.raises(policy.PolicyError) as err:
        policy.set_mode(_cfg(failover_allowed=False), "failover", path)
    assert (err.value.status, err.value.reason) == (403, "failover_not_allowed")
    assert not path.exists()
    # Turning it OFF is always allowed.
    assert policy.set_mode(_cfg(failover_allowed=False), "fixed", path)["mode"] == "fixed"


def test_a_mode_the_deck_does_not_know_is_refused(tmp_path):
    with pytest.raises(policy.PolicyError) as err:
        policy.set_mode(_cfg(failover_allowed=True), "roulette", tmp_path / "p.json")
    assert (err.value.status, err.value.reason) == (400, "bad_mode")


def test_a_stale_override_never_beats_a_revoked_permission(tmp_path):
    path = tmp_path / "accounts-policy.json"
    path.write_text(json.dumps({"mode": "failover"}))
    got = policy.effective(_cfg(failover_allowed=False), path)
    assert got["mode"] == "fixed"
