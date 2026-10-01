"""The harvest loop: the deck reads back what a session actually said.

`server.onboard` can read a `YOS_DESK` line (an agent naming itself) and a
`YOS_HIRE` line (an agent asking for a colleague), and can apply both. Until
this module nothing *called* it, because nothing read a session's output --
so the conversation only ever ran one way. An agent could be told who it was
and could never tell the deck anything back, which is exactly why an agent
could not hire another agent. This is the return path.

One tail per session, over the same transcript the collector already reads,
using the byte-offset pattern of `sources.bus.BusReader` and
`sources.transcript.TranscriptTail`: read only the delta, stop at the last
complete newline, and treat a changed inode or a shrunken file as a fresh one.

Three things make this safe to run against an LLM's stdout on a loop.

**The offset is on disk.** This daemon restarts constantly. An in-memory
offset means every restart re-reads every transcript from byte 0 and re-applies
every historical `YOS_HIRE` -- silently re-hiring desks the owner has since
fired. The offsets file is the difference between a reader and a time machine.

**The partial is buffered by the offset, not by the process.** A poll that
lands mid-record leaves the offset *before* the half-written line, so those
bytes are re-read next tick. An in-memory buffer would do the same until the
restart that drops it and eats the record.

**The actor is the session's own desk name, never the name on the line.**
`onboard.apply_patch` refuses a patch naming another desk -- but only if it is
handed the truth about who spoke. Passing along whatever the line claimed would
disarm that guard while looking exactly like a working harvester.

A session with no desk has no actor, so nothing it says is applied. Its bytes
are still consumed: banking them would mean the day it is given a desk, every
hire it asked for last week fires at once.

Nothing here spawns. `poll` mutates the roster and returns a record per line it
acted on -- including refusals, with the refusing layer's own reason slug.

Every one of those records is also appended to the bus ledger the hooks write,
`events.jsonl` -- one log, not two. **The refusals are the reason.** An agent
trying to rewrite its boss's charter, or to hire past the depth cap, is exactly
the event nobody is watching for; `onboard`'s docstring has always promised
"refused and logged" and until now it only returned. A hire that *succeeds* is
logged by `hire.hire()` itself, so it is not logged again here: one event per
thing that happened.

**A desk's ordinary prose is carried too, and that is the other half of the
return path.** A hired agent asks its opening question the way it was told to
-- "in plain prose, written out in this conversation" -- and until now nobody
read it: `Surface` builds a thread out of the office queue and the SendMessage
comms edges, and plain assistant text is in neither. Measured on a live hire,
the owner's board showed a desk that had named itself, an empty chat, and no
unread badge, so the product's opening move was invisible on the only surface
he reads. The tail that was already here is the one thing on the machine that
knows which desk said what, and remembers on disk how far it has read -- which
is exactly what stops a message polled every second from posting every second.

What qualifies is **turn-final prose**: a text block on an assistant record
whose `stop_reason` is `end_turn`. That is the model stopping and handing back
to a human. Narration emitted alongside a tool call ("I'll run the drift scan
myself") sits on a record stamped `tool_use`, thinking blocks are not text at
all, and a subagent's turns are `isSidechain`. Measured on the real
`drift-watch` transcript: 2 qualifying records in 87KB. Anything with no
`stop_reason` at all -- another engine, an older transcript -- says nothing,
because a chat nobody can bear to read is the same failure as an empty one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import office, onboard, paths
from .hire import HireError, events_path
from . import roster
from .roster import load_roster

# Keep this many line digests per session, so a transcript replaying an old
# message cannot re-apply it. Bounded because the file is rewritten every poll
# and a session's history is otherwise unbounded; only the recent past can
# realistically be replayed.
SEEN_LIMIT = 256

# Same guard as onboard.MAX_LINE, applied a layer earlier: a transcript record
# carrying a megabyte of tool output is not a self-description, and hashing it
# every poll costs real time for a result we would throw away.
MAX_RECORD = 256 * 1024

# Every marker onboard.parse_lines understands. Read from onboard rather than
# repeated here, so a third marker added there is covered here for free.
_MARKERS = (onboard.DESK_PREFIX, onboard.HIRE_PREFIX)

# A desk scheduling, listing or deleting its own routine. Applied here rather
# than in `onboard` because it writes the deck's routines file through
# `api.Surface` -- the same methods behind the app's Routines panel -- not the
# roster. It exists because the only scheduler a desk knew was Claude Code's
# `CronCreate`, which is session-only: measured on the box, atlas's daily
# report lived in one session, would expire in 7 days, and `GET /v1/routines`
# was empty while the owner looked for it.
ROUTINE_PREFIX = "YOS_ROUTINE"

# How much of the parser's own error reaches the ledger. The line that failed
# is an LLM's untrusted stdout and can be arbitrarily long; past this many
# characters the reason is cut, not the record refused.
MAX_REASON = 200

# The one `stop_reason` that means the model finished and handed back to a
# human. Every other value -- `tool_use` above all -- is the session still
# working, and its text is narration about that work rather than a message.
END_TURN = "end_turn"

# Who a desk's prose is addressed to. A ROLE, not the owner's name: `api`
# routes any record whose `to` is in `OWNER_SENDERS` into that sender's own
# direct thread, so the harvester never has to know what the owner is called.
#
# Taken from `office` rather than spelled again. It is no longer this module's
# alone: `spawn.announce_restart` posts the restart line to the same inbox so
# it lands in the same thread as the desk's prose, and two copies of the string
# that decides WHICH THREAD would put the two halves of one conversation in
# different places the first time either was reworded.
OWNER_INBOX = office.OWNER_INBOX

# Who a receipt is from. The deck itself, not the owner and not the new hire:
# a desk reading "you now have a report" must not think the owner typed it, and
# must not think the report introduced itself.
DECK_SENDER = "deck"


# What each marker cannot be written without. `YOS_HIRE` needs all four
# because `onboard.apply_hire` hands `cwd` straight to `hire.hire()`, which
# checks it is a real directory -- and nothing on the agent-to-agent path
# allocates one. `api.Surface._workspace` exists only behind the HTTP
# interview door, so on this path the field is still load-bearing.
_REQUIRED: dict[str, tuple[str, ...]] = {
    onboard.HIRE_PREFIX: ("name", "label", "charter", "cwd"),
    onboard.DESK_PREFIX: (),  # any one of its optional fields is enough
}


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:16]


def _missing_fields(marker: str, payload: object) -> str:
    """Why well-formed JSON was still dropped, in words the agent can act on.

    "well-formed but empty" told nobody what to fix. A hire lost for want of
    `cwd` and a hire lost for want of a charter read identically, so neither
    the agent that emitted the line nor the owner reading the board could tell
    which -- which is most of why the record was worth nothing.
    """
    if not isinstance(payload, dict):
        return "not a JSON object"
    missing = [f for f in _REQUIRED.get(marker, ())
               if not str(payload.get(f) or "").strip()]
    if missing:
        return "missing required field(s): " + ", ".join(missing)
    return "well-formed but empty"


class Spoken(NamedTuple):
    """One thing a session said, and whether it was said *to somebody*.

    `to_owner` separates the two jobs this tail does. Every block is read for
    markers, because a `YOS_DESK` line is just as real mid-turn. Only a
    turn-final one is a message.
    """

    text: str
    to_owner: bool
    # Identity of the transcript entry this came from (`uuid#block`). Empty
    # when the source carries none, and dedupe then falls back to the text.
    key: str = ""


def is_marker_line(raw: str) -> bool:
    """True for a line that opens a marker `onboard.parse_lines` would read.

    The prefix must be the whole token and start the line, matching
    `onboard._payload` exactly -- `YOS_DESKTOP {...}` is somebody else's, and
    the prefix quoted inside a sentence is prose.
    """
    stripped = raw.strip()
    return any(_opens(stripped, marker)
               for marker in _MARKERS + (ROUTINE_PREFIX,))


def _opens(stripped: str, marker: str) -> bool:
    return (stripped.startswith(marker)
            and (len(stripped) == len(marker) or stripped[len(marker)].isspace()))


def chat_text(text: str) -> str:
    """The part of a turn-final block the owner should actually read.

    Marker lines come out. A desk naming itself is the roster's business and
    already shows up as the desk's own name and chip on the board; the raw
    JSON in the chat is noise, and a turn that was *only* a marker is not a
    message at all -- it returns "" and nothing is posted.
    """
    kept = [line for line in (text or "").splitlines() if not is_marker_line(line)]
    return "\n".join(kept).strip()


def _spoken(record: dict) -> list[Spoken]:
    """What the session itself said in this transcript record.

    Only the assistant's own text blocks. A user turn quoting `YOS_HIRE` is
    someone else's words -- the owner's, or another session's message -- and
    reading those would let anyone who can type into a session hire from it.

    `stop_reason` is stamped on *every* record of one assistant message, not
    just the last: Claude Code splits one message's content blocks across
    several records, so a text block that precedes a tool call in the same
    message is correctly stamped `tool_use`. That is what makes this a usable
    signal rather than a guess at which record ended the turn.
    """
    if record.get("type") != "assistant":
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    # A subagent's turns live in the parent's transcript flagged as a
    # sidechain. They are work, not the desk talking to its owner.
    to_owner = (message.get("stop_reason") == END_TURN
                and not record.get("isSidechain"))
    uuid = record.get("uuid")
    uuid = uuid if isinstance(uuid, str) and uuid else ""
    return [
        Spoken(block["text"], to_owner, f"{uuid}#{i}" if uuid else "")
        for i, block in enumerate(content)
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


class _Tail:
    """One session's place in its own transcript. Serialisable, on purpose."""

    def __init__(self, offset: int = 0, inode: int | None = None,
                 seen: list[str] | None = None) -> None:
        self.offset = offset
        self.inode = inode
        self.seen: list[str] = list(seen or [])

    def as_dict(self) -> dict:
        return {"offset": self.offset, "inode": self.inode, "seen": self.seen}

    def remember(self, digest: str) -> None:
        self.seen.append(digest)
        if len(self.seen) > SEEN_LIMIT:
            del self.seen[: len(self.seen) - SEEN_LIMIT]

    def read(self, path: Path) -> list[Spoken]:
        """Every line the session has said since the last read.

        Advances the offset only past the last complete newline: a half-written
        record stays unread until the tick that completes it.
        """
        try:
            stat = path.stat()
        except OSError:
            return []

        # Rotation, truncation, or a resumed session writing a fresh file. The
        # digests go with it: they described bytes that no longer exist.
        if self.inode is not None and (stat.st_ino != self.inode or stat.st_size < self.offset):
            self.offset = 0
            self.seen.clear()
        self.inode = stat.st_ino

        if stat.st_size <= self.offset:
            return []

        try:
            with path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(stat.st_size - self.offset)
        except OSError:
            return []

        cut = chunk.rfind(b"\n")
        if cut == -1:
            return []
        self.offset += cut + 1

        spoken: list[Spoken] = []
        for raw in chunk[:cut].split(b"\n"):
            if not raw.strip() or len(raw) > MAX_RECORD:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                # Not a transcript record. Anything else a session's output
                # channel carries is read as the plain line it looks like --
                # for markers only. A line with no turn around it carries no
                # evidence that anybody was being spoken to.
                try:
                    spoken.append(Spoken(raw.decode("utf-8"), False))
                except UnicodeDecodeError:
                    pass
                continue
            if isinstance(record, dict):
                spoken.extend(_spoken(record))
        return spoken


class Harvester:
    """Tails every live session's transcript and applies what it finds.

    `roster_path` is the roster to mutate; `offsets_path` is where this reader
    remembers how far it has read, and must outlive the process.

    `seat` is how a hire becomes staff. Hiring writes a desk; it does not put
    anybody at it, and `hire.hire()` staying free of spawning is what lets this
    whole suite run without opening Terminal windows on the owner's Mac. But a
    desk nobody is at is not a colleague: measured, two agents hired live came
    up `OFFLINE` and stayed there, and their manager -- asked for a status --
    did the work itself and never mentioned either of them. So the wiring
    that already owns both HTTP start doors hands one in: `app.py` passes
    `api.Surface.start_agent`, which goes through the same `_start` as the
    interview door, so the workspace-trust gate cannot be skipped by this path.
    Optional, and a failure costs the seat and not the tick.
    """

    def __init__(self, roster_path: Path | str, offsets_path: Path | str,
                 *, seat: Callable[[str], object] | None = None,
                 routines: object | None = None) -> None:
        self.roster_path = Path(roster_path)
        self.offsets_path = Path(offsets_path)
        self.seat = seat
        #: `api.Surface`, for `create_routine` / `delete_routine` /
        #: `routine_rows`. Its write lock is the one the HTTP routes take, so a
        #: desk and the owner editing routines at once cannot lose a write.
        self.routines = routines
        self._tails: dict[str, _Tail] = self._load()

    # -- where we had got to ------------------------------------------------

    def _load(self) -> dict[str, _Tail]:
        try:
            raw = json.loads(self.offsets_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        rows = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        tails: dict[str, _Tail] = {}
        for session_id, row in rows.items():
            if not isinstance(session_id, str) or not isinstance(row, dict):
                continue
            offset = row.get("offset")
            inode = row.get("inode")
            seen = row.get("seen")
            tails[session_id] = _Tail(
                offset=offset if isinstance(offset, int) and offset >= 0 else 0,
                inode=inode if isinstance(inode, int) else None,
                seen=[s for s in seen if isinstance(s, str)] if isinstance(seen, list) else [],
            )
        return tails

    def _save(self) -> None:
        """Atomic: a poll interrupted mid-write must not leave a torn offsets
        file, because an unreadable one reads as byte 0 for every session."""
        payload = {
            "version": 1,
            "sessions": {sid: tail.as_dict() for sid, tail in self._tails.items()},
        }
        try:
            self.offsets_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.offsets_path.with_suffix(self.offsets_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.offsets_path)
        except OSError:
            # A failed write costs a re-read, never a crashed tick.
            pass

    # -- the loop -----------------------------------------------------------

    def poll(self, sessions: list[dict]) -> list[dict]:
        """Read every session's new output and apply what it says about itself.

        Returns one record per line acted on, in the order they were said:

            {"session_id", "actor", "kind": "desk"|"hire"|"said",
             "result": "patched"|"hired"|"posted"|"refused", "name", "reason"}

        `actor` is always the session's own desk name. `name` is the desk the
        line was about -- the actor's own for a patch that lands, the requested
        name for a hire or a refusal. Never raises: a session's output is
        untrusted, and one bad line must not stop the tick.
        """
        applied: list[dict] = []
        roster_now = load_roster(self.roster_path)
        desks = {d.name for d in roster_now}
        # How many desks are actually staffed -- NOT how many Claude Code
        # sessions this Mac happens to be running. `sessions` is the collector's
        # whole card list: the owner's own terminals, every agent worktree,
        # every unrelated project window. Feeding `len(sessions)` to
        # `hire.hire`'s cap made the size of his COMPANY a function of how many
        # windows he had open, and on this path the refusal is silent -- it goes
        # to the ledger as `hire_refused` and nothing tells him his manager
        # tried to build a team and was turned down. Recomputed after every
        # hire below, because a hire is exactly what changes it.
        live = roster.live_desks(roster_now, sessions)
        touched = False

        for session in sessions:
            session_id = session.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            path = self._transcript(session)
            if path is None:
                continue

            tail = self._tails.get(session_id)
            if tail is None:
                tail = _Tail()
                self._tails[session_id] = tail

            before = (tail.offset, tail.inode, len(tail.seen))
            lines = tail.read(path)
            if (tail.offset, tail.inode, len(tail.seen)) != before:
                touched = True
            if not lines:
                continue

            # The actor is the desk this session sits at, and nothing else. A
            # session with no desk speaks for nobody -- but its bytes are still
            # consumed above, so its history never fires retroactively.
            actor = session.get("name")
            if not isinstance(actor, str) or actor not in desks:
                continue

            for spoken in lines:
                # The entry's identity when it has one, so a desk saying "ok"
                # twice is heard twice; the text only for keyless lines.
                digest = _digest(spoken.key or spoken.text)
                if digest in tail.seen:
                    continue
                results = self._apply(session_id, actor, spoken.text, live=live)
                # After `_apply`, so a desk that names itself and speaks in one
                # turn is on the roster before its words are attributed.
                if spoken.to_owner:
                    results = results + self._say(session_id, actor, spoken.text)
                tail.remember(digest)
                touched = True
                if results:
                    applied.extend(results)
                    roster_now = load_roster(self.roster_path)
                    desks = {d.name for d in roster_now}
                    # A desk hired one line ago is not seated yet -- no session
                    # is at it until something starts one -- so this does not
                    # move on a hire. It is reread anyway because the roster
                    # did, and a count derived from a stale roster is the class
                    # of bug this line exists to close.
                    live = roster.live_desks(roster_now, sessions)

        if touched:
            self._save()
        return applied

    def _transcript(self, session: dict) -> Path | None:
        """Where this session's output is written. A card carries `cwd` and
        `session_id`; `transcript` is an explicit override for a caller that
        already knows."""
        explicit = session.get("transcript")
        if isinstance(explicit, str) and explicit:
            return Path(explicit)
        cwd = session.get("cwd")
        session_id = session.get("session_id")
        if not isinstance(cwd, str) or not cwd:
            return None
        return paths.transcript_path(cwd, str(session_id))

    def _log(self, record: dict) -> None:
        """Append one harvested record to the bus, in the shape
        `hooks/cc-bus.js` writes: a float `ts`, an `event`, then the event's
        own fields. Never raises -- an unwritable ledger costs an audit line,
        not the tick that was doing real work.
        """
        line = {
            "ts": time.time(),
            "event": {"desk": "desk", "routine": "desk_routine"}.get(
                record["kind"], "hire_refused"),
            "session_id": record["session_id"],
            "actor": record["actor"],
            "name": record["name"],
            "result": record["result"],
            "reason": record["reason"],
        }
        try:
            bus = events_path(self.roster_path)
            bus.parent.mkdir(parents=True, exist_ok=True)
            with bus.open("a") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            pass

    def _say(self, session_id: str, actor: str, line: str) -> list[dict]:
        """Put a desk's turn-final prose in that desk's thread with the owner.

        Down `office.send`, the same queue his own replies go down and the same
        file `Surface` already tails -- so the two halves are one conversation
        in one order, and nothing in the client had to learn a second source.
        Addressed to the owner's *role*, which has no desk and no delivery
        hook, so this is never queued at anybody: it is a record of something
        already said, not a message waiting to be handed over.

        Exactly-once rests on the offsets file above, not on anything here: by
        the time a line reaches this method the tail has already advanced past
        it and written that down, so the next tick -- and the next process --
        start after it.
        """
        body = chat_text(line)
        if not body:
            return []  # a turn that was only a marker is not a message
        result = office.send(OWNER_INBOX, body, sender=actor,
                             extra={"spoke": True})
        return [{
            "session_id": session_id, "actor": actor, "kind": "said",
            "result": "posted" if result.get("ok") else "refused",
            "name": actor,
            # The daemon prints these. The message itself never goes in the
            # log: it is the owner's conversation, and it is already on the
            # queue where the board can read it.
            "reason": "" if result.get("ok") else str(result.get("detail") or ""),
        }]

    def _refused_markers(self, session_id: str, actor: str, line: str) -> list[dict]:
        """A refusal for each physical marker line the parser did not take.

        Asked PER LINE, using `parse_lines` itself as the oracle. It used to be
        asked once for the whole block and only when that block yielded
        *nothing*, which meant a marker line sharing a block with any
        well-formed one vanished with no trace at all -- the cwd-less
        `YOS_HIRE` that a live hire attempt was lost to.
        """
        out: list[dict] = []
        for raw in line.splitlines():
            stripped = raw.strip()
            if not is_marker_line(stripped) or _opens(stripped, ROUTINE_PREFIX):
                continue  # a routine line is `_schedule`'s, refusals included
            patches, hires = onboard.parse_lines(stripped)
            if patches or hires:
                continue  # this one was understood; it is not a refusal
            marker = next(m for m in _MARKERS if stripped.startswith(m))
            try:
                payload = json.loads(stripped[len(marker):])
                detail = _missing_fields(marker, payload)
            except (ValueError, RecursionError) as exc:
                detail = str(exc)
            record = {
                "session_id": session_id, "actor": actor, "name": actor,
                "kind": "desk" if marker == onboard.DESK_PREFIX else "hire",
                "result": "refused", "reason": f"{marker}: {detail}"[:MAX_REASON],
            }
            self._log(record)
            out.append(record)
        return out

    def _apply(self, session_id: str, actor: str, line: str, *, live: int) -> list[dict]:
        """Everything one line of output asks for. Refusals are results too."""
        try:
            patches, hires = onboard.parse_lines(line)
        except Exception:
            # parse_lines is documented never to raise; if that ever stops
            # being true it must cost one line, not the whole tick.
            return []

        # Every marker line the parser did not take, whether or not the block
        # also carried one it did. A refusal for the second line is exactly the
        # record that used to be hidden by the first line succeeding.
        out: list[dict] = self._refused_markers(session_id, actor, line)
        for patch in patches:
            record = {
                "session_id": session_id,
                "actor": actor,
                "kind": "desk",
                "result": "patched",
                "name": patch.name or actor,
                "reason": "",
            }
            try:
                desk = onboard.apply_patch(self.roster_path, actor, patch)
            except onboard.OnboardError as exc:
                record.update(result="refused", reason=exc.reason)
            except OSError as exc:
                record.update(result="refused", reason="unwritable", name=str(exc))
            else:
                record["name"] = desk.name
                actor = desk.name  # a rename takes effect for the rest of the line
            self._log(record)
            out.append(record)

        for request in hires:
            record = {
                "session_id": session_id,
                "actor": actor,
                "kind": "hire",
                "result": "hired",
                "name": request.name,
                "reason": "",
            }
            try:
                onboard.apply_hire(self.roster_path, actor, request, live_count=live)
            except (HireError, onboard.OnboardError) as exc:
                record.update(result="refused", reason=exc.reason)
            except OSError:
                record.update(result="refused", reason="unwritable")
            # A hire that landed is already in the ledger, written by
            # `hire.hire()` before it touched the roster. Logging it again here
            # would double-count every hire on the board's own audit trail.
            if record["result"] == "refused":
                self._log(record)
            self._tell_the_boss(actor, record)
            out.append(record)

        for raw in line.splitlines():
            if _opens(raw.strip(), ROUTINE_PREFIX):
                out.append(self._schedule(session_id, actor, raw.strip()))
        return out

    def _schedule(self, session_id: str, actor: str, line: str) -> dict:
        """One `YOS_ROUTINE` line: create, delete or list the ACTOR's routines.

        The routine's agent is always `actor`; an `agent` on the line is
        ignored, and deleting a routine the actor does not own is refused
        `not_yours` -- the privilege rule `apply_patch` holds for the roster.
        The desk is told the outcome by the deck either way: a desk that
        believes it scheduled something it did not is this defect again.
        """
        record = {"session_id": session_id, "actor": actor, "kind": "routine",
                  "result": "refused", "name": "", "reason": ""}
        payload = (onboard._payload(line, ROUTINE_PREFIX)
                   if len(line) <= onboard.MAX_LINE else None)
        try:
            if self.routines is None:
                raise HireError("routines_unavailable")
            if payload is None:
                raise HireError("not_json", "the line is not one JSON object")
            mine = [r for r in self.routines.routine_rows() if r["agent"] == actor]
            if "delete" in payload:
                rid = str(payload.get("delete") or "")
                record["name"] = rid
                if rid not in {r["id"] for r in mine}:
                    raise HireError("not_yours", f"no routine {rid!r} belongs to {actor}")
                self.routines.delete_routine(rid)
                mine = [r for r in mine if r["id"] != rid]
                record["result"], head = "deleted", f"Deleted routine {rid}."
            elif payload.get("list"):
                record["result"], head = "listed", "Your routines in the deck:"
            else:
                missing = [f for f in ("cron", "tz", "prompt")
                           if not isinstance(payload.get(f), str)
                           or not payload[f].strip()]
                if missing:
                    raise HireError("missing_field",
                                    "missing required field(s): " + ", ".join(missing))
                row = self.routines.create_routine({
                    "agent": actor, "prompt": payload["prompt"].strip(),
                    "trigger": {"kind": "cron", "spec": payload["cron"].strip(),
                                "tz": payload["tz"].strip()}})
                mine.append(row)
                record.update(result="scheduled", name=row["id"])
                head = (f"Scheduled routine {row['id']}. It is in the deck's "
                        "Routines panel, survives your restarts, and arrives as "
                        "a message from the deck when it fires.")
            rows = [f"- {r['id']}: {r['trigger'].get('spec')} "
                    f"({r['trigger'].get('tz')}) {r['prompt'][:80]}" for r in mine]
            text = "\n".join([head] + (rows or ["(none)"]))
        except Exception as exc:  # untrusted input: one bad line, one refusal
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            detail = getattr(exc, "detail", "") or str(exc)
            if isinstance(detail, dict):  # api.Refused keeps its body here
                detail = detail.get("detail") or reason
            record["reason"] = (str(detail) if reason == "missing_field"
                                else reason)[:MAX_REASON]
            text = (f"Not scheduled: {reason} -- {str(detail)[:MAX_REASON]}. "
                    "Nothing changed in the deck; do not report otherwise.")
        self._log(record)
        try:
            office.send(actor, text, sender=DECK_SENDER)
        except OSError:
            pass  # an undelivered receipt costs a line, never the tick
        return record

    def _seat(self, name: str) -> bool:
        """Put a session at the desk just hired. True if one is there now.

        Never raises. Opening a window can fail on a busy Mac, on an engine
        `build_argv` does not know, or on a trust dialog -- and none of those
        may cost the tick that keeps every card on the board up to date. The
        boss is told either way; a manager addressing a desk nobody is at gets
        `delivered: false` and no explanation, which is how a report becomes
        invisible twice.
        """
        if self.seat is None:
            return False
        try:
            self.seat(name)
        except Exception:
            return False
        return True

    def _tell_the_boss(self, boss: str, record: dict) -> None:
        """One line to the desk that did the hiring, in its own thread.

        The missing half of `hire.brief`. The brief tells the new hire who its
        boss is; nothing told the boss it now has a report, and a system prompt
        is frozen at spawn so there is no later edition of it to add to. This
        goes down `office.send` -- the same queue the owner's own messages use
        and the same one `_say` writes to -- so the manager reads it as an
        ordinary message on its next turn.

        A refusal is sent too. A desk told it can hire and then silently
        refused reports the hire it asked for as a hire that happened, which is
        the defect this is inside, wearing different clothes.
        """
        name = record.get("name") or "the new desk"
        if record.get("result") == "hired":
            seated = self._seat(name)
            where = ("A session is at that desk now -- brief it."
                     if seated else
                     "The desk is on the roster but nobody is sitting at it "
                     "yet: it reads OFFLINE and will not answer a message "
                     "until someone is.")
            text = f"Hired: {name} now reports to you. {where}"
        else:
            text = (f"Not hired: {name} was refused, {record.get('reason')}. "
                    "Nobody was added to your team -- do not report otherwise.")
        try:
            office.send(boss, text, sender=DECK_SENDER)
        except OSError:
            pass  # an undelivered receipt costs a line, never the tick
