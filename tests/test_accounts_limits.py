"""S9c: a desk that HITS a usage limit moves on its next idle tick.

The meter (S9) only sees 90%; a desk can hit a limit first -- a per-model
window, a burst. The CLI writes the hit into the desk's transcript.

MEASURED in the box's binary (claude 2.1.287, `strings`):
  * the CLI's own list of limit-message prefixes (`r8r`): "You've hit your",
    "You've reached your", "You're out of usage credits", "You're out of
    extra usage", ... and the composer `You've hit your ${limit}${resets}`;
  * the job states it maps API errors to: `billing_error` -> "usage limit
    reached -- check plan", `rate_limit` -> "rate limited -- wait and retry".
MEASURED in real transcripts on the box: an API error is an assistant line
with `isApiErrorMessage: true`, a top-level `error` kind, and the text --
e.g. `error: "authentication_failed"`, "Login expired · Please run /login".
That one is NOT a limit and must not move a desk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import accounts, limits, mover, policy, roster


def _api_error(error: str, text: str) -> str:
    return json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": error,
                       "message": {"role": "assistant", "model": "<synthetic>",
                                   "content": [{"type": "text", "text": text}]}})


def _said(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}]}})


USER = json.dumps({"type": "user", "message": {"role": "user", "content": "go on"}})
#: Built from the binary's own strings (see the module docstring).
SESSION_LIMIT = _api_error("rate_limit", "You've hit your session limit · resets 3pm")
WEEKLY_LIMIT = _api_error("rate_limit", "You've hit your weekly limit · resets Oct 4, 9am")
EXTRA_USAGE = _api_error("billing_error", "You're out of extra usage · add funds to continue")
LOGIN = _api_error("authentication_failed", "Login expired · Please run /login")
THROTTLE = _api_error("rate_limit", "API Error: 429 overloaded, retrying failed")


def _transcript(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.mark.parametrize("hit", [SESSION_LIMIT, WEEKLY_LIMIT, EXTRA_USAGE])
def test_the_clis_limit_messages_are_a_hit(tmp_path, hit):
    assert limits.limit_hit(_transcript(tmp_path, USER, _said("working"), USER, hit))


@pytest.mark.parametrize("other", [LOGIN, THROTTLE])
def test_a_login_error_or_a_plain_429_is_not(tmp_path, other):
    assert not limits.limit_hit(_transcript(tmp_path, USER, other))


def test_a_desk_that_has_answered_since_is_not_limited(tmp_path):
    assert not limits.limit_hit(_transcript(tmp_path, USER, SESSION_LIMIT, USER, _said("ok")))


def test_a_missing_or_broken_transcript_is_not(tmp_path):
    assert not limits.limit_hit(tmp_path / "nope.jsonl")
    assert not limits.limit_hit(_transcript(tmp_path, "{broken", "[]"))


# -- the sweep acts on it ------------------------------------------------------

POL = {"mode": "failover", "threshold_pct": 90, "failover_order": ["main", "work"],
       "api_account": "", "failover_allowed": True}


def _m(pct):
    return {"available": True, "reason_code": None,
            "limits": [{"key": "session", "percent": pct}, {"key": "weekly_all", "percent": 1}]}


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)


def test_a_limited_desk_moves_even_when_the_meter_is_under_the_threshold(reg):
    moved = []

    class _Mover:
        def move(self, name, to, **kw):
            moved.append((name, to, kw.get("note", "")))
            return {"moved": True}

    desks = [roster.Desk(name="hit", cwd="/w", engine="claude", mission="m"),
             roster.Desk(name="fine", cwd="/w", engine="claude", mission="m")]
    got = policy.sweep(desks=desks, meters={"main": _m(40), "work": _m(5)}, pol=POL,
                       mover=_Mover(), limited=lambda desk: desk.name == "hit")
    assert [(n, t) for n, t, _ in moved] == [("hit", "work")]
    assert "limit" in moved[0][2].lower(), "the desk is told why, so it picks up its work"
    assert got == [{"desk": "hit", "from": "main", "to": "work", "outcome": "moved",
                    "why": "limit_hit"}]


def test_a_limit_hit_never_moves_where_failover_is_not_allowed(reg):
    class _Never:
        def move(self, *a, **k):
            raise AssertionError("moved without failover_allowed")

    desk = roster.Desk(name="hit", cwd="/w", engine="claude", mission="m")
    assert policy.sweep(desks=[desk], meters={"main": _m(40), "work": _m(5)},
                        pol={**POL, "failover_allowed": False}, mover=_Never(),
                        limited=lambda d: True) == []


def test_one_tick_reads_the_policy_the_meters_and_each_desks_transcript(reg, monkeypatch):
    from server import deckconfig
    cfg = deckconfig.DeckConfig(accounts=deckconfig.AccountsSection(
        policy="failover", failover_allowed=True, failover_order=("main", "work")))
    moved = []

    class _Mover:
        def move(self, name, to, **kw):
            moved.append((name, to))

    rows = policy.tick(load_cfg=lambda: cfg,
                       meters=lambda: [(accounts.main_account(), _m(40)),
                                       (accounts.get("work"), _m(5))],
                       desks=lambda: [roster.Desk(name="hit", cwd="/w", engine="claude",
                                                  mission="m")],
                       mover=_Mover(), limited=lambda d: True)
    assert moved == [("hit", "work")] and rows[0]["why"] == "limit_hit"
    fixed = deckconfig.DeckConfig()
    assert policy.tick(load_cfg=lambda: fixed, meters=lambda: 1 / 0, desks=lambda: 1 / 0,
                       mover=_Mover(), limited=lambda d: True) == []


def test_the_app_ticks_at_the_limit_cadence_with_the_transcript_check():
    from server import app as app_mod
    import inspect
    src = inspect.getsource(app_mod._sweep_accounts_forever)
    assert "LIMIT_SECONDS" in src and "limits.desk_limited" in src and "policy.tick" in src


def test_the_mover_carries_a_custom_note():
    seen = []
    m = mover.Mover(card_of=lambda n: None,
                    desk_of=lambda n: roster.Desk(name=n, cwd="/w", engine="claude", mission="m"),
                    last_job=lambda n: None, live=lambda n: set(),
                    save_account=lambda n, t: seen.append(t), record=lambda e: None)
    # Never-run desk: no resume, so just prove the keyword is accepted.
    accounts_get = accounts.get
    try:
        accounts.get = lambda ident, path=None: accounts.Account(
            ident, ident, "subscription", Path("/x")) if ident == "work" else accounts_get(ident)
        assert m.move("d", "work", note="custom")["moved"] is True
    finally:
        accounts.get = accounts_get
