"""S9d: a desk that wakes or starts while its account is spent starts on the
next account at once -- not five minutes later, after a turn spent at the limit.

`policy.Placer` uses the same rule as the sweep (the threshold, or a limit the
desk just hit). At START it rewrites the desk's account before anything is
spawned; at WAKE it hands the desk to the mover (a live one `at_boundary`,
behind the mover's mid-turn gate), which resumes it in the new account. `policy.PLACER` is None until the app installs it, so every other
caller and every other test is unchanged.
"""

from __future__ import annotations

import json

import pytest

from server import accounts, deckconfig, policy, roster, spawn, wake


def _m(pct):
    return {"available": True, "reason_code": None,
            "limits": [{"key": "session", "percent": pct}, {"key": "weekly_all", "percent": 1}]}


ON = deckconfig.DeckConfig(accounts=deckconfig.AccountsSection(
    policy="failover", failover_allowed=True, failover_order=("main", "work")))


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)


class Rig:
    def __init__(self, main_pct=95.0, cfg=ON, live=False, limited=False):
        self.moved, self.saved, self.polls = [], [], 0
        self.desk = roster.Desk(name="atlas", cwd="/w", engine="claude", mission="m")

        def meters():
            self.polls += 1
            return [(accounts.main_account(), _m(main_pct)), (accounts.get("work"), _m(5))]

        class _Mover:
            def move(_s, name, to, **kw):
                self.moved.append((name, to))
                self.kw = kw

        self.placer = policy.Placer(
            load_cfg=lambda: cfg, meters=meters, mover=_Mover(),
            live=lambda name: {"sid"} if live else set(),
            desk_of=lambda name: self.desk if name == "atlas" else None,
            save_account=lambda name, to: self.saved.append((name, to)),
            limited=lambda desk: limited)


def test_a_desk_started_while_main_is_spent_starts_on_work(reg):
    rig = Rig()
    got = rig.placer.on_start(rig.desk)
    assert got.account == "work" and rig.saved == [("atlas", "work")]


def test_a_start_under_the_threshold_is_untouched(reg):
    rig = Rig(main_pct=40)
    assert rig.placer.on_start(rig.desk) is rig.desk and rig.saved == []


def test_a_fixed_policy_does_not_even_read_the_meters(reg):
    rig = Rig(cfg=deckconfig.DeckConfig())
    assert rig.placer.on_start(rig.desk) is rig.desk
    rig.placer.on_wake("atlas")
    assert rig.polls == 0 and rig.moved == []


def test_an_asleep_desk_woken_while_main_is_spent_moves_first(reg):
    rig = Rig()
    rig.placer.on_wake("atlas")
    assert rig.moved == [("atlas", "work")]


def test_an_asleep_desk_that_hit_a_limit_moves_on_wake(reg):
    rig = Rig(main_pct=40, limited=True)
    rig.placer.on_wake("atlas")
    assert rig.moved == [("atlas", "work")]


def test_a_live_desk_about_to_get_a_message_moves_at_its_turn_boundary(reg):
    """Its next turn is about to start: move it first, behind the mover's
    mid-turn gate -- not on a sweep that has to land in a 20 s idle gap."""
    rig = Rig(live=True)
    rig.placer.on_wake("atlas")
    assert rig.moved == [("atlas", "work")] and rig.kw == {"at_boundary": True}


def test_a_placer_failure_never_blocks_the_wake(reg):
    rig = Rig()

    class _Boom:
        def move(self, *a, **k):
            raise RuntimeError("box on fire")

    rig.placer.mover = _Boom()
    rig.placer.on_wake("atlas")  # no raise


# -- the hooks ------------------------------------------------------------------


def test_spawn_start_asks_the_placer_first(reg, monkeypatch, tmp_path):
    seen = []

    class _P:
        def on_start(self, desk):
            return roster.Desk(**{**desk.__dict__, "account": "work"})

    monkeypatch.setattr(policy, "PLACER", _P())
    monkeypatch.setattr(spawn, "retire", lambda name: [])
    monkeypatch.setattr(spawn.office, "conversation", lambda name, **kw: "")
    monkeypatch.setattr(spawn, "spawn_background", lambda desk, **kw: (
        seen.append(desk.account) or {"ok": True, "agent_id": "x", "pretrust": {}}))
    spawn.start(roster.Desk(name="a", cwd="/w", engine="claude", mission="m"),
                roster_path=tmp_path / "r.json",
                channel=spawn.Channel("background", "no_osascript", ""))
    assert seen == ["work"]


def test_the_waker_asks_the_placer_before_it_resumes(reg, monkeypatch):
    order = []

    class _P:
        def on_wake(self, name):
            order.append(("place", name))

    monkeypatch.setattr(policy, "PLACER", _P())
    job = wake.Job(short="s", session_id="sid", cwd="/w", created_at="")
    waker = wake.Waker(
        live=lambda name: set(), running=lambda sid: False, last=lambda name: job,
        desk_of=lambda name: roster.Desk(name=name, cwd="/w", engine="claude", mission="m"),
        resume=lambda sid, **kw: order.append(("resume", sid)) or "sid",
        pending=lambda name: [], ack=lambda mid: None, record=lambda e: None,
        free_bytes=lambda: 1 << 40)
    waker.ensure_awake("atlas", reason="owner_message")
    assert order == [("place", "atlas"), ("resume", "sid")]


def test_the_app_installs_the_placer_with_its_own_mover_and_meters():
    import inspect
    from server import app as app_mod
    src = inspect.getsource(app_mod._start_oauth_refresher)
    assert "policy.PLACER = policy.Placer(" in src
    assert "meters=_meters.poll_all" in src and "mover=_mover" in src
