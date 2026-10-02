"""Which Claude account a desk should be on (S9, docs/plans/2026-10-01-two-accounts.md).

`choose` is PURE: the desk's account, every account's meter, the policy ->
the account to move it to, or None. The mover does the moving, and its gate
keeps it from ever happening mid-turn.

Owner ruling 2026-10-01: auto-failover is ON for his box, which he chose after
being told the terms are unclear. Every other install defaults OFF:
`failover_allowed = false` makes every mode behave as `fixed`. The app's
toggle (`PUT /v1/accounts/policy`) writes `mode` to a deck-owned file that
overrides deck.toml, but it can never go past `failover_allowed`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import atomic, deckconfig
from .paths import BUS_DIR

STATE_PATH: Path = BUS_DIR / "accounts-policy.json"
MODES = ("fixed", "failover", "failover_api")
#: The windows that gate every desk. A per-model weekly window does not.
GATING = ("session", "weekly_all")
#: Reasons that leave an account usable: its numbers are known, or its login
#: is good and the move itself starts the daemon that refreshes it.
USABLE = (None, "cached", "rate_limited", "unreachable", "idle_token")


class PolicyError(Exception):
    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.status, self.reason, self.detail = status, reason, detail or reason


def used_pct(meter: dict | None) -> float | None:
    """The highest gating window, or None when there are no numbers."""
    found = [float(w.get("percent") or 0.0) for w in (meter or {}).get("limits") or []
             if isinstance(w, dict) and w.get("key") in GATING]
    return max(found) if found else None


def _usable(meter: dict | None, threshold: float) -> bool:
    if not meter or meter.get("reason_code") not in USABLE:
        return False
    pct = used_pct(meter)
    return pct is not None and pct < threshold


def choose(current: str, meters: dict[str, dict], pol: dict) -> str | None:
    """The account to move a desk on `current` to, or None to leave it."""
    mode = pol.get("mode") if pol.get("failover_allowed") else "fixed"
    if mode not in ("failover", "failover_api"):
        return None
    threshold = float(pol.get("threshold_pct") or 90)
    pct = used_pct(meters.get(current))
    if pct is None or pct < threshold:
        return None  # unknown is not spent: never move on a guess
    for ident in pol.get("failover_order") or ():
        if ident != current and _usable(meters.get(ident), threshold):
            return ident
    api = pol.get("api_account") or ""
    if mode == "failover_api" and api and api != current and api in meters:
        return api  # an API account has no plan window to be over
    return None


#: How often the sweeper looks (the plan: a 5-minute idle sweeper). The
#: meters themselves refresh on the same cadence, so faster buys nothing.
SWEEP_SECONDS = 300.0


#: How often a desk's transcript is checked for a limit it just hit (S9c).
LIMIT_SECONDS = 30.0

LIMIT_NOTE = ("[Agent Deck] You hit a usage limit on your previous Claude account, so "
              "this desk was moved to {to}. Same conversation, same memory. Pick up "
              "the work you were doing when the limit stopped you.")


def target_for(current: str, meters: dict[str, dict], pol: dict,
               limited) -> tuple[str | None, str]:
    """(account to move to, why). The threshold first; else, if the desk's last
    turn hit a usage limit (`limited()`), its account counts as spent."""
    target = choose(current, meters, pol)
    if target is not None:
        return target, "threshold"
    if pol.get("failover_allowed") and pol.get("mode") != "fixed" and limited():
        spent = {**meters, current: {"reason_code": None,
                                     "limits": [{"key": "session", "percent": 100.0}]}}
        target = choose(current, spent, pol)
        if target is not None:
            return target, "limit_hit"
    return None, ""


def sweep(*, desks, meters: dict[str, dict], pol: dict, mover,
          limited=lambda desk: False, held: "Held | None" = None) -> list[dict]:
    """Apply the policy once. Every Claude desk `choose` wants moved is handed
    to the mover, whose gate refuses one that is mid-turn (`not_idle`): that
    desk stays where it is and is tried again on the next sweep.

    `limited(desk)`: the desk's last turn ended on a usage-limit error (S9c).
    Its account counts as spent for it whatever the meter says, and it is told
    why it moved so it resumes its work."""
    from . import accounts
    from .mover import MoveError

    out = []
    for desk in desks:
        if desk.engine != "claude":
            continue
        current = accounts.for_desk(desk).id
        target, why = target_for(current, meters, pol, lambda: limited(desk))
        if target is None:
            if held is not None:
                held.clear(desk.name, "no longer over the threshold")
            continue
        try:
            if why == "limit_hit":
                mover.move(desk.name, target, note=LIMIT_NOTE.format(to=target))
            else:
                mover.move(desk.name, target)
            outcome = "moved"
        except MoveError as exc:
            outcome = exc.reason
            if held is not None:
                held.refused(desk.name, current, target, exc.detail, meters.get(current))
        if held is not None and outcome == "moved":
            held.clear(desk.name, "moved")
        row = {"desk": desk.name, "from": current, "to": target, "outcome": outcome}
        if why == "limit_hit":
            row["why"] = why
        out.append(row)
    return out


def tick(*, load_cfg, meters, desks, mover, limited, held=None) -> list[dict]:
    """One pass of the sweeper. Under a `fixed` policy it reads deck.toml and
    nothing else (no meters, no roster, no transcripts)."""
    pol = effective(load_cfg())
    if pol["mode"] == "fixed":
        return []
    return sweep(desks=desks(), meters={a.id: p for a, p in meters()}, pol=pol,
                 mover=mover, limited=limited, held=held)


#: A desk held on a spent account this long gets one card in Atlas's thread.
HELD_CARD_SECONDS = 1800.0
#: How often the watcher looks for a held desk's turn ending.
BOUNDARY_SECONDS = 1.0


def _pct(meter: dict | None) -> float:
    return max((float(x.get("percent") or 0) for x in (meter or {}).get("limits") or ()),
               default=0.0)


class Held:
    """Desks the policy wants moved that the gate refused. MEASURED 2026-10-01:
    `not_idle` every 30 s for hours, nobody told. Now one line per reason, a
    move the moment the turn ends, and ONE card to Atlas after 30 minutes."""

    def __init__(self, *, log=print, card=lambda text: None, clock=time.time) -> None:
        self.log, self.card, self.clock = log, card, clock
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def refused(self, desk: str, src: str, dst: str, why: str, meter=None) -> None:
        from . import accounts
        now = self.clock()
        with self._lock:
            row = self.rows.setdefault(desk, {"since": now, "carded": False, "why": ""})
            row.update(src=src, dst=dst, pct=_pct(meter))
            fresh, row["why"] = row["why"] != why, why
            due = not row["carded"] and now - row["since"] >= HELD_CARD_SECONDS
            row["carded"] = row["carded"] or due
        if fresh:
            self.log(f"accounts: {desk} held on {src} (wants {dst}): {why}; "
                     "moves at its next turn end")
        if due:
            label = (accounts.get(src) or accounts.main_account()).label
            self.card(f"{desk} is still on {label} ({row['pct']:.0f}%) because {why}")

    def clear(self, desk: str, outcome: str) -> None:
        with self._lock:
            row = self.rows.pop(desk, None)
        if row is not None:
            self.log(f"accounts: {desk} {row['src']} -> {row['dst']}: {outcome}")

    def at_boundary(self, mover) -> None:
        """Move each held desk whose turn has just ended, before the next one."""
        from .mover import MoveError
        for desk, row in list(self.rows.items()):
            try:
                if not mover.turn_over(desk):
                    continue
                mover.move(desk, row["dst"], at_boundary=True)
            except MoveError as exc:
                self.refused(desk, row["src"], row["dst"], exc.detail)
                continue
            except Exception:  # noqa: BLE001 - the next sweep tries again
                continue
            self.clear(desk, "moved at its turn end")


#: Installed by the app (S9d). None: every start and wake is what it was.
PLACER = None


class Placer:
    """The policy at the moment a desk wakes or starts (S9d), not only every
    LIMIT_SECONDS: a desk that comes up while its account is spent comes up on
    the next one. Same rule as the sweep (`target_for`)."""

    def __init__(self, *, load_cfg, meters, mover, live, desk_of, save_account,
                 limited) -> None:
        self.load_cfg, self.meters, self.mover = load_cfg, meters, mover
        self.live, self.desk_of, self.save_account = live, desk_of, save_account
        self.limited = limited

    def target(self, desk) -> str | None:
        from . import accounts
        if desk.engine != "claude":
            return None
        pol = effective(self.load_cfg())
        if pol["mode"] == "fixed":
            return None
        meters = {a.id: p for a, p in self.meters()}
        target, _why = target_for(accounts.for_desk(desk).id, meters, pol,
                                  lambda: self.limited(desk))
        return target

    def on_start(self, desk):
        """The desk to start: on the next account if its own is spent."""
        import dataclasses
        from . import accounts
        try:
            target = self.target(desk)
        except Exception:  # noqa: BLE001 - placement never blocks a start
            return desk
        if target is None:
            return desk
        self.save_account(desk.name, target)
        stored = "" if target == accounts.DEFAULT_ID else target
        return dataclasses.replace(desk, account=stored)

    def on_wake(self, name: str) -> None:
        """An ASLEEP desk about to be woken moves first; the mover's resume is
        the wake. A LIVE one about to be handed a message is at a turn
        boundary: it moves now unless the CLI says it is mid-turn."""
        try:
            if effective(self.load_cfg())["mode"] == "fixed":
                return  # every install without failover_allowed: nothing read
            desk = self.desk_of(name)
            if desk is None:
                return
            target = self.target(desk)
            if target is not None:
                if self.live(name):
                    self.mover.move(name, target, at_boundary=True)
                else:
                    self.mover.move(name, target)
        except Exception:  # noqa: BLE001 - placement never blocks a wake
            return


def _override(path: Path) -> str | None:
    try:
        mode = json.loads(path.read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        return None
    return mode if mode in MODES else None


def effective(cfg: deckconfig.DeckConfig, path: Path | None = None) -> dict:
    """deck.toml's [accounts], with the app's toggle on top -- capped by
    `failover_allowed`, which only deck.toml can grant."""
    a = cfg.accounts
    mode = _override(Path(path or STATE_PATH)) or a.policy
    if not a.failover_allowed:
        mode = "fixed"
    return {"mode": mode, "threshold_pct": a.threshold_pct,
            "failover_allowed": a.failover_allowed, "default": a.default,
            "failover_order": list(a.failover_order), "api_account": a.api_account}


def set_mode(cfg: deckconfig.DeckConfig, mode: str, path: Path | None = None) -> dict:
    if mode not in MODES:
        raise PolicyError(400, "bad_mode", f"mode must be one of {', '.join(MODES)}")
    if mode != "fixed" and not cfg.accounts.failover_allowed:
        raise PolicyError(403, "failover_not_allowed",
                          "automatic switching is off on this deck; its owner turns it on "
                          "with  deckctl config set accounts.failover_allowed true")
    target = Path(path or STATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(target, json.dumps({"mode": mode}), mode=0o600)
    return effective(cfg, target)
