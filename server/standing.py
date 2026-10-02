"""Standing approvals: an "always" with limits, set once by the owner.

Owner, 2026-10-02: he sets a policy once and desks act within it without a
card each time; anything outside it still raises the card it raises today.

A policy is NOT a second permission system. It is evaluated at the same
decision point as an "always" rule (`server/app.py` `_approve` and
`_permission`, beside `autoreview.evaluate`), it only ever turns the deck's
"no opinion" into an allow, and it never lifts a deny, a require-approval
rule or the floor in `standing_floor`. This module holds the policies; the
counting and the decision live in `standing_usage`.

Life cycle: `proposed` (a desk asked for it -- grants nothing) -> `active`
(the owner said yes) -> `revoked`. An owner-created policy starts active.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import atomic, standing_floor
from .paths import BUS_DIR

DEFAULT_PATH: Path = BUS_DIR / "standing.json"

KINDS = ("send_email", "spend_money", "post_comment", "run_command",
         "call_api")
STATUSES = ("proposed", "active", "revoked")
#: The tool each kind means when a policy names none.
DEFAULT_TOOL = {"run_command": "Bash", "send_email": "*", "spend_money": "*",
                "post_comment": "*", "call_api": "*"}

_LOCK = threading.Lock()


class StandingError(Exception):
    """A refusal in the deck's `reason` style; the API maps it to `Refused`."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.status, self.reason, self.detail = status, reason, detail or reason


@dataclass(frozen=True)
class Policy:
    id: str
    desk: str                     # a desk name, or "*" for every desk
    kind: str                     # one of KINDS
    tool: str = "*"               # fnmatch on the tool name
    pattern: str = "*"            # what the action must match (see usage)
    count_per_day: int | None = None
    usd_per_day: float | None = None
    recipients: tuple[str, ...] = ()   # addresses, or "@domain" / "domain"
    account: str = ""             # e.g. the sender address it may use
    expires_at: float | None = None
    created_by: str = ""
    created_at: float = 0.0
    status: str = "proposed"
    note: str = ""
    approved_at: float | None = None
    revoked_at: float | None = None

    def live(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.status == "active" and (
            self.expires_at is None or self.expires_at > now)

    def wire(self) -> dict:
        row = asdict(self)
        limits = {k: row.pop(k) for k in
                  ("count_per_day", "usd_per_day", "recipients", "account")}
        limits["recipients"] = list(limits["recipients"])
        return {**row, "limits": limits}


def summary(policy: Policy) -> str:
    """One line a lock screen can carry: what the policy would let a desk do.

    e.g. "Always, up to a limit? scout: send email, 80/day, to @acme.com --
    weekly updates to Acme"."""
    who = "every desk" if policy.desk == "*" else policy.desk
    parts = [policy.kind.replace("_", " ")]
    if policy.kind == "run_command" and policy.pattern not in ("", "*"):
        parts[0] += f" `{policy.pattern}`"
    if policy.count_per_day is not None:
        parts.append(f"{policy.count_per_day}/day")
    if policy.usd_per_day is not None:
        parts.append(f"${policy.usd_per_day:g}/day")
    if policy.recipients:
        parts.append("to " + ", ".join(policy.recipients))
    line = f"Always, up to a limit? {who}: {', '.join(parts)}"
    return f"{line} -- {policy.note}" if policy.note else line


def _num(value, kind, name: str):
    if value is None or value == "":
        return None
    try:
        out = kind(value)
    except (TypeError, ValueError):
        raise StandingError(400, "bad_limit", f"{name} must be a number")
    if isinstance(value, bool) or out <= 0:
        raise StandingError(400, "bad_limit", f"{name} must be above zero")
    return out


def _from_raw(raw: dict) -> Policy | None:
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        return None
    limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else raw
    try:
        return Policy(
            id=str(raw.get("id") or ""), desk=str(raw.get("desk") or ""),
            kind=raw["kind"], tool=str(raw.get("tool") or "*"),
            pattern=str(raw.get("pattern") or "*"),
            count_per_day=_num(limits.get("count_per_day"), int, "count"),
            usd_per_day=_num(limits.get("usd_per_day"), float, "usd"),
            recipients=tuple(str(r).lower() for r in
                             (limits.get("recipients") or ()) if str(r)),
            account=str(limits.get("account") or "").lower(),
            expires_at=_num(raw.get("expires_at"), float, "expires_at"),
            created_by=str(raw.get("created_by") or ""),
            created_at=float(raw.get("created_at") or 0),
            status=(raw.get("status") if raw.get("status") in STATUSES
                    else "proposed"),
            note=str(raw.get("note") or ""),
            approved_at=raw.get("approved_at"),
            revoked_at=raw.get("revoked_at"))
    except StandingError:
        return None


def load(path: Path = DEFAULT_PATH) -> list[Policy]:
    """Every policy on disk. Unreadable is none -- never a grant."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    rows = raw.get("policies") if isinstance(raw, dict) else None
    out = [_from_raw(r) for r in rows or []] if isinstance(rows, list) else []
    return [p for p in out if p is not None and p.id]


def _save(path: Path, policies: list[Policy]) -> None:
    body = {"version": 1, "policies": [asdict(p) for p in policies]}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(Path(path), json.dumps(body, indent=2))


def validate(policy: Policy) -> Policy:
    """Refuse what can never be honoured, rather than store it."""
    if not policy.desk:
        raise StandingError(400, "missing_field", "desk is required")
    if policy.kind not in KINDS:
        raise StandingError(400, "bad_kind",
                            f"kind must be one of {', '.join(KINDS)}")
    if policy.count_per_day is None and policy.usd_per_day is None:
        raise StandingError(400, "no_limit", "a standing approval needs "
                            "count_per_day or usd_per_day")
    if policy.kind == "spend_money" and policy.usd_per_day is None:
        raise StandingError(400, "no_limit", "spend_money needs usd_per_day")
    if policy.kind == "run_command" and policy.pattern.strip("* ") == "":
        raise StandingError(400, "too_wide", "run_command needs a command "
                            "pattern, e.g. `gh pr*`")
    hit = standing_floor.policy_targets(policy.tool, policy.pattern)
    if hit is not None:
        raise StandingError(409, "never_coverable",
                            f"{hit.replace('_', ' ')} always goes to the "
                            "owner; no standing approval can cover it")
    return policy


def create(fields: dict, *, by: str, status: str = "active",
           path: Path = DEFAULT_PATH, now: float | None = None) -> Policy:
    """A new policy. `status` is `active` for the owner, `proposed` for a desk."""
    now = time.time() if now is None else now
    raw = dict(fields or {})
    kind = raw.get("kind")
    if kind not in KINDS:
        raise StandingError(400, "bad_kind",
                            f"kind must be one of {', '.join(KINDS)}")
    raw.setdefault("tool", DEFAULT_TOOL[kind])
    for key in ("id", "status", "created_by", "created_at", "approved_at",
                "revoked_at"):
        raw.pop(key, None)
    limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else raw
    for key, kind_ in (("count_per_day", int), ("usd_per_day", float)):
        _num(limits.get(key), kind_, key)  # raises the real reason
    _num(raw.get("expires_at"), float, "expires_at")
    policy = _from_raw({**raw, "id": "sa_" + secrets.token_hex(4),
                        "status": status, "created_by": by,
                        "created_at": now,
                        "approved_at": now if status == "active" else None})
    if policy is None:
        raise StandingError(400, "bad_policy", "not a standing approval")
    validate(policy)
    with _LOCK:
        _save(path, [*load(path), policy])
    return policy


def find(policy_id: str, path: Path = DEFAULT_PATH) -> Policy | None:
    return next((p for p in load(path) if p.id == policy_id), None)


def _change(policy_id: str, path: Path, fn) -> Policy:
    with _LOCK:
        rows = load(path)
        current = next((p for p in rows if p.id == policy_id), None)
        if current is None:
            raise StandingError(404, "unknown_policy",
                                f"no standing approval {policy_id!r}")
        updated = fn(current)
        _save(path, [updated if p.id == policy_id else p for p in rows])
    return updated


#: What an edit may change. Who made it, when, and its status may not.
EDITABLE = ("desk", "tool", "pattern", "count_per_day", "usd_per_day",
            "recipients", "account", "expires_at", "note")


def edit(policy_id: str, patch: dict, *, path: Path = DEFAULT_PATH) -> Policy:
    def apply(current: Policy) -> Policy:
        if current.status == "revoked":
            raise StandingError(409, "revoked", f"{policy_id} was revoked")
        merged = current.wire()
        limits = dict(merged.pop("limits"))
        for key, value in (patch or {}).items():
            if key == "limits" and isinstance(value, dict):
                limits.update({k: v for k, v in value.items()
                               if k in EDITABLE})
            elif key in EDITABLE:
                (limits if key in limits else merged)[key] = value
        out = _from_raw({**merged, "limits": limits})
        for key, kind_ in (("count_per_day", int), ("usd_per_day", float)):
            _num(limits.get(key), kind_, key)
        if out is None:
            raise StandingError(400, "bad_policy", "not a standing approval")
        return validate(out)
    return _change(policy_id, path, apply)


def approve(policy_id: str, *, path: Path = DEFAULT_PATH,
            now: float | None = None) -> Policy:
    """A proposed policy becomes active. The owner's call only."""
    def apply(current: Policy) -> Policy:
        if current.status != "proposed":
            raise StandingError(409, "not_proposed",
                                f"{policy_id} is {current.status}")
        return replace(validate(current), status="active",
                       approved_at=time.time() if now is None else now)
    return _change(policy_id, path, apply)


def revoke(policy_id: str, *, path: Path = DEFAULT_PATH,
           now: float | None = None) -> Policy:
    def apply(current: Policy) -> Policy:
        if current.status == "revoked":
            raise StandingError(409, "revoked", f"{policy_id} was revoked")
        return replace(current, status="revoked",
                       revoked_at=time.time() if now is None else now)
    return _change(policy_id, path, apply)
