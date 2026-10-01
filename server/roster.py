"""The roster: durable named desks, independent of any process.

A roster entry is a *desk*. A session is whoever is sitting at it right now.
Today a card vanishes from the board the moment its Terminal tab closes -- the
whole point of this module is that a desk survives that: it shows `OFFLINE`
instead of disappearing. `occupancy()` is the join that makes that true.

A flat list of desks is a staffing agency, not a company. `reports_to` is what
turns it into one: every desk names its boss, so work and escalations travel up
exactly one level. The observed org this copies is three deep -- a chief of
staff (the only desk the owner talks to), project managers under it, function
specialists under those -- and `boss_of` / `chain` / `tree` / `depth` /
`route_to` are how the rest of the deck reads that shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import BUS_DIR
from . import atomic

DEFAULT_PATH = BUS_DIR / "roster.json"


@dataclass(frozen=True)
class Desk:
    name: str
    cwd: str
    engine: str  # "claude" | "opencode" | "codex"
    mission: str
    model: str = ""
    created_at: float = 0.0
    # The function this desk performs: "growth", "sales", "studio", "product".
    label: str = ""
    # The charter: what this desk owns, in prose. This is the description a new
    # hire is briefed with -- see server/hire.brief().
    charter: str = ""
    # The name of this desk's boss. None means it reports to the owner, and
    # there should be exactly one such desk (the chief of staff).
    reports_to: str | None = None
    # Path to the desk's picture, which the desk generates for itself during
    # onboarding. Empty means the board draws its initials. Only ever written
    # through `server.onboard.apply_patch`, which is where the path is checked
    # for being a real image inside the avatar directory.
    avatar: str = ""
    # One line of character the desk's brief carries under "How you sound":
    # "dry, blunt, a bit of humour". Empty means the house voice alone.
    persona: str = ""
    # How the desk sounds on a live call: {"id": <AVSpeech identifier>,
    # "rate": float}. None means the app derives one from the name.
    voice: dict | None = None
    # The drawn character: {"shape": <AvatarShape>, "color": 0-11}. NOT
    # `avatar` -- that name was already the picture path above, and renaming
    # it would break every roster.json on disk. The API may call this
    # `avatar` on the wire; on the roster it is `avatar_look`.
    avatar_look: dict | None = None
    # A throwaway desk the deck's acceptance probes run against. On such a
    # desk -- and only there -- a message the engineer posts is framed as a
    # task (`office.ENGINEER_TEST_MARK`) instead of "not a task", so a probe
    # measures what the desk DOES. Never set on a real desk; set only at hire.
    test: bool = False


def _as_dict(value: object) -> dict | None:
    """A JSON object stays one; anything else is dropped. One malformed field
    must not cost a desk its place on the board."""
    return dict(value) if isinstance(value, dict) else None


def load_roster(path: Path) -> list[Desk]:
    """Every desk on the roster. Missing or unreadable file -> []."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    rows = raw.get("agents")
    if not isinstance(rows, list):
        return []
    desks: list[Desk] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            desks.append(
                Desk(
                    name=str(row["name"]),
                    cwd=str(row["cwd"]),
                    engine=str(row["engine"]),
                    mission=str(row["mission"]),
                    model=str(row.get("model") or ""),
                    created_at=float(row.get("created_at") or 0.0),
                    label=str(row.get("label") or ""),
                    charter=str(row.get("charter") or ""),
                    reports_to=(
                        str(row["reports_to"])
                        if row.get("reports_to") is not None
                        else None
                    ),
                    avatar=str(row.get("avatar") or ""),
                    persona=str(row.get("persona") or ""),
                    voice=_as_dict(row.get("voice")),
                    avatar_look=_as_dict(row.get("avatar_look")),
                    test=row.get("test") is True,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return desks


def save_roster(path: Path, desks: list[Desk]) -> None:
    """Atomic write: no reader ever sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "agents": [asdict(d) for d in desks]}
    atomic.write_text(path, json.dumps(payload))


def upsert(path: Path, desk: Desk) -> None:
    """Add `desk`, or replace the existing entry with the same name."""
    desks = [d for d in load_roster(path) if d.name != desk.name]
    desks.append(desk)
    save_roster(path, desks)


def remove(path: Path, name: str) -> None:
    """Drop the desk named `name`. Safe to call when it was never there."""
    desks = [d for d in load_roster(path) if d.name != name]
    save_roster(path, desks)


def occupancy(desks: list[Desk], sessions: list[dict]) -> list[dict]:
    """Join desks with live sessions by name.

    Desks come first, in roster order, each `{**desk_fields, "desk": True,
    "state": ..., "session_id": ..., "pid": ...}` -- the live session's state
    when a session's `name` equals the desk's `name`, else the literal
    `"OFFLINE"` with `session_id=None, pid=None`. Live sessions with no desk
    are appended unchanged, tagged `"desk": False`.
    """
    by_name: dict[str, dict] = {}
    for session in sessions:
        name = session.get("name")
        if name is None:
            continue
        # A live card outranks a lingering DEAD one whatever the order: right
        # after a wake the old card can outlive the new one for seconds.
        held = by_name.get(name)
        if held is None or held.get("state") == "DEAD" or session.get("state") != "DEAD":
            by_name[name] = session

    rows: list[dict] = []
    seated: set[str] = set()
    for desk in desks:
        session = by_name.get(desk.name)
        row = {**asdict(desk), "desk": True}
        if session is None:
            row["state"] = "OFFLINE"
            row["session_id"] = None
            row["pid"] = None
        else:
            row["state"] = session.get("state")
            row["session_id"] = session.get("session_id")
            row["pid"] = session.get("pid")
            seated.add(desk.name)
        rows.append(row)

    for session in sessions:
        if session.get("name") in seated:
            continue
        rows.append({**session, "desk": False})

    return rows


def live_desks(desks: list[Desk], sessions: list[dict]) -> int:
    """How many desks currently have someone sitting at them.

    NOT `len(sessions)`. This Mac runs many Claude Code sessions that are not
    anyone's desk -- subagent worktrees, the manager's own terminal, an
    unrelated project window -- and a cap on the *org* must not refuse a hire
    because of a session that has nothing to do with it. A `DEAD` card lingers
    on the board briefly for the fade-out (`Collector.DEAD_GRACE_SECONDS`) and
    is not actually running, so it does not count either.
    """
    return sum(
        1
        for row in occupancy(desks, sessions)
        if row["desk"] and row["session_id"] is not None and row["state"] != "DEAD"
    )


# ── the org chart ──────────────────────────────────────────────────────────
#
# Five small pure functions over a list of desks. They share one hard rule: a
# malformed org must FAIL, never hang and never quietly shrink. Two shapes go
# wrong in practice --
#
#   a cycle   (a reports to b, b reports to a; or a desk reporting to itself)
#             -> ValueError. A walk up the chain would otherwise never end.
#   an orphan (reports_to names a desk that is not on the roster, usually a
#             typo) -> surfaced, not swallowed: `chain` ends on the dangling
#             name, `tree` puts the desk at the top flagged `"orphan": True`.
#             The quiet version of this bug makes a desk vanish off the board.


def _by_name(desks: list[Desk]) -> dict[str, Desk]:
    return {d.name: d for d in desks}


def chief(desks: list[Desk]) -> str | None:
    """The chief of staff (Atlas): the one desk that speaks for the deck.

    The only root, if there is one. Otherwise the only root anyone reports
    to -- MEASURED on the box 2026-09-30, `new-hire-82d9ab` and `wake-probe`
    have no boss either, and neither runs anything. None when that is still
    ambiguous: two desks cannot both be the single voice that may page him.
    """
    roots = [d.name for d in desks if d.reports_to is None]
    if len(roots) == 1:
        return roots[0]
    bosses = {d.reports_to for d in desks if d.reports_to is not None}
    staffed = [name for name in roots if name in bosses]
    return staffed[0] if len(staffed) == 1 else None


def boss_of(desks: list[Desk], name: str) -> str | None:
    """The name of `name`'s boss. `None` for a root (its boss is the owner),
    and `None` for a desk that is not on the roster at all."""
    desk = _by_name(desks).get(name)
    return desk.reports_to if desk is not None else None


def chain(desks: list[Desk], name: str) -> list[str]:
    """`[name, boss, boss's boss, ...]` up to the root.

    Raises ValueError if `name` is not a desk, or if the walk meets a name it
    has already seen -- that is a reporting cycle, and the message names it.
    A boss that is not itself a desk ends the chain as its last element, so an
    orphan reads as an orphan instead of impersonating a root.
    """
    by_name = _by_name(desks)
    if name not in by_name:
        raise ValueError(f"no such desk: {name!r}")

    links = [name]
    seen = {name}
    current = name
    while True:
        boss = by_name[current].reports_to
        if boss is None:
            return links
        if boss in seen:
            raise ValueError("reporting cycle: " + " -> ".join(links + [boss]))
        links.append(boss)
        seen.add(boss)
        if boss not in by_name:
            return links
        current = boss


def depth(desks: list[Desk], name: str) -> int:
    """How many levels down from the top this desk sits. 0 for a root."""
    return len(chain(desks, name)) - 1


def tree(desks: list[Desk]) -> list[dict]:
    """The org as nested `{**desk_fields, "orphan": bool, "reports": [...]}`.

    Roots first, in roster order, and every desk appears exactly once. A desk
    whose named boss is missing is treated as a root and flagged `orphan`.
    The whole graph is walked for cycles *before* anything is built: a desk
    inside a cycle is never a root, so without that check it would be dropped
    from the output without a word.
    """
    by_name = _by_name(desks)
    for desk in desks:
        chain(desks, desk.name)  # raises on a cycle anywhere in the org

    roots: list[Desk] = []
    children: dict[str, list[Desk]] = {}
    for desk in desks:
        if desk.reports_to is None or desk.reports_to not in by_name:
            roots.append(desk)
        else:
            children.setdefault(desk.reports_to, []).append(desk)

    def node(desk: Desk) -> dict:
        return {
            **asdict(desk),
            "orphan": desk.reports_to is not None and desk.reports_to not in by_name,
            "reports": [node(child) for child in children.get(desk.name, [])],
        }

    return [node(desk) for desk in roots]


def route_to(desks: list[Desk], sender: str) -> str | None:
    """Who this desk reports its work to: one level up, never straight to the
    top. `None` means the owner -- only the root desk gets that. Raises
    ValueError if the sender sits in a reporting cycle."""
    if sender not in _by_name(desks):
        return None
    links = chain(desks, sender)
    return links[1] if len(links) > 1 else None
