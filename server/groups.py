"""Groups: several agents in one conversation, about one project.

A group is a *named set of desk names* with a thread of its own. It is not a
new kind of desk, it does not appear in the org chart, and it grants nothing:
`reports_to` is still the only edge that means anything, and a member of a
group has exactly the authority its desk already had.

**A group message is a fanout, not a new delivery mechanism.** `broadcast` puts
one copy on the office queue per member through `office.send` -- the same queue
the owner's direct messages use, read by the same office hook on the member's
next turn. That is the whole point: a member with nobody at its desk gets a
group message queued exactly as it would get a direct one. A second delivery
path would mean a second, subtly different set of rules for when a message has
actually arrived, and the first time they disagreed nobody would be able to say
which one was right.

The copies carry two extra fields -- `group` (the group's name) and `group_id`
(one id shared by every copy of one message) -- plus a shared `ts`. That is
what lets `server/api.py` show the group's thread as the one message the owner
sent, rather than as N near-identical ones.

Stored beside the roster, in the same directory and by the same trick
`hire.events_path` uses, so production lands in `~/.claude/agent-bus/` while a
test with a tmp roster gets a tmp groups file.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import atomic, office

GROUPS_FILENAME = "groups.json"

#: A "group" past this size is a broadcast, and a broadcast that queues one
#: copy per member is how the office log becomes the bottleneck.
MAX_MEMBERS = 16


class GroupError(Exception):
    """Refusal. `reason` is a stable machine-readable slug, the same shape as
    `hire.HireError` and `onboard.OnboardError`, so every caller on the deck
    reads a refusal the same way."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class Group:
    name: str
    members: tuple[str, ...]
    created_at: float = 0.0


def groups_path(roster_path: Path | str) -> Path:
    """Where groups live, derived from the roster."""
    return Path(roster_path).parent / GROUPS_FILENAME


def thread_id(name: str) -> str:
    """The group's thread. Third kind alongside `direct:` and `peer:`."""
    return f"group:{name}"


# ── at rest ────────────────────────────────────────────────────────────────


def load_groups(path: Path | str) -> list[Group]:
    """Every group. Missing or unreadable file -> [].

    Same rule as `roster.load_roster`: a corrupt file is an empty list and a
    board that still paints, never an exception thrown at a request.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Group] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        members = row.get("members")
        if not isinstance(name, str) or not name or not isinstance(members, list):
            continue
        clean = tuple(m for m in members if isinstance(m, str) and m)
        if not clean:
            continue
        try:
            created = float(row.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        out.append(Group(name=name, members=clean, created_at=created))
    return out


def save_groups(path: Path | str, groups: list[Group]) -> None:
    """Atomic write: no reader ever sees a partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(target, json.dumps({
        "version": 1,
        "groups": [{"name": g.name, "members": list(g.members),
                    "created_at": g.created_at} for g in groups],
    }))


def find(groups: list[Group], name: str) -> Group | None:
    return next((g for g in groups if g.name == name), None)


# ── changing the set ───────────────────────────────────────────────────────


def create(path: Path | str, *, name: str, members: list[str],
           known: set[str]) -> Group:
    """Create a group and return it. Raises GroupError:

      missing_field      no name
      no_members         nobody in it
      unknown_member     a name that is not a desk -- a typo in a member list
                         is a member that silently never gets anything
      name_taken         a group by that name already exists
      too_many_members   past MAX_MEMBERS

    Duplicates in `members` are collapsed, in the order first given: a member
    listed twice must not be sent the same message twice.
    """
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise GroupError("missing_field", "a group needs a name")

    ordered: list[str] = []
    for member in members or []:
        if not isinstance(member, str) or not member.strip():
            continue
        member = member.strip()
        if member not in ordered:
            ordered.append(member)
    if not ordered:
        raise GroupError("no_members", "a group with nobody in it sends nothing")
    if len(ordered) > MAX_MEMBERS:
        raise GroupError("too_many_members",
                         f"{len(ordered)} members; the cap is {MAX_MEMBERS}")

    missing = [m for m in ordered if m not in known]
    if missing:
        raise GroupError("unknown_member",
                         f"not a desk: {', '.join(sorted(missing))}")

    groups = load_groups(path)
    if find(groups, clean_name) is not None:
        raise GroupError("name_taken", f"a group named {clean_name!r} already exists")

    group = Group(name=clean_name, members=tuple(ordered), created_at=time.time())
    save_groups(path, groups + [group])
    return group


def remove(path: Path | str, name: str) -> None:
    """Drop a group. Raises `unknown_group` if it was never there -- deleting
    something that does not exist is a client working from a stale list, and
    telling it so is more useful than a silent success."""
    groups = load_groups(path)
    if find(groups, name) is None:
        raise GroupError("unknown_group", f"no group named {name!r}")
    save_groups(path, [g for g in groups if g.name != name])


# ── sending to one ─────────────────────────────────────────────────────────


def resolved_members(group: Group, resolve=None) -> list[str]:
    """The names this group's members go by NOW, in order, without duplicates.

    `resolve` is `onboard.resolve` bound to the alias map, injected the same way
    `deliver` is: this module knows that a name can go stale, not where the
    renames are stored.

    Deduped AFTER resolution, never before. Two entries that now point at the
    same desk are one recipient -- a member reading the same instruction twice
    is the exactly-once failure this module's contract rules out.
    """
    out: list[str] = []
    for member in group.members:
        name = resolve(member) if resolve is not None else member
        if name and name not in out:
            out.append(name)
    return out


def broadcast(group: Group, text: str, *, sender: str = office.OWNER_HANDLE,
              deliver=None, resolve=None, quote: dict | None = None) -> list[dict]:
    """Queue `text` for every member, exactly once each.

    Returns one row per member: `{"to", "ok", "id", "group_id", "delivered"}`.
    `deliver` is the daemon's inject-now callable; when it says the bytes went
    out the queued record is acked, exactly as a direct send does. When it says
    no -- or when there is nobody at the desk -- the record stays queued for
    the member's next turn, which is the case this whole module exists for.

    **Members are resolved at SEND time, not at write time.** The stored group
    keeps the names it was created with; a member that renames itself
    afterwards would otherwise be sent to under a name nobody occupies, and it
    fails the quiet way -- `office.send` queues that record happily, so there
    is no error and no log line, just a member that stopped hearing from the
    group. Resolving here means a rename that happens long after the group was
    written still lands.

    One `ts` and one `group_id` across every copy, so a reader can fold the
    fanout back into the single message the owner actually sent.

    `quote` is the message he is answering (`{id, author, excerpt, text}`).
    Each member gets its OWN `reply_line` -- the member that wrote the quoted
    message reads "your message", the others read whose it was.
    """
    stamp = time.time()
    shared = uuid.uuid4().hex[:12]
    rows: list[dict] = []

    for member in resolved_members(group, resolve):
        extra = {"ts": stamp, "group": group.name, "group_id": shared}
        line = ""
        if quote:
            line = office.reply_line(quote["author"], quote["text"], to=member)
            extra["reply_to"] = {k: quote[k] for k in ("id", "author", "excerpt")}
            extra["reply_line"] = line
        result = office.send(member, text, sender, extra=extra)
        row = {"to": member, "ok": bool(result.get("ok")),
               "id": result.get("id"), "group_id": shared, "delivered": False}
        if row["ok"] and deliver is not None:
            try:
                # `deliver` is the socket fast path, and it skips
                # `hooks/cc-office.js` -- the one thing that otherwise says who
                # sent this. Unmarked, a member whose desk happened to be awake
                # is the only one that cannot tell the owner from a peer, which
                # is the split that had a desk refusing him. Same `sender` the
                # record above carries, so the two halves of one message never
                # disagree about who wrote it.
                row["delivered"] = bool(
                    deliver(member, office.attribute(text, sender, reply=line)))
            except Exception:
                row["delivered"] = False
            if row["delivered"]:
                office.ack(result["id"])
        rows.append(row)

    return rows
