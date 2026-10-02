"""Preflight for the always-on box: judge measurements, refuse a silent bad deploy.

Everything Agent Deck does today lives on the owner's Mac. Shut the lid and every
routine stops. The fix is a box that stays up -- and the risk of that fix is a
deploy that *looks* fine. A daemon that never started is obvious within a
minute; a daemon that started without a token, or onto a port other_app owns, or
onto a filesystem it cannot write, is a green systemd unit that answers nothing
useful. That is the failure this module exists to make impossible.

`preflight(facts) -> list[str]` is a pure judge. It opens no sockets, runs no
subprocesses and reads no files: somebody else measures the box (the installer,
or the owner by hand from the runbook) and hands the numbers here. That is what makes
it testable on a laptop that is forbidden to touch the box at all.

Two rules shape every check:

* **An unmeasured fact is a blocker.** A missing key never reads as a pass --
  "no error" is exactly what "nothing ran" looks like.
* **The message carries the consequence, not just the symptom.** "no token" is
  a nag someone overrides at 1am; "every /v1 route answers 503" is a reason.

Empty list means go.
"""

from __future__ import annotations

# ── what the box has to clear ────────────────────────────────────────────────

#: The deck's own code is 3.11+ (`X | None` annotations, `tomllib`). The box
#: reports python3.12, so this is headroom rather than a stretch.
MIN_PYTHON = (3, 11)

#: Ports `deck.toml` reserves (`deck.reserved_ports`) arrive as the
#: `reserved_ports` fact. Reserved, not merely occupied: a scan taken while the
#: other service restarts shows the port free, and installing there would work
#: once and break that service permanently on its next start. Nothing is
#: reserved by default -- a stranger's box has no neighbour to protect.
#: The default deck port.
DEFAULT_DECK_PORT = 7789

#: Transcripts, the event log and the office log all grow on disk. Below this
#: the first thing to fail is the append to `events.jsonl`, which shows up as a
#: board that quietly stops updating.
MIN_FREE_DISK_MB = 1024


def _version_tuple(value) -> tuple[int, ...] | None:
    """`"3.12.3"` or `(3, 12, 3)` -> `(3, 12, 3)`. Anything else -> None."""
    if isinstance(value, (tuple, list)):
        parts = list(value)
    elif isinstance(value, str):
        parts = value.strip().split(".")
    else:
        return None
    out: list[int] = []
    for part in parts:
        try:
            out.append(int(str(part).strip()))
        except (TypeError, ValueError):
            break
    return tuple(out) or None


def _shown(value) -> str:
    """How a version prints in a blocker: `3.9.18` for either input shape."""
    if isinstance(value, (tuple, list)):
        return ".".join(str(p) for p in value)
    return str(value)


def preflight(facts: dict) -> list[str]:
    """Return the human-readable reasons this box is not ready. Empty = go.

    `facts` is never mutated; the caller may re-measure and re-judge the same
    dict. See the module docstring for the fact contract.
    """
    facts = dict(facts or {})
    blockers: list[str] = []

    # ── the interpreter ──────────────────────────────────────────────────────
    if "python_version" not in facts:
        blockers.append(
            "python version is unknown: nothing measured the interpreter on the box"
        )
    else:
        version = _version_tuple(facts["python_version"])
        if version is None:
            blockers.append(
                f"python version {facts['python_version']!r} is unreadable: "
                "expected something like 3.12.3"
            )
        elif version < MIN_PYTHON:
            wanted = ".".join(str(p) for p in MIN_PYTHON)
            blockers.append(
                f"python {_shown(facts['python_version'])} is older than the "
                f"{wanted} the deck needs; it will not import at all"
            )

    # ── node ─────────────────────────────────────────────────────────────────
    # Not optional and not only a build tool: all four session hooks
    # (cc-bus, cc-approve, cc-office, cc-compact) are node, and so is the
    # WhatsApp bridge the routines deliver through.
    if "node_present" not in facts:
        blockers.append("node presence is unknown: nothing measured it on the box")
    elif not facts["node_present"]:
        blockers.append(
            "node is missing: every session hook and the WhatsApp bridge run "
            "under node, so the board would stay empty and no routine would land"
        )

    # ── claude ───────────────────────────────────────────────────────────────
    if "claude_present" not in facts:
        blockers.append("claude presence is unknown: nothing measured it on the box")
    elif not facts["claude_present"]:
        blockers.append(
            "claude is missing: the deck spawns desks with `claude --bg` and "
            "has nothing to spawn without it"
        )

    if "claude_authenticated" not in facts:
        blockers.append(
            "whether claude is authenticated is unknown: nothing measured it"
        )
    elif not facts["claude_authenticated"]:
        blockers.append(
            "claude is installed but not authenticated: every desk the deck "
            "spawns would die at the login prompt, with the board showing it as WORKING"
        )

    # ── the port ─────────────────────────────────────────────────────────────
    blockers.extend(_port_blockers(facts))

    # ── the bus directory ────────────────────────────────────────────────────
    if "bus_dir_writable" not in facts:
        blockers.append(
            "bus directory writability is unknown: nothing tried to write there"
        )
    elif not facts["bus_dir_writable"]:
        where = facts.get("bus_dir", "the agent-bus directory")
        blockers.append(
            f"the bus directory {where} is not writable by the service user: "
            "no session event is ever recorded, so the board stays blank"
        )

    # ── the token ────────────────────────────────────────────────────────────
    if "token_configured" not in facts or not facts["token_configured"]:
        blockers.append(
            "AGENT_DECK_TOKEN is not configured: with no token every /v1 route "
            "answers 503, so the box would look healthy and answer nothing"
        )

    # ── disk ─────────────────────────────────────────────────────────────────
    if "disk_free_mb" not in facts:
        blockers.append("disk headroom is unknown: nothing measured free space")
    else:
        try:
            free = int(facts["disk_free_mb"])
        except (TypeError, ValueError):
            blockers.append(
                f"disk headroom {facts['disk_free_mb']!r} is unreadable: "
                "expected a number of megabytes"
            )
        else:
            if free < MIN_FREE_DISK_MB:
                blockers.append(
                    f"disk headroom is {free} MB, under the {MIN_FREE_DISK_MB} MB "
                    "the event log and transcripts need; appends fail silently first"
                )

    return blockers


def _reserved_ports(facts: dict) -> set[int]:
    """The ports config keeps the deck off. Unreadable entries are skipped."""
    out: set[int] = set()
    for value in facts.get("reserved_ports") or ():
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _port_blockers(facts: dict) -> list[str]:
    """Whether the deck can have the port it intends to bind.

    Split out because the port is the one fact judged twice -- once against a
    reservation that holds even when the scan says free, and once against what
    is actually listening.
    """
    out: list[str] = []

    if "deck_port" not in facts:
        out.append(
            "the deck port is unknown: nothing said which port the unit would bind"
        )
        port = None
    else:
        try:
            port = int(facts["deck_port"])
        except (TypeError, ValueError):
            out.append(f"the deck port {facts['deck_port']!r} is not a port number")
            port = None

    reserved = _reserved_ports(facts)
    if port in reserved:
        out.append(
            f"port {port} is reserved (deck.reserved_ports) for another service "
            f"on this box; run the deck on another port such as {DEFAULT_DECK_PORT} "
            "instead of taking it"
        )

    if "ports_in_use" not in facts:
        out.append("which ports are in use is unknown: nothing scanned for listeners")
        return out

    in_use = facts["ports_in_use"] or {}
    if port is None or port in reserved:
        # Already refused above; naming the same conflict twice helps nobody.
        return out

    holder = None
    for key, value in dict(in_use).items():
        try:
            if int(key) == port:
                holder = value
                break
        except (TypeError, ValueError):
            continue

    if holder is None:
        return out
    if _is_our_own(holder):
        # A re-run: the deck already owns its port. That is the success state,
        # not a conflict, or the installer could never be run twice.
        return out
    out.append(
        f"port {port} is already in use by {holder}: the deck will not fight it "
        "for the port -- stop that service or pick another port"
    )
    return out


def _is_our_own(holder) -> bool:
    """Is the listener on the deck's port the deck itself?"""
    return "agentdeck" in str(holder).lower()
