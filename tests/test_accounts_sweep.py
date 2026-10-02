"""S9b: the 5-minute sweep that applies the policy, and the app's toggle.

`policy.sweep` asks `choose` for every Claude desk and hands each "move" to the
mover, whose gate refuses a desk that is mid-turn (`not_idle`); that desk is
tried again on the next sweep. `PUT /v1/accounts/policy` is the app's
auto-switch toggle; `GET /v1/usage` reports the EFFECTIVE policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import accounts, deckconfig, mover, policy, roster
from server import api as api_mod

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
    monkeypatch.setattr(policy, "STATE_PATH", tmp_path / "accounts-policy.json")
    return path


def _desk(name, engine="claude", account=""):
    return roster.Desk(name=name, cwd="/w", engine=engine, mission="m", account=account)


def test_the_sweep_moves_only_desks_on_a_spent_account(reg):
    moved = []

    class _Mover:
        def move(self, name, to):
            if name == "busy":
                raise mover.MoveError(409, "not_idle", "mid-turn")
            moved.append((name, to))
            return {"ok": True, "moved": True}

    desks = [_desk("a"), _desk("busy"), _desk("on-work", account="work"),
             _desk("oc", engine="opencode")]
    got = policy.sweep(desks=desks, meters={"main": _m(95), "work": _m(10)},
                       pol=POL, mover=_Mover())
    assert moved == [("a", "work")]
    assert {r["desk"]: r["outcome"] for r in got} == {"a": "moved", "busy": "not_idle"}


def test_a_fixed_policy_sweeps_nothing(reg):
    class _Never:
        def move(self, name, to):
            raise AssertionError("moved under a fixed policy")

    assert policy.sweep(desks=[_desk("a")], meters={"main": _m(99), "work": _m(1)},
                        pol={**POL, "mode": "fixed"}, mover=_Never()) == []


# -- the toggle route ------------------------------------------------------------


@pytest.fixture
def client(reg, monkeypatch):
    from server import accounts_api
    monkeypatch.setenv(api_mod.TOKEN_ENV, "t")
    allowed = {"value": True}
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: deckconfig.DeckConfig(
        accounts=deckconfig.AccountsSection(failover_allowed=allowed["value"],
                                            failover_order=("main", "work"))))
    app = FastAPI()
    api_mod.register(app, surface=api_mod.Surface(
        snapshot=lambda: {"generated_at": 1.0, "sessions": []},
        roster_path=Path("/nonexistent/r.json"), prefs_path=Path("/nonexistent/p.json")),
        background=False)

    class _Meters:
        def poll_all(self):
            return []

    app.include_router(accounts_api.build_router(meters=_Meters(), mover=object()))
    c = TestClient(app)
    c.allowed = allowed
    return c


AUTH = {"Authorization": "Bearer t"}


def test_the_toggle_writes_the_mode_and_answers_the_effective_policy(client):
    r = client.put("/v1/accounts/policy", json={"mode": "failover"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["mode"] == "failover"
    assert policy.effective(deckconfig.load())["mode"] == "failover"
    r = client.put("/v1/accounts/policy", json={"mode": "fixed"}, headers=AUTH)
    assert r.json()["mode"] == "fixed"


def test_the_toggle_is_refused_where_failover_is_not_allowed(client):
    client.allowed["value"] = False
    r = client.put("/v1/accounts/policy", json={"mode": "failover"}, headers=AUTH)
    assert r.status_code == 403 and r.json()["reason"] == "failover_not_allowed"


def test_the_toggle_refuses_a_mode_it_does_not_know(client):
    r = client.put("/v1/accounts/policy", json={"mode": "roulette"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["reason"] == "bad_mode"
    assert client.put("/v1/accounts/policy", json={"mode": "fixed"}).status_code == 401


def test_usage_reports_the_effective_policy(reg, monkeypatch):
    from server import usage_api
    monkeypatch.setattr(deckconfig, "load", lambda *a, **k: deckconfig.DeckConfig(
        accounts=deckconfig.AccountsSection(policy="fixed", failover_allowed=True)))
    policy.set_mode(deckconfig.load(), "failover")
    assert usage_api._policy()["mode"] == "failover"


class _Held:
    def __init__(self):
        self.now, self.lines, self.cards = 0.0, [], []
        self.h = policy.Held(log=self.lines.append, card=self.cards.append,
                             clock=lambda: self.now)


class _Busy:
    def __init__(self, why="its session is in the middle of a turn"):
        self.why, self.over, self.moved = why, False, []

    def move(self, name, to, **kw):
        if not (kw.get("at_boundary") and self.over):
            raise mover.MoveError(409, "not_idle", self.why)
        self.moved.append((name, to, kw))
        return {"ok": True, "moved": True}

    def turn_over(self, name):
        return self.over


def _spent(pct=94):
    return {"available": True, "reason_code": None,
            "limits": [{"key": "session", "percent": 3}, {"key": "weekly_all", "percent": pct}]}


def test_a_held_desk_is_logged_once_per_reason_not_every_sweep(reg):
    """MEASURED on the box 2026-10-01: `not_idle` twice a minute for hours."""
    rig, busy = _Held(), _Busy()
    for _ in range(5):
        policy.sweep(desks=[_desk("busy-desk")], meters={"main": _spent(), "work": _m(5)},
                     pol=POL, mover=busy, held=rig.h)
    assert len(rig.lines) == 1 and "middle of a turn" in rig.lines[0]
    busy.why = "a subagent of it is still running"
    policy.sweep(desks=[_desk("busy-desk")], meters={"main": _spent(), "work": _m(5)},
                 pol=POL, mover=busy, held=rig.h)
    assert len(rig.lines) == 2 and rig.cards == []


def test_a_desk_held_30_minutes_gets_one_card_in_atlas_thread(reg):
    rig, busy = _Held(), _Busy()
    for t in (0, 1799, 1800, 1830, 4000):
        rig.now = t
        policy.sweep(desks=[_desk("busy-desk")], meters={"main": _spent(), "work": _m(5)},
                     pol=POL, mover=busy, held=rig.h)
    assert rig.cards == ["busy-desk is still on Main (94%) because "
                         "its session is in the middle of a turn"]


def test_a_busy_desk_moves_the_moment_its_turn_ends(reg):
    rig, busy = _Held(), _Busy()
    policy.sweep(desks=[_desk("busy-desk")], meters={"main": _spent(), "work": _m(5)},
                 pol=POL, mover=busy, held=rig.h)
    rig.h.at_boundary(busy)  # still mid-turn: nothing
    assert busy.moved == [] and "busy-desk" in rig.h.rows
    busy.over = True
    rig.h.at_boundary(busy)
    assert busy.moved == [("busy-desk", "work", {"at_boundary": True})]
    assert rig.h.rows == {} and "moved at its turn end" in rig.lines[-1]


def test_a_desk_no_longer_over_the_threshold_is_let_go(reg):
    rig, busy = _Held(), _Busy()
    policy.sweep(desks=[_desk("quiet-desk")], meters={"main": _spent(), "work": _m(5)},
                 pol=POL, mover=busy, held=rig.h)
    policy.sweep(desks=[_desk("quiet-desk")], meters={"main": _m(10), "work": _m(5)},
                 pol=POL, mover=busy, held=rig.h)
    assert rig.h.rows == {}
