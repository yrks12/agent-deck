"""The rules every desk runs on -- the CURRENT ones, not the ones it was born with.

A desk's brief is its `--append-system-prompt`, and the CLI freezes it in the
job's `respawnFlags`: a respawn or a wake brings back the SAME brief
(server/spawn.py `stop_job`, measured). So a rule changed in the code reached
no running desk -- MEASURED 2026-09-30: after "you never type a password" was
replaced, a growth desk still quoted the old line to the owner.

So the deck publishes the rule sections of the brief with a version, records
which version each desk started on, and `hooks/cc-office.js` hands a desk the
lines that changed -- marking the old ones superseded -- the first time it
starts, resumes, compacts or takes a prompt after a change. Only on a change.

Files, under `~/.claude/agent-bus/rules/`:
    current.json         {"version", "lines", "superseded"}
    seen/<desk>.json     {"version", "lines"} -- what that desk was last given
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import atomic, hire
from .paths import BUS_DIR

#: Lines that were once in a desk's brief and are now WRONG. A desk started
#: before versions existed has no `seen` record, so the deck cannot diff it;
#: these are named to it as superseded explicitly.
SUPERSEDED = (
    "At a login, a 2FA code, a captcha or any payment, stop: the tools refuse "
    "to type there, and you never type a password or a card number. Tell "
    "whoever you report to, in one line, which site and why, and that it is "
    "waiting on your screen -- the owner will take over and sign in himself, "
    "and that sign-in is then yours in the same browser.",
    "At a login, 2FA code, captcha or payment page, raise Take over the "
    "screen, then carry on when he says it is done.",
)


def sections() -> list[str]:
    """The desk-independent rule sections of `hire.brief`, in its order."""
    return [hire.CHANNEL, hire.HIRING, hire.SCHEDULES, hire.COMPUTER,
            hire.NEVER_BLOCKED, hire.HOW_YOU_SOUND, hire.HOW_YOU_DECIDE]


def lines() -> list[str]:
    return [line for s in sections() for line in s.split("\n") if line.strip()]


def version() -> str:
    return hashlib.sha256("\n".join(lines()).encode()).hexdigest()[:12]


#: Where the rules live. A module constant so a test can point it elsewhere.
RULES_DIR: Path = BUS_DIR / "rules"


def _dir(bus: Path | None) -> Path:
    return Path(bus) / "rules" if bus else RULES_DIR


def publish(bus: Path | None = None) -> str:
    """Write the current rules where the office hook reads them. Never raises."""
    try:
        path = _dir(bus) / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(path, json.dumps({
            "version": version(), "lines": lines(),
            "superseded": list(SUPERSEDED)}))
    except OSError:
        pass
    return version()


def mark_seen(desk: str, bus: Path | None = None) -> None:
    """`desk` was just started with the current brief. Never raises."""
    name = str(desk or "")
    if not name or "/" in name or name.startswith("."):
        return
    try:
        path = _dir(bus) / "seen" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(path, json.dumps({"version": version(),
                                            "lines": lines()}))
    except OSError:
        pass
