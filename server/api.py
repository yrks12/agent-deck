"""/v1 -- the JSON surface a native macOS / iOS client talks to.

`server/app.py` is the deck's own web app: it serves the board, the pages, the
hooks' hot path. This module is a **separate, versioned, client-facing** router
mounted onto it, so the app that a phone runs is never coupled to the shape of
`/api/state`. Wiring is one call from the daemon:

    from . import api
    api.register(app, snapshot=lambda: _state, comms=collector.comms,
                 deliver=_try_inject)

Four rules hold this module together, each of them a bug that would only show
up on a real device:

1. **Cursors, never offsets.** A cursor names a *message*; an offset names a
   *position*, and positions shift under an append. A client paging backwards
   through history while its agent keeps talking skips exactly as many messages
   as arrived between two requests. `_cursor()` is a fixed-width, lexically
   sortable `(timestamp, id)` key, and every page is a bisect on it.

2. **`GET /v1/agents` is on the app-foreground path**, so it may not do work
   proportional to history. The message log is tailed by byte offset
   (`_MessageLog`, the same trick `sources/transcript.py` uses on 8 MB
   transcripts): a warm request reads *no* message bytes at all. Previews and
   unread counts are folded forward as records arrive, never recomputed by
   re-reading transcripts.

3. **Fail closed.** No token configured means `/v1` answers 503, not "open".
   The daemon binds 127.0.0.1 and this module does not change that: the
   intended route to a phone is an SSH tunnel or a private mesh (Tailscale),
   never a public port. See `docs/client-api.md`.

4. **Strict JSON, one timestamp format.** Python emits `NaN` by default and a
   strict mobile parser rejects the whole document. `StrictJSON` renders with
   `allow_nan=False` over a sanitised payload, and every time field in every
   payload is epoch **seconds as a float**.

State lives where it already lives. Desks are `server/roster.py`. Owner->agent
messages are `server/office.py`'s `messages.jsonl`. Agent<->agent traffic is
the collector's `CommsIndex`. The only thing this module owns is a small
sidecar of client-only preferences (`agent_prefs.json`): avatar, section,
notifications, pinned, and the per-agent read cursor. Those are deliberately
NOT on the roster: `roster.Desk` is the org chart, and a phone toggling a
notification switch must not be able to rewrite it.
"""

from __future__ import annotations

import asyncio
import bisect
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import (ask_recorder, asking, autoreview, browser_reaper, computer_heal,
               browser_takeover, decisions,
               groups, handoff, hire, login_requests, login_vault, office,
               onboard,
               ntfy, owner_alerts, paths, pretrust, push_policy, ringing,
               roster, routines, standing,
               sandbox, screen, screen_stream, seat, spawn,
               terminal_stream)
from .sources import comms as comms_mod

# ── contract constants ───────────────────────────────────────────────────────

TOKEN_ENV = "AGENT_DECK_TOKEN"

#: Who the human is, on the wire. One spelling, everywhere.
_LOG = logging.getLogger(__name__)

OWNER = office.OWNER_HANDLE
#: Senders in `messages.jsonl` that mean "this came from the owner's side".
#: `routine` is the scheduler speaking on his behalf (`app._deliver_routine`).
#:
#: Owned by `office` now, not merely spelled the same there. This set decided
#: `"role": "owner"` for the app and stopped -- it never reached the agent, so
#: a desk read him as a peer and refused him. `office.attribute` is the last
#: hop, and it has to ask the SAME question this does or the two answers drift
#: and one door is open.
OWNER_SENDERS = office.OWNER_SENDERS
#: The owner typing, as opposed to the deck acting for him. Only these two are
#: `role: "owner"`; every other `OWNER_SENDERS` member is `role: "system"`.
TYPED_BY_HIM = office.TYPED_BY_HIM
#: Senders that are neither him nor a desk: drawn as `role: "system"` under
#: their own name. `engineer` is deliberately not an owner sender.
SYSTEM_SENDERS = frozenset({"routine", "deck", office.ENGINEER,
                            office.ENGINEER_TEST})
#: Who a POST may claim to be (K2). Anything else is `bad_sender`.
POST_SENDERS = frozenset({OWNER, office.ENGINEER})
#: What an app may send as `as` to mean him, whatever this deck's handle is:
#: the neutral id, so one app build works against every deck.
OWNER_ALIASES = frozenset({OWNER, "owner"})
CHANNELS = frozenset({"text", "voice"})
#: Appended to a voice utterance in the office record (K6) so the desk knows
#: it was spoken; stripped again before the app sees the text.
VOICE_NOTE = "\n(said on a live call)"
#: Appended to a recorded voice message's transcript for the desk, stripped
#: again before the app sees it; the record's `voice_note` names the audio.
RECORDED_NOTE = "\n(sent as a voice message; this is its transcript)"

#: `blocked.reason` for a seated desk frozen on a permission dialog the deck
#: cannot relay. Documented in `docs/client-api.md` §3.1 -- a slug the client
#: does not know renders as a generic fallback forever, so the table is part of
#: the change, not a follow-up.
STUCK_ON_DIALOG = "dialog_unrelayed"


def delivery_note(reached: list[str], waiting: list[str], *,
                  waking: list[str] = ()) -> dict:
    """What became of a message, in words a client can put on the screen. PURE.

    **The hole this fills.** `POST /v1/threads/{id}/messages` has always
    answered `delivered: true|false`, and `docs/client-api.md` has always said
    `false` means "queued, not lost". On the owner's screen the two rendered
    identically, so a message that reached nobody looked exactly like one that
    landed -- and when the office hook was not installed on that machine,
    `false` really did mean lost. He asked "why is my messages getting
    disappeared?" about a state the API had already reported to him.

    One vocabulary for both doors. A direct send passes a single name in one
    list or the other; a group fan-out passes the two lists it already
    computes. The client renders one thing either way instead of a special
    case per endpoint.

    `state` is the field to branch on and it is a closed set:

        delivered   every named desk took it down its live socket
        queued      none did; it waits in `messages.jsonl` for their next turn
        mixed       some did (a group where only part of the room is seated)

    `what` is a whole sentence, addressed to the owner, safe to show verbatim.
    It never says "failed": nothing failed. The record is on disk and
    `hooks/cc-office.js` reads it out on the desk's next turn.

    `waking` names the waiting desks that are ASLEEP and being woken right now
    (K3). When that is every one of them, `reason` is `waking` and `what` says
    so -- MEASURED live on atlas: without it a POST to a sleeping desk answered
    "not at a desk right now ... read on the next turn there" while the deck
    was in fact waking it. `reason` is otherwise "".
    """
    reached, waiting = list(reached), list(waiting)
    if waiting and not reached and set(waiting) <= set(waking):
        verb = "was" if len(waiting) == 1 else "were"
        return {"state": "queued", "reason": WAKING, "reached": reached,
                "waiting": waiting,
                "what": f"{_names(waiting)} {verb} asleep and "
                        f"{_is(waiting)} being woken to read this."}
    if not waiting:
        state = "delivered"
        what = f"Read now by {_names(reached)}." if reached else "Sent."
    elif reached:
        state = "mixed"
        what = (f"{_names(reached)} read this now. {_names(waiting)} "
                f"{_is(waiting)} not at a desk right now, so it is waiting and "
                f"will be read on the next turn there.")
    else:
        state = "queued"
        what = (f"{_names(waiting)} {_is(waiting)} not at a desk right now. "
                f"This is waiting and will be read on the next turn there -- "
                f"it is not lost, and sending it again would send it twice.")
    return {"state": state, "reason": "", "reached": reached,
            "waiting": waiting, "what": what}


#: `delivery.reason` on a message that reached nobody. A CLOSED set of two,
#: documented in `docs/client-api.md` §3.4, because the client renders a
#: sentence per slug and falls back to something generic for one it does not
#: know. The specifics -- which dialog, which failed start -- ride in `what`
#: rather than in a slug, so `blocked` can grow new causes without every client
#: needing a release to describe them.
NO_SESSION = "no_session"
DESK_BLOCKED = "desk_blocked"
#: A third reason (K3): the desk is asleep and resumable, so the message is on
#: its way rather than stuck. Its `state` is `sent`, not `undelivered`.
WAKING = "waking"

#: Card states that mean nobody is there to take a message. `OFFLINE` is
#: `roster.occupancy`'s own word for a desk with no session joined to it;
#: `DEAD` is a card the collector has watched go away and has not yet dropped.
DARK_STATES = frozenset({"OFFLINE", "DEAD"})


def delivery_of(name: str, *, acked: bool, state: str, session_id,
                blocked: dict | None, asleep: bool = False) -> dict:
    """What became of ONE message to `name`, as of right now. PURE.

    **The hole this fills.** The box's Claude login expired, every desk started
    and died instantly, and he typed "hi?", "whats goingon?", "hello?", "?",
    "hello" into his chief's thread over some hours. The app drew all five
    exactly like messages that had landed, because `GET
    /v1/threads/{id}/messages` said nothing at all about what became of one.
    He ended up asking whether the deck was offline. His words: "i cant see
    sent/read".

    Every input here is evidence the deck already holds:

        acked        an `{"ack": id}` record is in `messages.jsonl`. Written by
                     exactly two things -- `office.ack`, after the bytes went
                     down a live socket, and `hooks/cc-office.js:ackMessages`,
                     when a session took the message into a turn.
        state        `roster.occupancy`'s join of desk to live card.
        blocked      `Surface._blocked` -- the desk is there and cannot move.

    THREE STATES, not four. There is no `read`: both ack writers append the
    same bare `{"ts", "ack"}` record, so nothing on disk separates "a session
    took it" from "the model read it". A `read` state would be a guess wearing
    a receipt's clothes, on the one screen this change exists to make honest.

    CALLED PER READ, never stored. A state stamped at write time would have
    frozen his five messages at whatever was true the second he pressed
    return -- which is the same lie one layer along, and the reason
    `test_a_message_taken_after_the_desk_came_back_reads_delivered` exists.

    `what` is a whole sentence, addressed to him, safe to show verbatim. Every
    branch names the desk, because "not delivered" without a name is the blank
    thread again in two words.
    """
    if acked:
        return {"state": "delivered", "reason": "",
                "what": f"{name}'s session took this."}
    if blocked:
        why = str((blocked or {}).get("what") or "it is stopped")
        return {"state": "undelivered", "reason": DESK_BLOCKED,
                "what": f"{name} has not seen this: {why}. It is on the queue "
                        f"and is handed over as soon as that desk can move."}
    if asleep or str(state or "") == "ASLEEP":
        return {"state": "sent", "reason": WAKING,
                "what": f"{name} was asleep and is being woken to read this."}
    if not session_id or str(state or "") in DARK_STATES:
        return {"state": "undelivered", "reason": NO_SESSION,
                "what": f"Nobody is at {name}'s desk, so nothing has taken "
                        f"this. It is on the queue and is handed over the "
                        f"moment a session starts there -- but until one does, "
                        f"{name} has not seen it."}
    return {"state": "sent", "reason": "",
            "what": f"{name} is at its desk and has not picked this up yet. "
                    f"It is handed over on that desk's next turn."}


def _is(names: list[str]) -> str:
    """Agreement for the sentence `_names` opens. One desk is, two are."""
    return "is" if len(names) == 1 else "are"


#: `AvatarShape` in the app (`macos/Sources/DeckKit/Models/AvatarLook.swift`).
AVATAR_SHAPES = ("blob", "hexagon", "wedge", "tablet", "pebble", "teardrop",
                 "cloud", "squircle")
AVATAR_COLORS = 12
VOICE_RATE = (0.1, 2.0)


def _look_of(agent: dict) -> tuple:
    """What a client draws a desk with: the character, label, call voice and
    the line under its name."""
    return (agent.get("avatar_look"), agent.get("label"), agent.get("voice"),
            agent.get("description"))


def _voice(value) -> dict | None:
    """K6 `voice` -> what the roster stores. None clears it. Refuses 400."""
    if value is None:
        return None
    ok = isinstance(value, dict) and isinstance(value.get("id"), str)
    rate = value.get("rate", 1.0) if ok else None
    if ok and (isinstance(rate, bool) or not isinstance(rate, (int, float))
               or not VOICE_RATE[0] <= rate <= VOICE_RATE[1]):
        ok = False
    if not ok:
        raise Refused(400, "bad_voice",
                      '`voice` is {"id": <voice identifier>, "rate": '
                      f'{VOICE_RATE[0]}-{VOICE_RATE[1]}}} or null')
    return {"id": value["id"].strip()[:200], "rate": float(rate)}


def _description(value) -> str:
    """`description` -> what the roster stores. null clears it. Refuses 400."""
    text = roster.clean_description(value) if value is not None else ""
    if (value is not None and not isinstance(value, str)) or (
            len(text) > roster.DESCRIPTION_MAX):
        raise Refused(400, "bad_description",
                      "`description` is one or two sentences, at most "
                      f"{roster.DESCRIPTION_MAX} characters, or null")
    return text


def _avatar_look(value) -> dict | None:
    """K6 `avatar` object -> the roster's `avatar_look`. None clears it."""
    if value is None:
        return None
    color = value.get("color") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("shape") not in AVATAR_SHAPES
            or isinstance(color, bool) or not isinstance(color, int)
            or not 0 <= color < AVATAR_COLORS):
        raise Refused(400, "bad_avatar",
                      f'`avatar` is {{"shape": one of {", ".join(AVATAR_SHAPES)}, '
                      f'"color": 0-{AVATAR_COLORS - 1}}}')
    return {"shape": value["shape"], "color": color}


def _names(names: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`. PURE. For prose the owner reads, so the
    list is joined the way he would say it, not with a bare comma."""
    if len(names) <= 1:
        return names[0] if names else "nobody"
    return f"{', '.join(names[:-1])} and {names[-1]}"

DEFAULT_SECTION = "Work"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
PREVIEW_CHARS = 140
#: Kept in memory per thread. The office log is append-only on disk; this is
#: the window a client can page through without us re-reading the file.
MAX_THREAD_MESSAGES = 4000
#: SSE: how long a quiet stream waits before proving the connection is alive.
#: The client's "Checking connection / Reconnecting" indicator keys off this.
HEARTBEAT_SECONDS = 15.0
#: How often the shared background task folds the collector snapshot forward.
REFRESH_SECONDS = 1.0
#: The engines `spawn.build_argv` knows how to start. Refused at the boundary,
#: because a desk written with any other engine can never be sat at.
ENGINES = ("claude", "opencode", "codex")
#: What a desk hired through the interview door is called until it names
#: itself, and what it is told it is. The name is a placeholder on purpose:
#: the whole point of that door is that the agent picks its own.
PLACEHOLDER_PREFIX = "new-hire"
PROVISIONAL_LABEL = "New"
PROVISIONAL_CHARTER = (
    "You have just been hired and nothing about you has been decided yet. The "
    "name on this desk is a placeholder you are going to replace: work out "
    "what the job actually is in conversation with the owner, then name "
    "yourself."
)
#: Runs shown per routine, newest first. `routines.runs` keeps MAX_RUNS; the
#: panel wants "did the last few work", not an audit trail.
ROUTINE_RUNS_SHOWN = 3

#: A group refusal that is the client's request being malformed, rather than
#: the org saying no. Everything else is a 409.
_GROUP_STATUS = {"missing_field": 400, "no_members": 400, "unknown_group": 404}

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_PATH = re.compile(r"(?<![\w~.])~?/[\w./~+@-]*[\w~+@-]")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg")
_CURSOR = re.compile(r"^\d{18}-\S+$")


# ── serialisation ────────────────────────────────────────────────────────────


def _num(value) -> float | None:
    """A float a strict JSON parser will accept, or None.

    NaN reaches us for real: a session card's `state_since` is arithmetic over
    fields that can be absent. Emitting it poisons the whole document for a
    client that parses strictly, and the failure looks like "the app shows
    nothing" rather than "one timestamp was bad".
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clean(value):
    """Recursively replace every non-finite float with None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


class StrictJSON(JSONResponse):
    """`allow_nan=False` over a sanitised payload.

    The sanitiser is what makes this never raise; `allow_nan=False` is what
    makes a future bug loud in the test suite instead of silent on a device.
    """

    def render(self, content) -> bytes:
        return json.dumps(
            _clean(content), ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


class Refused(HTTPException):
    """A refusal in the deck's existing shape: `{ok, reason, detail}`.

    Same `reason` slug convention as `manager.InjectError`, `spawn.SpawnError`
    and `hire.HireError`, so a client reads every refusal on this machine the
    same way and can branch on a string rather than on prose.
    """

    def __init__(self, status_code: int, reason: str, detail: str = "",
                 headers: dict | None = None) -> None:
        super().__init__(
            status_code=status_code,
            detail={"ok": False, "reason": reason, "detail": detail or reason},
            headers=headers,
        )
        self.reason = reason


async def _refused_handler(request: Request, exc: Refused) -> StrictJSON:
    body = exc.detail if isinstance(exc.detail, dict) else {
        "ok": False, "reason": "error", "detail": str(exc.detail)}
    return StrictJSON(body, status_code=exc.status_code,
                      headers=getattr(exc, "headers", None))


# ── cursors ──────────────────────────────────────────────────────────────────


def _cursor(ts: float, message_id: str) -> str:
    """A total order over messages: `<micros, 18 digits>-<id>`.

    Fixed width so lexical order *is* numeric order, which is what lets every
    page be a `bisect` on a plain sorted list of strings. Ties on the timestamp
    are broken by the id, so the order is total and stable -- two messages
    written in the same microsecond still page deterministically.
    """
    micros = _num(ts) or 0.0
    return f"{int(round(micros * 1_000_000)):018d}-{message_id}"


def _check_cursor(value, field: str) -> str | None:
    """A cursor this API issued, or a 400 naming which parameter was wrong.

    Deliberately total: a client sends a JSON body, so `value` can be any JSON
    type. A dict reaching the regex is a 500, and a 500 is a shape no client
    has a handler for.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _CURSOR.match(value):
        raise Refused(400, "bad_cursor",
                      f"{field}={value!r} is not a cursor from this API")
    return value


def _required(payload: dict, key: str) -> str:
    """A field the route cannot proceed without, named in the refusal.

    "Bad request" with no field name is a client bug that takes an afternoon to
    find on a device with no debugger.
    """
    value = str(payload.get(key) or "").strip()
    if not value:
        raise Refused(400, "missing_field", f"{key} is required")
    return value


def _clamp_limit(value) -> int:
    """Page size, clamped rather than refused.

    FastAPI's own `ge=`/`le=` validation answers 422 with `{"detail": [...]}`,
    which is a body shape that appears nowhere else on this API. Clamping keeps
    the one documented error shape true for every route.
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, limit))


def _keys(messages: list[dict]) -> list[str]:
    return [m["cursor"] for m in messages]


# ── artefacts ────────────────────────────────────────────────────────────────


def attachments(text: str) -> list[dict]:
    """File paths, images and links a bubble should render as chips.

    Derived from the text, never stored: the deck's message log carries a
    string and nothing else. A client that wants a real attachment model gets
    it when there is a real one to serve.
    """
    from . import uploads  # it imports this module

    found: list[dict] = []
    seen: set[str] = set()
    remainder = text or ""

    for url in _URL.findall(remainder):
        if url not in seen:
            seen.add(url)
            found.append({"kind": "link", "value": url})
    remainder = _URL.sub(" ", remainder)

    for path in _PATH.findall(remainder):
        if path in seen or len(path) < 3:
            continue
        seen.add(path)
        kind = "image" if path.lower().endswith(_IMAGE_SUFFIXES) else "file"
        entry = {"kind": kind, "value": path}
        # One he sent from the phone: its bytes are on this box, so his bubble
        # can draw it (`uploads`).
        # Owner, 2026-10-01: and one an agent sent him (`deck_mcp.send_file`)
        # says what it is, so a video plays instead of being a path.
        entry.update(uploads.describe(path))
        found.append(entry)

    return found[:10]


_SENT_YOU = {"video": "a video", "image": "an image", "audio": "an audio clip",
             "pdf": "a PDF"}
_ATTACHED_LINE = re.compile(r"^Attached (?:image|video|audio|file): \S+$")


def alert_body(text: str, found: list[dict]) -> str:
    """A push's words. "sent you a video: <caption>" when the message carries a
    file the deck holds, never the box path; otherwise the words. PURE."""
    held = [a for a in found if a.get("url")]
    if not held:
        return text
    what = _SENT_YOU.get(str(held[0].get("media") or ""), "a file")
    words = "\n".join(line for line in (text or "").splitlines()
                      if not _ATTACHED_LINE.match(line.strip())).strip()
    return f"sent you {what}: {words}" if words else f"sent you {what}"


def _one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ── thread ids ───────────────────────────────────────────────────────────────


def direct_id(agent: str) -> str:
    return f"direct:{agent}"


def peer_id(a: str, b: str) -> str:
    """`peer:<lower>|<higher>`. Sorted so one pair is always one thread."""
    first, second = sorted([a, b])
    return f"peer:{first}|{second}"


def _parse_thread_id(thread_id: str) -> tuple[str, tuple[str, ...]]:
    kind, _, rest = (thread_id or "").partition(":")
    if kind == "direct" and rest:
        return "direct", (rest,)
    if kind == "peer" and "|" in rest:
        return "peer", tuple(rest.split("|", 1))
    # A group's participants are its members, and they live in the groups file
    # rather than in the id. What the id carries is the group's name.
    if kind == "group" and rest:
        return "group", (rest,)
    raise Refused(404, "unknown_thread", f"{thread_id!r} is not a thread id")


def describe_tool_short(tool: str) -> str:
    """`Bash` -> `run`, `Read` -> `read`: the verb a permission row reads with."""
    return {"Bash": "run", "Read": "read", "Write": "write", "Edit": "edit",
            "NotebookEdit": "edit", "WebFetch": "fetch"}.get(
                tool, f"use {asking.describe_tool(tool)}")


def ask_is_from(ask: asking.Ask | handoff.Handoff, *, name: str = "",
                cwd: str = "", session_id: str = "") -> bool:
    """Did the desk described by these three fields raise this? PURE.

    Takes an `asking.Ask` or a `handoff.Handoff`: both record a blocked desk in
    `.agent` the same three ways, and a secure handoff drawn against a session
    UUID is invisible in the app for exactly the reason an approval was. A
    `Handoff` carries no `.cwd`, so the folder rule below simply does not fire
    for one -- read off with a default rather than branching on the type.

    **The one rule, in one place.** Two callers need it and they must never
    disagree: `Surface._answerable` asks "is there a live question this desk
    could settle from the app", and `Surface._desk_for_ask` asks the same thing
    from the other end, "which desk does this question belong to". When those
    two answered differently, a desk was published as merely stopped while its
    question sat on the board under a name nothing could match.

    Three ways to recognise a desk, because the ask ledger records whichever
    one reached it:

    * **its name** -- the good case, and what `asking.Ask.agent` is documented
      to hold;
    * **its session id** -- `hooks/cc-permission.js` sends no desk name, so
      `app._permission` falls through to the raw session UUID. Measured live:
      ask `xgdtu` recorded `agent:
      "bfffdc59-..."`, which is a desk to the deck and gibberish to the app;
    * **`session <id>`** -- `ask_recorder._who` truncates that same id to
      `ID_PREFIX` characters and prefixes it when it is composing a line for a
      phone. Imported rather than re-spelled here: two truncations that can
      drift apart is one of them quietly stopping matching.

    ...and failing all three, **its folder**. A question asked from a desk's
    own workspace is that desk's question even when the deck has never seen the
    session id, which is the shape every live blocker had. Checked last, and by
    the caller ordering the passes, because two desks can share a folder while
    a name or a session id names exactly one.
    """
    recorded = str(getattr(ask, "agent", "") or "").strip()
    name = str(name or "").strip()
    if name and recorded == name:
        return True
    ident = str(session_id or "").strip()
    if ident and recorded in (ident,
                              f"session {ident[:ask_recorder.ID_PREFIX]}"):
        return True
    return bool(cwd) and str(getattr(ask, "cwd", "") or "") == cwd


# ── the incremental message log ──────────────────────────────────────────────


class _MessageLog:
    """Byte-offset tail over `office.messages.jsonl`.

    The same rule `sources/transcript.py` lives by: never re-read bytes we have
    already parsed. This is the whole reason `GET /v1/agents` costs the same on
    a log with four messages and one with four hundred thousand -- a warm poll
    stats the file, sees no growth, and opens nothing.
    """

    def __init__(self) -> None:
        self._offset = 0
        self._inode: int | None = None
        self.acked: set[str] = set()

    def _reset(self) -> None:
        self._offset = 0
        self.acked.clear()

    def poll(self, path: Path) -> list[dict]:
        """Records appended since the last poll. Never raises."""
        try:
            stat = path.stat()
        except OSError:
            return []
        # Rotation, truncation, or a test pointing us at a different file.
        if self._inode is not None and (
            stat.st_ino != self._inode or stat.st_size < self._offset
        ):
            self._reset()
        self._inode = stat.st_ino
        if stat.st_size <= self._offset:
            return []

        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(stat.st_size - self._offset)
        except OSError:
            return []

        cut = chunk.rfind(b"\n")
        if cut == -1:
            return []  # half a record: leave the offset and retry next poll
        self._offset += cut + 1

        fresh: list[dict] = []
        for line in chunk[:cut].split(b"\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("ack"):
                self.acked.add(str(record["ack"]))
            elif record.get("id"):
                fresh.append(record)
        return fresh


# ── the surface ──────────────────────────────────────────────────────────────


class Surface:
    """Everything `/v1` serves, folded forward off the collector's snapshot.

    Nothing here polls the filesystem for sessions: `snapshot` is the collector
    tick `app.py` already computes once a second, handed in as a callable. A
    second scanner would double the deck's I/O and could disagree with the
    board about who is live.
    """

    def __init__(self, *, snapshot=None, comms=None, deliver=None,
                 roster_path=None, prefs_path=None, asks_path=None,
                 rules_path=None, routines_path=None, groups_path=None,
                 handoffs_path=None, asleep=None, decisions_path=None,
                 push_policy=None, clock=None) -> None:
        self._snapshot = snapshot or (lambda: {})
        #: When a push may go out (`push_policy`). Built late, beside the
        #: roster, so its state file lands where the deck's state lives.
        self._push = push_policy
        self._clock = clock or time.time
        #: The first refresh reads the whole log: history, never news.
        self._booted = False
        self._tests: set[str] = set()
        #: `(name) -> bool`: is this desk without a live session but resumable?
        #: The fact belongs to the wake module; the daemon wires the real one.
        #: Until then nobody is asleep, and an unseated desk stays `OFFLINE`.
        self._asleep = asleep or (lambda name: False)
        self.comms = comms if comms is not None else comms_mod.CommsIndex()
        #: `(name, text) -> bool`. The daemon passes `app._try_inject`, which
        #: is True only when the bytes actually went out; without it a send is
        #: still queued for the agent's next turn, never dropped.
        self._deliver = deliver
        self._roster_path = roster_path
        self._prefs_path = prefs_path
        self._asks_path = asks_path
        self._rules_path = rules_path
        self._routines_path = routines_path
        self._groups_path = groups_path
        self._handoffs_path = handoffs_path
        self._decisions_path = decisions_path
        #: `{decision id: (state, answer)}` as last announced on the stream.
        #: None until the first refresh, which adopts what is on disk silently
        #: -- a card asked before this process started is not news.
        self._decision_seen: dict[str, tuple] | None = None
        #: Frames produced by a refresh nobody published (a GET, a write):
        #: a refresh reports each change once, so they wait for the next one.
        self._held: list[dict] = []
        #: One refresh at a time: requests and the background tick run it on
        #: worker threads, and it diffs against state it then replaces.
        self._refresh_lock = threading.RLock()
        #: Serialises read-modify-write over the roster and the routine file.
        #: Each write is already atomic; what is not atomic is deciding *what*
        #: to write from what was just read, and two taps on "+" land there.
        self._write_lock = threading.Lock()

        self._log = _MessageLog()
        #: What may notify him (`owner_alerts`), folded off the same log in
        #: the same order: every label is decided once, as the record lands.
        #: Kept sorted by cursor; `_alert_ids` makes a re-read (a rotated log
        #: read from byte zero) a no-op rather than a second notification.
        self._bosses: dict[str, str | None] = {}
        self._classifier = owner_alerts.Classifier(
            boss_of=lambda name: self._bosses.get(name),
            is_test=lambda name: name in self._tests)
        self._alerts: list[dict] = []
        self._alert_ids: set[str] = set()
        # thread id -> messages from the office log, kept sorted by cursor.
        self._logged: dict[str, list[dict]] = {}
        # thread id -> messages rebuilt from the comms index each refresh.
        self._edged: dict[str, list[dict]] = {}
        #: The relay. `direct:<desk>` -> the peer messages that desk sent or
        #: received, so the owner reads a dispatch and its reply in the one
        #: conversation he already has open. Two stores, mirroring the two
        #: above: `_relay` is appended incrementally off the office log,
        #: `_edge_relay` is rebuilt with `_edged` each refresh. The message
        #: objects are SHARED with the peer buckets, not copied -- one record,
        #: two views, one `id`, so a client that dedupes on `id` is right.
        self._relay: dict[str, list[dict]] = {}
        self._edge_relay: dict[str, list[dict]] = {}
        self._threads: dict[str, dict] = {}
        self._agents: list[dict] = []
        #: `{name: (state, session_id, blocked)}` -- what `delivery_of` needs
        #: about a recipient, rebuilt every tick alongside `_agents`.
        self._reach: dict[str, tuple] = {}
        self._prefs: dict[str, dict] = {}
        self._prefs_stamp = None
        #: `{old_name: new_name}` for every desk that has renamed itself. A
        #: desk's name keys its thread, its read cursor and its unread count,
        #: and the interview door renames a desk *while the owner is typing
        #: into it* -- this is what carries the conversation across.
        self._aliases: dict[str, str] = {}
        self._aliases_seen = False
        #: Renames observed since the last event fan-out, `(old, new)`.
        self._renamed: list[tuple[str, str]] = []
        #: Small sidecars beside the roster, cached by (mtime, size). Rule 2:
        #: `GET /v1/agents` is on the app-foreground path, so adding a file
        #: next to the roster must not add a file read to every request.
        self._sidecars: dict[str, tuple] = {}

    # -- paths (resolved late so tests and a restart both see the real one) --

    @property
    def roster_path(self) -> Path:
        return Path(self._roster_path or roster.DEFAULT_PATH)

    @property
    def prefs_path(self) -> Path:
        return Path(self._prefs_path or (paths.BUS_DIR / "agent_prefs.json"))

    @property
    def messages_path(self) -> Path:
        return Path(office.MESSAGES_FILE)

    # Defaulting to each module's own path is what lets the daemon mount this
    # with nothing but a snapshot and still read the asks the hook writes, the
    # rules the hook reads, and the routines the scheduler fires.
    @property
    def asks_path(self) -> Path:
        return Path(self._asks_path or asking.DEFAULT_PATH)

    @property
    def handoffs_path(self) -> Path:
        return Path(self._handoffs_path or handoff.DEFAULT_PATH)

    @property
    def rules_path(self) -> Path:
        return Path(self._rules_path or autoreview.DEFAULT_PATH)

    @property
    def routines_path(self) -> Path:
        return Path(self._routines_path or routines.DEFAULT_PATH)

    @property
    def decisions_path(self) -> Path:
        """Beside the roster, like the mandate: production reads
        `~/.claude/agent-bus/decisions.json`, a test reads its own."""
        return Path(self._decisions_path
                    or self.roster_path.parent / decisions.DEFAULT_PATH.name)

    def _decision_cards(self) -> dict[str, dict]:
        return self._sidecar("decisions", self.decisions_path, decisions.load)

    @property
    def groups_path(self) -> Path:
        return Path(self._groups_path or groups.groups_path(self.roster_path))

    @property
    def mandate_path(self) -> Path:
        """The company a new hire has just joined, in its own words. Derived
        from the roster like every other bus file, so production reads
        `~/.claude/agent-bus/mandate.md` and a test reads its own."""
        return self.roster_path.parent / "mandate.md"

    def mandate(self) -> str:
        """The mandate, or an empty string. NEVER an invented one: a new hire
        told a made-up company mandate would go and act on it."""
        try:
            return self.mandate_path.read_text()
        except OSError:
            return ""

    # -- preferences sidecar -------------------------------------------------

    def _load_prefs(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.prefs_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        agents = raw.get("agents") if isinstance(raw, dict) else None
        return agents if isinstance(agents, dict) else {}

    def _save_prefs(self, prefs: dict[str, dict]) -> None:
        path = self.prefs_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "agents": prefs}))
        os.replace(tmp, path)

    def pref(self, name: str) -> dict:
        return self._prefs.get(name) or {}

    def update_pref(self, name: str, patch: dict) -> dict:
        prefs = self._load_prefs()
        entry = dict(prefs.get(name) or {})
        entry.update(patch)
        prefs[name] = entry
        self._save_prefs(prefs)
        self._prefs = prefs
        return entry

    # -- folding the world forward ------------------------------------------

    def _sidecar(self, key: str, path: Path, load):
        """`load(path)`, re-read only when the file actually changed.

        A `stat` per request instead of an `open`: these files are tiny, but
        the sidebar is polled and the rule this module lives by is that a warm
        request opens nothing it does not have to.
        """
        try:
            info = path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        cached = self._sidecars.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        value = load(path)
        self._sidecars[key] = (stamp, value)
        return value

    def _canonical(self, name: str) -> str:
        """The name a desk goes by now, after any renames."""
        return onboard.resolve(self._aliases, name) if self._aliases else name

    def _canonical_thread(self, thread_id: str) -> str:
        """The thread id a client should be using. An id built on a name a desk
        has since replaced still resolves here, so a client that was mid-request
        when its agent named itself gets the conversation rather than a 404."""
        kind, participants = _parse_thread_id(thread_id)
        if kind == "direct":
            return direct_id(self._canonical(participants[0]))
        if kind == "peer":
            return peer_id(*(self._canonical(p) for p in participants))
        return thread_id

    def _absorb_renames(self, aliases: dict[str, str]) -> None:
        """Move everything keyed on a desk's old name onto its new one.

        The first map this Surface sees is adopted silently: it describes
        renames from before this process started, and announcing those to a
        client that has only just connected is a rename it cannot act on.
        """
        first = not self._aliases_seen
        self._aliases_seen = True
        if aliases == self._aliases:
            return
        fresh = [old for old, new in aliases.items() if self._aliases.get(old) != new]
        self._aliases = aliases
        if first:
            return

        prefs = self._load_prefs()
        moved = False
        for old in fresh:
            new = self._canonical(old)
            if new == old:
                continue
            self._renamed.append((old, new))
            # The read cursor, the section, the pin. Dropping these re-marks
            # the whole conversation unread the moment the agent introduces
            # itself, which is the one moment the owner is watching.
            if old in prefs and new not in prefs:
                prefs[new] = prefs.pop(old)
                moved = True

        for thread_id in list(self._logged):
            target = self._canonical_thread(thread_id)
            if target == thread_id:
                continue
            for message in self._logged.pop(thread_id):
                message["thread_id"] = target
                self._insert(target, message)

        # The relay is keyed on the desk, so it is re-keyed here too -- but the
        # message objects are NOT rewritten. They are the peer buckets' own
        # records, and the loop above has already given them the renamed peer
        # thread id; touching `thread_id` here would overwrite it with a
        # `direct:` id and lose the attribution the client renders from.
        for thread_id in list(self._relay):
            target = self._canonical_thread(thread_id)
            if target == thread_id:
                continue
            for message in self._relay.pop(thread_id):
                self._place(self._relay, target, message)

        if moved:
            self._save_prefs(prefs)
            self._prefs = prefs

    def _message_from_record(self, record: dict) -> tuple[str, dict] | None:
        """One office-log record as a thread id plus a client message."""
        to = str(record.get("to") or "").strip()
        sender = str(record.get("from") or OWNER).strip()
        text = str(record.get("text") or "")
        message_id = str(record.get("id") or "")
        if not to or not message_id or to == "*":
            return None  # a broadcast belongs to no single thread
        to = self._canonical(to)
        if sender not in OWNER_SENDERS and sender not in SYSTEM_SENDERS:
            sender = self._canonical(sender)

        group = str(record.get("group") or "").strip()
        if group:
            # Every copy of one fanned-out group message carries the same
            # `group_id` and the same `ts`, so the group's thread shows the one
            # message that was sent rather than one per member.
            thread_id = groups.thread_id(group)
            author = OWNER if sender in TYPED_BY_HIM else sender
            message_id = str(record.get("group_id") or message_id)
        elif sender in OWNER_SENDERS or sender in SYSTEM_SENDERS:
            thread_id = direct_id(to)
            author = OWNER if sender in TYPED_BY_HIM else sender
        elif to in OWNER_SENDERS:
            thread_id, author = direct_id(sender), sender
        else:
            thread_id, author = peer_id(sender, to), sender

        ts = _num(record.get("ts")) or 0.0
        if author == OWNER:
            role = "owner"
        elif author in SYSTEM_SENDERS:
            role = "system"
        else:
            role = "agent"
        channel = str(record.get("channel") or "text")
        if channel not in CHANNELS:
            channel = "text"
        if channel == "voice" and text.endswith(VOICE_NOTE):
            text = text[:-len(VOICE_NOTE)]
        note = record.get("voice_note")
        if isinstance(note, str) and note and role == "owner":
            if text.endswith(RECORDED_NOTE):
                text = text[:-len(RECORDED_NOTE)]
        else:
            note = ""
        # K4: a desk's `ask`. The card as it was asked rides on the record;
        # `_stamp` overlays its current state from `decisions.json` per read.
        card = record.get("decision")
        decision = None
        if (record.get("kind") == "decision" and isinstance(card, dict)
                and card.get("id") and role == "agent"):
            decision = dict(card)
        extra = {"decision": decision} if decision else {}
        if note:
            extra["voice_note"] = {"id": note, "url": f"/v1/voice-notes/{note}"}
        quoted = record.get("reply_to")
        if isinstance(quoted, dict) and quoted.get("id"):
            # Only present on a reply, like `decision`: a client reads a
            # missing key as "answers nothing".
            extra["reply_to"] = {"id": str(quoted["id"]),
                                 "author": str(quoted.get("author") or ""),
                                 "excerpt": str(quoted.get("excerpt") or "")}
        return thread_id, {
            "id": message_id,
            "cursor": _cursor(ts, message_id),
            "thread_id": thread_id,
            "author": author,
            "role": role,
            "channel": channel,
            "kind": "decision" if decision else "text",
            **extra,
            "ts": ts,
            "text": text,
            "attachments": attachments(text),
            "via": "office",
            # 0 unless `office.send` had to cut this one, in which case it is
            # the length the sender wrote. Always present, so a client reads a
            # number rather than having to know the key can be missing -- and
            # a cut is a fact about the message, not an absence of one.
            "truncated": int(_num(record.get("truncated")) or 0),
        }

    def _message_from_edge(self, edge: dict) -> tuple[str, dict] | None:
        sender = self._canonical(str((edge.get("from") or {}).get("name") or "").strip())
        target = self._canonical(str((edge.get("to") or {}).get("name") or "").strip())
        if not sender or not target or sender == target:
            return None
        text = str(edge.get("text") or "")
        ts = _num(edge.get("ts")) or 0.0
        message_id = str(edge.get("id") or "")
        if not message_id:
            return None
        thread_id = peer_id(sender, target)
        return thread_id, {
            "id": message_id,
            "cursor": _cursor(ts, message_id),
            "thread_id": thread_id,
            "author": sender,
            "role": "agent",
            "channel": "text",
            "kind": "text",
            "ts": ts,
            "text": text,
            "attachments": attachments(text),
            "via": "transcript",
            "truncated": int(_num(edge.get("truncated")) or 0),
        }

    @staticmethod
    def _place(store: dict[str, list[dict]], key: str, message: dict) -> None:
        """One message into one bucket, in cursor order, at most once.

        Idempotent on `id`: `_absorb_renames` re-inserts a whole bucket after a
        rename, and the relay is fed from the same call, so a second `_place`
        of the same record has to be a no-op rather than a duplicate.
        """
        bucket = store.setdefault(key, [])
        keys = _keys(bucket)
        index = bisect.bisect_left(keys, message["cursor"])
        if index < len(bucket) and bucket[index]["id"] == message["id"]:
            return  # already folded in
        bucket.insert(index, message)
        if len(bucket) > MAX_THREAD_MESSAGES:
            del bucket[: len(bucket) - MAX_THREAD_MESSAGES]

    def _insert(self, thread_id: str, message: dict) -> None:
        self._place(self._logged, thread_id, message)
        self._fan(self._relay, thread_id, message)

    @staticmethod
    def _fan(store: dict[str, list[dict]], thread_id: str,
             message: dict) -> None:
        """Mirror a peer message into the owner-facing thread of BOTH parties.

        Both, and only both. A desk's chat carries what that desk itself sent
        and received -- never what two other desks said to each other, even
        when they both report to it. That fence is what keeps the owner's
        conversation with a manager readable on a board of eight: a status
        sweep across three reports is six inline lines, not the subtree's
        whole traffic.
        """
        kind, participants = _parse_thread_id(thread_id)
        if kind != "peer":
            return
        for name in participants:
            Surface._place(store, direct_id(name), message)

    def messages_for(self, thread_id: str) -> list[dict]:
        """Every message in a thread, oldest first.

        Two sources can feed one thread: an agent-to-agent message can be
        *queued* through the office and also *observed* in a transcript. They
        are merged, not concatenated, so the order stays total.
        """
        parts = [self._logged.get(thread_id) or [],
                 self._edged.get(thread_id) or []]
        if thread_id.startswith("direct:"):
            # ...and a third and fourth: the relay. A desk's own thread also
            # carries what it said to another desk and what came back, so the
            # owner reads the dispatch and the reply where he already is.
            parts += [self._relay.get(thread_id) or [],
                      self._edge_relay.get(thread_id) or []]
        parts = [p for p in parts if p]
        if len(parts) == 1:
            return parts[0]
        if not parts:
            return []
        merged: list[dict] = []
        seen: set[str] = set()
        for part in parts:
            for message in part:
                if message["id"] not in seen:
                    seen.add(message["id"])
                    merged.append(message)
        merged.sort(key=lambda m: m["cursor"])
        return merged

    def refresh(self) -> list[dict]:
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> list[dict]:
        """Fold one collector tick into the client view. Returns SSE events.

        O(new records) on disk. The comms rebuild is O(edges), and `CommsIndex`
        caps itself at `MAX_EDGES`, so neither term grows with history on disk.
        """
        before_agents = {a["name"]: (a["state"], a["unread"], a["blocked"])
                         for a in self._agents}
        before_heads = {tid: t["last_cursor"] for tid, t in self._threads.items()}
        before_looks = {a["name"]: _look_of(a) for a in self._agents}

        state = self._snapshot() or {}
        cards = [c for c in (state.get("sessions") or []) if isinstance(c, dict)]
        desks = roster.load_roster(self.roster_path)
        self._absorb_renames(self._sidecar(
            "aliases", onboard.aliases_path(self.roster_path),
            onboard.load_aliases))
        self._prefs = self._load_prefs()

        self._bosses = {d.name: d.reports_to for d in desks}
        self._tests = {d.name for d in desks if owner_alerts.is_test_desk(
            name=d.name, label=d.label, test=d.test)}
        fresh_alerts = len(self._alerts)
        for record in self._log.poll(self.messages_path):
            label = self._classifier.classify(record)
            folded = self._message_from_record(record)
            if folded is not None:
                self._insert(*folded)
                if label is not None:
                    self._note_alert(label, record, *folded)

        known = {d.name for d in desks}
        known.update(str(c.get("name")) for c in cards if c.get("name"))

        edged: dict[str, list[dict]] = {}
        relay: dict[str, list[dict]] = {}
        for edge in self.comms.edges(comms_mod.build_directory(cards)):
            folded = self._message_from_edge(edge)
            if folded is None:
                continue
            thread_id, message = folded
            _kind, participants = _parse_thread_id(thread_id)
            if not known.intersection(participants):
                continue  # traffic between two strangers is not our roster's
            edged.setdefault(thread_id, []).append(message)
            self._fan(relay, thread_id, message)
        for bucket in edged.values():
            bucket.sort(key=lambda m: m["cursor"])
        self._edged = edged
        self._edge_relay = relay

        self._rebuild(desks, cards)
        try:
            self._decide_pushes(self._alerts[fresh_alerts:])
        except Exception as exc:  # a push decision must never cost the board
            _LOG.warning("push decision failed: %r", exc)
        held, self._held = self._held, []
        self._note_look_changes(before_looks)
        return (self._events(before_agents, before_heads) + held
                + self._decision_events())

    # -- what may notify him -------------------------------------------------

    #: A page of alerts. A phone asks after a night asleep; this bounds it.
    ALERTS_MAX = 50
    #: A card logged this recently before a restart may never have been
    #: offered (the deck was down when it was raised): offer it at boot.
    BOOT_GRACE = 900.0

    def _note_alert(self, label: str, record: dict, thread_id: str,
                    message: dict) -> None:
        """One labelled office record, as the alert every channel reads."""
        desk = str(message.get("author") or "")
        card = message.get("decision") or {}
        is_card = message.get("kind") == "decision" and card.get("id")
        alert_id = (f"decision:{card['id']}" if is_card
                    else f"msg:{message['id']}")
        if alert_id in self._alert_ids:
            return
        self._alert_ids.add(alert_id)
        text = card.get("prompt") if is_card else alert_body(
            str(message.get("text") or ""), message.get("attachments") or [])
        alert = {
            "id": alert_id,
            "kind": label,
            "source": "decision" if is_card else "message",
            "agent": desk,
            "thread_id": thread_id,
            "card_id": str(card.get("id") or "") if is_card else "",
            "message_id": message["id"],
            "urgent": record.get("urgent") is True,
            "body": owner_alerts.one_line(asking.redact(str(text or ""))),
            "ts": message["ts"],
            "cursor": message["cursor"],
        }
        # Same ordered, capped insert every thread bucket uses.
        self._place({"all": self._alerts}, "all", alert)

    def _who(self, desk: str) -> str:
        for row in self._agents:
            if row.get("name") == desk:
                return str(row.get("label") or "").strip() or desk
        return desk

    def _live_alerts(self) -> list[dict]:
        """Approvals and handoffs: state, not log. Only while still waiting."""
        out = []
        for row in [self._approval(a) for a in asking.pending(self.asks_path)]:
            desk = str(row.get("agent") or "")
            what = describe_tool_short(str(row.get("tool") or ""))
            subject = str(row.get("subject") or "")
            out.append(self._live("approval", row, desk,
                                  f"{what}: {subject}" if subject else what))
        for row in [self._handoff_row(h)
                    for h in handoff.waiting(self.handoffs_path)]:
            desk = str(row.get("agent") or "")
            out.append(self._live("handoff", row, desk,
                                  str(row.get("needs") or "")))
        for policy in standing.load(standing.DEFAULT_PATH):
            if policy.status == "proposed":
                out.append(self._standing_alert(policy))
        return out

    def _standing_alert(self, policy: "standing.Policy") -> dict:
        """A desk's PROPOSED standing approval: his yes or no, like a card.

        Read from `standing.json` (the deck tool and `POST
        /v1/standing-approvals` both write there), so it is live state: it
        stops being wanted the moment he approves or revokes it. Filed under
        the desk that proposed it; one the owner filed as proposed is filed
        under the desk it is for."""
        by = policy.created_by
        desk = by if by and by not in office.OWNER_SENDERS and by != "owner" \
            else ("" if policy.desk == "*" else policy.desk)
        alert = self._live("standing", {"id": policy.id,
                                        "ts": policy.created_at}, desk,
                           standing.summary(policy))
        wire = policy.wire()
        return {**alert, "id": f"standing:{policy.id}",
                "policy_id": policy.id,
                "standing": {k: wire[k] for k in
                             ("desk", "kind", "tool", "pattern", "limits",
                              "expires_at", "note", "created_by")},
                "approve_route": f"/v1/standing-approvals/{policy.id}/approve",
                "revoke_route": f"/v1/standing-approvals/{policy.id}"}

    def _live(self, source: str, row: dict, desk: str, body: str) -> dict:
        ts = _num(row.get("ts")) or 0.0
        card_id = str(row.get("id") or "")
        return {
            "id": f"{'ask' if source == 'approval' else 'handoff'}:{card_id}",
            "kind": owner_alerts.NEEDS_YOU,
            "source": source,
            "agent": desk,
            "thread_id": direct_id(self._canonical(desk)) if desk else "",
            "card_id": card_id,
            "message_id": "",
            "urgent": False,
            "body": owner_alerts.one_line(body),
            "ts": ts,
            "cursor": _cursor(ts, f"{source}-{card_id}"),
        }

    @property
    def pushes(self) -> push_policy.PushPolicy:
        if self._push is None:
            quiet = os.environ.get("DECK_QUIET_HOURS")
            if quiet is None:
                try:
                    from . import deckconfig
                    quiet = deckconfig.load().notify.quiet_hours
                except Exception:
                    quiet = ""
            self._push = push_policy.PushPolicy(
                path=self.roster_path.parent / "pushes.json",
                quiet=push_policy.quiet_window(quiet))
        return self._push

    def seen(self) -> None:
        """He is looking: a read, a message from him, an app in front."""
        self.pushes.seen(at=self._clock())

    def _decide_pushes(self, logged: list[dict]) -> None:
        """Offer what is new about him; let the policy decide what buzzes."""
        policy = self.pushes
        now = self._clock()
        live = self._live_alerts()
        if not self._booted:
            # History is not news. What is still waiting is: a card already
            # offered before a restart is remembered on disk (and so is its
            # hold), and a card raised while the deck was down -- a live one,
            # or a decision logged in the last `BOOT_GRACE` -- is offered now.
            # MEASURED 2026-10-01: 54 restarts, 5 cards lost to them.
            recent = [a for a in logged if a["kind"] == owner_alerts.NEEDS_YOU
                      and now - float(a.get("ts") or 0) < self.BOOT_GRACE]
            for alert in recent + live:
                if alert["agent"] not in self._tests:
                    policy.offer({**alert, "title": self._title(alert),
                                  "who": self._who(alert["agent"])},
                                 at=now - policy.settle)
            self._booted = True
            logged = []
        for alert in logged + live:
            if alert["agent"] in self._tests:
                continue
            policy.offer({**alert, "title": self._title(alert),
                          "who": self._who(alert["agent"])}, at=now)
        pending = {(a["source"], a["card_id"]) for a in live}
        cards = self._decision_cards()
        unread = {row.get("name"): row.get("unread") or 0 for row in self._agents}

        def wanted(alert: dict) -> bool:
            if alert["source"] in ("approval", "handoff", "standing"):
                return (alert["source"], alert["card_id"]) in pending
            if alert["source"] == "decision":
                state = (cards.get(alert["card_id"]) or {}).get("state", "open")
                return state == "open"
            return unread.get(alert["agent"], 1) > 0

        policy.tick(now, wanted)

    def _rings(self) -> list[dict]:
        """Desks calling him now (`server/ringing.py`). Never a test desk."""
        return [r for r in ringing.live(self._clock())
                if r["agent"] not in self._tests]

    def push_rings(self) -> list[dict]:
        """Each live ring pushed once, now (`PushPolicy.ring`): the Mac's
        notification, the phone's and ntfy all read it from `owner_alerts`."""
        out = []
        for ring in self._rings():
            who = self._who(ring["agent"])
            alert = {"id": f"ring:{ring['id']}", "kind": owner_alerts.NEEDS_YOU, "source": "ring",
                     "agent": ring["agent"], "thread_id": ring["thread_id"],
                     "card_id": "", "message_id": "", "ring_id": ring["id"],
                     "urgent": True, "who": who, "ts": ring["created_at"],
                     "title": owner_alerts.one_line(
                         f"{who} is calling — {ring['reason']}"),
                     "body": "Tap to answer. It rings for "
                             f"{int(ringing.RING_SECONDS)} seconds."}
            push = self.pushes.ring(alert, at=self._clock())
            if push is not None:
                out.append(push)
        return out

    def owner_alerts(self, since: str | None, *, limit: int = ALERTS_MAX,
                     active: bool = False) -> dict:
        """The PUSHES, strictly after `since`: what every channel delivers.

        No `since` is a first poll: nothing, and a cursor at the head, so a
        phone that just installed the app is not buzzed with history.
        `active` is an app in front: he is looking, and nothing is pushed
        until he leaves (`push_policy`). `ntfy` says whether ntfy delivers
        these too, so the phone does not post the same push twice.
        """
        if active:
            self.seen()
        pushes = list(self.pushes.pushes)
        delivers = ntfy.target() is not None
        if since is None:
            head = max([p["cursor"] for p in pushes]
                       + [_cursor(self._clock(), "0")])
            return {"alerts": [], "next_since": head, "ntfy": delivers,
                    "ringing": self._rings()}
        fresh = [p for p in pushes if p["cursor"] > since][:limit]
        return {"alerts": fresh,
                "next_since": fresh[-1]["cursor"] if fresh else since,
                "ntfy": delivers, "ringing": self._rings()}

    def _title(self, alert: dict) -> str:
        who = self._who(alert["agent"]) or "A desk"
        if alert["kind"] == owner_alerts.NEEDS_YOU:
            return f"{who} needs you"
        return f"{who} (urgent)" if alert.get("urgent") else who

    def _refresh_holding(self) -> None:
        """`refresh` for a caller that does not publish (a GET, a write). Every
        frame is kept for the next publisher: a change is diffed once, so a
        frame dropped here is never sent -- a desk's line eaten by a poll, or
        his own sent line eaten by the send, until he reopened the chat."""
        with self._refresh_lock:
            fresh = self.refresh()  # takes `_held` itself: read it after
            self._held += fresh

    def _decision_events(self) -> list[dict]:
        """A `decision` frame per card that appeared or changed state (K4)."""
        cards = self._decision_cards()
        now = {i: (c.get("state"), c.get("answer")) for i, c in cards.items()}
        seen, self._decision_seen = self._decision_seen, now
        if seen is None:
            return []
        stamp = time.time()
        return [{"type": "decision", "ts": stamp,
                 "thread_id": direct_id(self._canonical(
                     str(cards[i].get("desk") or ""))),
                 "decision": decisions.public(cards[i])}
                for i, key in now.items() if seen.get(i) != key]

    def _rebuild(self, desks: list[roster.Desk], cards: list[dict]) -> None:
        rows = roster.occupancy(desks, cards)
        # `occupancy` joins a desk to its session's state, id and pid but not
        # to the rest of the card, so the live session is kept to hand for the
        # fields a desk row has no answer for (which project it is working in).
        seated = {str(c.get("name")): c for c in cards if c.get("name")}
        reports: dict[str, list[str]] = {}
        for desk in desks:
            if desk.reports_to:
                reports.setdefault(desk.reports_to, []).append(desk.name)

        threads: dict[str, dict] = {}
        by_agent: dict[str, list[str]] = {}

        # Every agent always has a direct thread, even an empty one: the client
        # opens a chat before there is anything in it.
        for row in rows:
            name = str(row.get("name") or "")
            if name:
                by_agent.setdefault(name, []).append(direct_id(name))

        for thread_id in set(self._logged) | set(self._edged):
            kind, participants = _parse_thread_id(thread_id)
            if kind == "group":
                # A group's thread belongs to the groups file, never to what is
                # still in the message log: a deleted group leaves no thread.
                continue
            for participant in participants:
                bucket = by_agent.setdefault(participant, [])
                if thread_id not in bucket:
                    bucket.append(thread_id)

        by_group = {groups.thread_id(g.name): g for g in
                    self._sidecar("groups", self.groups_path, groups.load_groups)}
        for thread_id, group in by_group.items():
            for member in group.members:
                bucket = by_agent.setdefault(member, [])
                if thread_id not in bucket:
                    bucket.append(thread_id)

        for thread_id in {t for ids in by_agent.values() for t in ids}:
            kind, participants = _parse_thread_id(thread_id)
            if kind == "group":
                group = by_group.get(thread_id)
                title = participants[0]
                members = [OWNER, *(group.members if group else ())]
            elif kind == "direct":
                title, members = participants[0], [OWNER, participants[0]]
            else:
                title = f"{participants[0]} ⇄ {participants[1]}"
                members = list(participants)
            messages = self.messages_for(thread_id)
            last = messages[-1] if messages else None
            threads[thread_id] = {
                "id": thread_id,
                "kind": kind,
                "title": title,
                "participants": members,
                # The client must not have to infer this. A peer thread is a
                # transcript of two agents talking; there is no frame that
                # inserts a third party into it, so it is view-only, full stop.
                "read_only": kind == "peer",
                "message_count": len(messages),
                "last_ts": last["ts"] if last else 0.0,
                "last_cursor": last["cursor"] if last else "",
                # Set here rather than from the winning agent row: a relayed
                # message is now the newest thing in two threads at the same
                # cursor, and whichever thread the agent row picked used to be
                # the only one that got a preview line at all.
                "preview": _one_line(last["text"]) if last else "",
            }

        self._threads = threads

        agents: list[dict] = []
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            pref = self.pref(name)
            read_cursor = str(pref.get("read_cursor") or "")
            thread_ids = by_agent.get(name, [direct_id(name)])

            newest = None
            # Counted by message id, not by thread. One peer message is now in
            # three of this desk's threads -- the peer transcript and both
            # parties' own chats -- and adding the buckets up would badge every
            # dispatch two and three times over.
            unread_ids: set[str] = set()
            for thread_id in thread_ids:
                messages = self.messages_for(thread_id)
                if not messages:
                    continue
                if newest is None or messages[-1]["cursor"] > newest[0]["cursor"]:
                    newest = (messages[-1], thread_id)
                start = bisect.bisect_right(_keys(messages), read_cursor)
                unread_ids.update(m["id"] for m in messages[start:]
                                  if m["role"] == "agent")
            unread = len(unread_ids)

            preview = ""
            last_at = _num(row.get("state_since"))
            # WHO spoke last, which `state` structurally cannot say. The owner's
            # complaint: the line under his conversation reads "Idle -- its
            # session is up and nothing is running" the instant the agent
            # finishes answering him. It is true and it is useless, because
            # `IDLE` covers three different situations -- it answered you, you
            # asked it something and it has not come back, and nobody has ever
            # spoken to it -- and the row published no way to tell them apart:
            # `last_activity_at` is a timestamp with no author and `preview` is
            # text with no author. Read from the CONVERSATION rather than from
            # the session, so a desk that answered him and was then re-seated
            # does not go blank the moment its brain is replaced.
            spoke_last = ""
            if newest is not None:
                message, _thread_id = newest
                # From the message's OWN thread, never the bucket it was read
                # out of: a relayed line found in `direct:<desk>` is still peer
                # traffic and still reads "Messaged X: ..." in the sidebar.
                preview = self._preview(name, message["thread_id"], message)
                last_at = message["ts"]
                # A schedule or the deck speaking is still his side of the
                # conversation, so the closed `agent|owner|""` set stays closed.
                spoke_last = ("agent" if message["role"] == "agent"
                              else "owner")

            agents.append({
                "name": name,
                "label": str(row.get("label") or ""),
                # What this desk is for, in a sentence the owner reads under
                # its name. `""` when nobody has written one.
                "description": str(row.get("description") or ""),
                "section": str(pref.get("section") or DEFAULT_SECTION),
                "avatar": pref.get("avatar"),
                # K6: how the desk sounds and the character it is drawn as.
                # None means the client derives both from the name.
                "voice": row.get("voice"),
                "avatar_look": row.get("avatar_look"),
                "state": self._state_of(name, row),
                "unread": unread,
                "last_activity_at": last_at,
                # `"agent"`, `"owner"`, or `""` when this desk has never been
                # in a conversation. Always present, so a client reads a value
                # rather than having to know the key can be missing.
                "last_activity_by": spoke_last,
                "preview": preview,
                "thread_id": direct_id(name),
                "pinned": bool(pref.get("pinned", False)),
                "notifications": bool(pref.get("notifications", True)),
                "desk": bool(row.get("desk")),
                # Where this desk works. For an interviewed hire this is the
                # workspace the deck allocated it, and the app shows it rather
                # than guessing at a project name.
                "cwd": str(row.get("cwd") or ""),
                "project": str(row.get("project")
                              or (seated.get(name) or {}).get("project") or ""),
                "session_id": row.get("session_id"),
                # Which Claude account the desk belongs to, and which one its
                # live session is on (they differ only mid-move). The default
                # account is named, never "", so a client compares ids.
                "account": str(row.get("account") or "")
                           or (str((seated.get(name) or {}).get("account") or "")
                               if not row.get("desk") else "") or "main",
                "running_account": ((seated.get(name) or {}).get("account") or "main")
                                   if row.get("session_id") else None,
                # Why this desk is dark, when the deck knows. An OFFLINE that
                # is stuck on a dialog and an OFFLINE that has simply not been
                # started look identical, and that is what let a whole build
                # cycle ship a hire that could never begin.
                "blocked": self._blocked(row, pref, seated.get(name)),
                "boss": row.get("reports_to"),
                "reports": sorted(reports.get(name, [])),
            })

        self._agents = agents
        # Everything `delivery_of` needs about a recipient, keyed by name, so
        # stamping a page of messages costs one dict lookup per message rather
        # than a scan of the roster per message.
        self._reach = {a["name"]: (a["state"], a["session_id"], a["blocked"])
                       for a in agents}

    def _state_of(self, name: str, row: dict) -> str:
        """The card state, with `OFFLINE` split in two (K3): a desk with no
        session that can still be resumed is `ASLEEP`, not `OFFLINE`."""
        state = str(row.get("state") or "IDLE")
        if state == "OFFLINE" and not row.get("session_id"):
            try:
                if self._asleep(name):
                    return "ASLEEP"
            except Exception:
                pass  # a broken probe must not blank the sidebar
        return state

    def _stamp(self, thread_id: str, messages: list[dict]) -> list[dict]:
        """`messages` with `delivery` on each. COPIES, never the cached dicts.

        Applied where a client READS -- `page`, `send`, and the `message` SSE
        frame -- and deliberately not inside `messages_for`, which the refresh
        loop calls once per thread per tick to build previews and unread
        counts. Stamping there would put an O(every message on disk) walk on a
        1 Hz timer and undo `test_the_sidebar_cost_does_not_scale_with_message_history`.

        `None` for anything that is not him speaking to one desk:

        * a DESK's own line -- he is reading it, there is nothing to report;
        * a PEER line relayed into his thread -- it was never his to deliver;
        * a GROUP message. `_message_from_record` folds one fan-out into a
          single message keyed by `group_id`, so the per-member ids that carry
          the acks are not in this bucket to be asked about. The group's own
          POST still answers `delivery_note` with `reached` and `waiting`.

        The key is always present, so a client reads a value rather than having
        to know a key can be missing.
        """
        kind, participants = _parse_thread_id(thread_id)
        target = participants[0] if kind == "direct" else ""
        state, session_id, blocked = self._reach.get(target, ("", None, None))
        acked = self._log.acked
        cards = (self._decision_cards()
                 if any(m.get("kind") == "decision" for m in messages) else {})
        return [
            {**message,
             **({"decision": decisions.public(cards[message["decision"]["id"]])}
                if message.get("kind") == "decision"
                and message["decision"]["id"] in cards else {}),
             "delivery": delivery_of(target, acked=message["id"] in acked,
                                     state=state, session_id=session_id,
                                     blocked=blocked)
             if target and message["role"] in ("owner", "system") else None}
            for message in messages
        ]

    def _blocked(self, row: dict, pref: dict, card: dict | None) -> dict | None:
        """Why this desk cannot move, or None. Two causes, one field.

        **OFFLINE** is the stored cause: the session never opened, or opened
        onto Claude Code's trust dialog. `_start` writes that reason to the
        prefs file and clears it on a clean start.

        **Seated and frozen** is computed live, and this is the half that was
        missing. A session at `attention.kind == "permission_prompt"` with no
        live question on the board is stopped at a dialog in its own Terminal
        window: the deck cannot relay it, the app has nothing to tap, and the
        row said nothing at all. That covers every desk outside the permission
        hooks -- older desks, and the owner's own windows.

        Computed, never stored, because it has to clear the instant the modal
        does. A stored copy is the stale-reason failure the OFFLINE half
        already had to fix once.

        Only `permission_prompt`. `agent_needs_input` is the other attention
        kind and never has an ask row by construction, so this rule would fire
        on every question a desk puts to its owner -- and the deck holds no
        evidence those are unanswerable.
        """
        if str(row.get("state") or "") == "OFFLINE":
            stored = pref.get("blocked")
            # A trust block is stale the moment the folder is trusted: only a
            # start clears it, and a desk woken by `wake` never passes through
            # one -- acme-lead read "Waiting for you" for a trusted folder.
            if (isinstance(stored, dict) and "trust" in str(stored.get("what"))
                    and row.get("cwd") and pretrust.is_trusted(row["cwd"])):
                return None
            return stored

        attention = (card or {}).get("attention") or {}
        if attention.get("kind") != "permission_prompt":
            return None
        if self._answerable(str(row.get("name") or ""),
                            str(row.get("cwd") or ""),
                            str(row.get("session_id") or "")):
            return None

        return {
            "what": "this desk is stopped on a permission dialog in its own "
                    "Terminal window, and no question for it reached the deck "
                    "— nothing in the app can answer it, so it has to be "
                    "answered at the keyboard",
            "reason": STUCK_ON_DIALOG,
            "detail": str(attention.get("message")
                          or "a dialog is open and no question for this desk "
                             "reached the deck"),
            "at": _num(attention.get("since")),
        }

    def _answerable(self, name: str, cwd: str, session_id: str = "") -> bool:
        """Is there a live question the owner can settle from the app?

        Matched by `ask_is_from`: the desk name, its session id, the `session
        <id>` form, OR its folder. A name-only match would call an answerable
        desk blocked -- and a FALSE block is the error that matters here. Both
        errors are possible; only one of them makes `blocked` untrustworthy, so
        the loose match is the right one.

        The session id is passed now rather than left to the folder match. The
        two failed together on exactly the shape that is common -- a hook that
        sends no desk name AND a desk whose folder is not where the tool call
        happened to be made.

        Read through `_sidecar`: `GET /v1/agents` runs on every app foreground
        and must not gain a file read per request.
        """
        try:
            live = self._sidecar("asks", self.asks_path, asking.pending)
        except Exception:  # pragma: no cover -- asking._load is already total
            return True  # cannot see the board; do not accuse a desk on a guess
        return any(ask_is_from(ask, name=name, cwd=cwd, session_id=session_id)
                   for ask in live)

    def _desk_for_ask(self, ask: asking.Ask | handoff.Handoff) -> str:
        """The DESK NAME this block belongs to, or "" when it cannot be said.

        `_answerable` read the other way round. Same predicate, so a desk that
        reads as answerable and a card that reads as unowned cannot both be
        true of one question.

        Identity before folder, in two passes: a name or a session id names
        exactly one desk, and a folder can be shared -- an interviewed hire and
        a desk pointed at the same project would otherwise resolve by roster
        order, which is not a fact about who asked.

        Empty rather than a guess when nothing matches. A card filed under a
        desk that never asked is worse than one he has to go and find, and the
        payload says which of the two he is looking at.
        """
        for by_folder in (False, True):
            for row in self._agents:
                name = str(row.get("name") or "")
                if not name:
                    continue
                if ask_is_from(
                    ask,
                    name="" if by_folder else name,
                    cwd=str(row.get("cwd") or "") if by_folder else "",
                    session_id="" if by_folder
                    else str(row.get("session_id") or ""),
                ):
                    return name
        return ""

    def _preview(self, name: str, thread_id: str, message: dict) -> str:
        """The one line under an agent's name in the sidebar.

        Most of what an agent does is talk to *another agent*, so the preview
        has to read from that traffic and say who it was with -- "Messaged
        Chief: ..." / "Message from Seeker: ...". A preview that only covered
        the owner's own chat would be blank for the busiest desks on the board.
        """
        kind, participants = _parse_thread_id(thread_id)
        body = _one_line(message["text"])
        if kind == "direct":
            if message["role"] == "system":
                return _one_line(f"{message['author'].capitalize()}: {body}")
            return f"You: {body}" if message["author"] == OWNER else body
        if kind == "group":
            return _one_line(f"{participants[0]}: {body}")
        other = next((p for p in participants if p != name), participants[0])
        if message["author"] == name:
            return _one_line(f"Messaged {other}: {body}")
        return _one_line(f"Message from {message['author']}: {body}")

    # -- events --------------------------------------------------------------

    def _note_look_changes(self, before: dict) -> None:
        """A desk that restyled itself (`set_my_look` writes the roster from
        another process) is announced as a same-name `agent_renamed`: the row is
        swapped in place on the Mac and the phone refetches, no new frame type."""
        for agent in self._agents:
            was = before.get(agent["name"])
            if was is not None and was != _look_of(agent):
                self._renamed.append((agent["name"], agent["name"]))

    def _events(self, before_agents: dict, before_heads: dict) -> list[dict]:
        now = time.time()
        events: list[dict] = []
        for agent in self._agents:
            was = before_agents.get(agent["name"])
            if was is None:
                continue
            # `blocked` is diffed in its own right, not read off a `state`
            # change: a desk can go from an answerable dialog to an
            # unanswerable one (or back) with `state` sitting at NEEDS_YOU
            # the whole time, because only whether the board holds a
            # matching pending ask moved. Keying this off `state` alone
            # would drop that transition on the floor. Carrying `blocked`
            # on the frame -- always, including `None` -- is what lets an
            # already-connected client both learn a reason and learn its
            # retraction without a refetch or a reconnect.
            if was[0] != agent["state"] or was[2] != agent["blocked"]:
                events.append({"type": "agent_state", "ts": now,
                               "name": agent["name"], "state": agent["state"],
                               "blocked": agent["blocked"]})
            if was[1] != agent["unread"]:
                events.append({"type": "unread", "ts": now,
                               "name": agent["name"], "unread": agent["unread"]})
        for thread_id, thread in self._threads.items():
            head = before_heads.get(thread_id)
            if head is None or not thread["last_cursor"] or thread["last_cursor"] <= head:
                continue
            # Stamped like a page: a client that draws the thread from the
            # stream and a client that draws it from `GET .../messages` must
            # see the same message, or the tick a message arrives on is the one
            # tick it has no state.
            fresh = [m for m in self.messages_for(thread_id)
                     if m["cursor"] > head]
            for message in self._stamp(thread_id, fresh):
                events.append({"type": "message", "ts": now,
                               "thread_id": thread_id,
                               "read_only": thread["read_only"],
                               "message": message})
        # The whole row, not just the new name: the sidebar swaps the row in
        # place rather than refetching, and a partial one is a flicker or a
        # duplicate entry for a desk that only ever existed once.
        for old, new in self._renamed:
            row = next((a for a in self._agents if a["name"] == new), None)
            if row is not None:
                events.append({"type": "agent_renamed", "ts": now,
                               "old_name": old, "agent": row})
        self._renamed = []
        return events

    # -- reads ---------------------------------------------------------------

    def agents(self) -> list[dict]:
        return self._agents

    def agent(self, name: str) -> dict:
        found = next((a for a in self._agents if a["name"] == name), None)
        if found is None:
            raise Refused(404, "unknown_agent", f"no agent named {name!r}")
        return found

    def threads(self) -> list[dict]:
        return sorted(self._threads.values(),
                      key=lambda t: (t["last_ts"], t["id"]), reverse=True)

    def thread(self, thread_id: str) -> dict:
        found = self._threads.get(thread_id)
        if found is None:
            raise Refused(404, "unknown_thread", f"no thread {thread_id!r}")
        return found

    def page(self, thread_id: str, *, since: str | None, before: str | None,
             limit: int) -> dict:
        """One page, oldest-first, addressed by cursor and never by offset.

        `since` walks forward (strictly newer), `before` walks backward
        (strictly older), neither inclusive. With no cursor a client gets the
        newest `limit` messages, which is what opening a chat wants.
        """
        thread_id = self._canonical_thread(thread_id)
        thread = self.thread(thread_id)
        messages = self.messages_for(thread_id)
        keys = _keys(messages)

        if since is not None:
            start = bisect.bisect_right(keys, since)
            window = messages[start:start + limit]
            has_before = start > 0
            has_after = start + limit < len(messages)
        elif before is not None:
            end = bisect.bisect_left(keys, before)
            start = max(0, end - limit)
            window = messages[start:end]
            has_before = start > 0
            has_after = end < len(messages)
        else:
            start = max(0, len(messages) - limit)
            window = messages[start:]
            has_before = start > 0
            has_after = False

        return {
            "thread_id": thread_id,
            "kind": thread["kind"],
            "read_only": thread["read_only"],
            "participants": thread["participants"],
            "messages": self._stamp(thread_id, window),
            "has_more_before": has_before,
            "has_more_after": has_after,
            # Hand the client back the two cursors it needs next, so it never
            # has to know how a cursor is built.
            "next_since": window[-1]["cursor"] if window else (since or ""),
            "next_before": window[0]["cursor"] if window else (before or ""),
        }

    # -- writes --------------------------------------------------------------

    def send(self, thread_id: str, text: str, *, sender=OWNER,
             channel="text", call_id="", skips_decisions: bool = True,
             voice_note: str = "", reply_to=None) -> dict:
        """Say something into a thread, as `sender` (K2).

        `sender` is `OWNER` (or its alias "owner") or `engineer` and nothing else: the routes the deck
        itself speaks through (`routine`, `deck`) never come in over HTTP.
        `channel: "voice"` needs a `call_id` and is framed for the desk as
        spoken (K6).
        """
        if isinstance(sender, str) and sender in OWNER_ALIASES:
            sender = OWNER
        if sender == OWNER:
            self.seen()  # he is typing in an app: hold pushes
        if not isinstance(sender, str) or sender not in POST_SENDERS:
            raise Refused(400, "bad_sender",
                          f"`as` must be \"{OWNER}\" or \"engineer\"")
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise Refused(400, "bad_channel",
                          "`channel` must be \"text\" or \"voice\"")
        call_id = str(call_id or "").strip()
        if channel == "voice" and not call_id:
            raise Refused(400, "missing_call_id",
                          "a voice message needs the `call_id` of its call")
        thread_id = self._canonical_thread(thread_id)
        kind, participants = _parse_thread_id(thread_id)
        quote = self._quote(thread_id, reply_to) if kind != "peer" else None
        if kind == "group":
            if sender != OWNER or channel != "text":
                raise Refused(400, "bad_sender",
                              "a group takes only the owner's typed text")
            return self.send_to_group(participants[0], text, quote=quote)
        if kind == "peer":
            raise Refused(
                409, "thread_is_read_only",
                "this is a transcript of two agents talking; open the agent's "
                "own chat to say something",
            )
        target = participants[0]
        self.agent(target)  # 404s for a name that is not on the board
        if sender == office.ENGINEER and self._is_test_desk(target):
            # Written HERE, after the roster says so, and never accepted over
            # HTTP (`POST_SENDERS`): the stronger mark exists only on a desk
            # that exists to be probed. See `office.ENGINEER_TEST`.
            sender = office.ENGINEER_TEST
        body = (text or "").strip()
        if not body:
            raise Refused(400, "empty_text", "a message needs some text")

        if sender == OWNER and "(sent by the engineer" in body:
            # The caller forgot `as: "engineer"`: this will read as him.
            _LOG.warning("owner-token post claims to be from the engineer but "
                         "was sent as the owner; thread=%s", thread_id)
        extra = None
        if channel == "voice":
            body += VOICE_NOTE
            extra = {"channel": "voice", "call_id": call_id}
        elif voice_note and sender == OWNER:
            # A recorded message (not a call): an ordinary owner message the
            # desk is told was spoken, carrying the recording's id.
            body += RECORDED_NOTE
            extra = {"voice_note": voice_note}
        line = ""
        if quote:
            # Beside his words, never inside them: the record's `text` is what
            # the app draws back at him, and his bubble already shows the
            # quote. `reply_line` is what both delivery doors hand the desk.
            line = office.reply_line(quote["author"], quote["text"], to=target)
            extra = {**(extra or {}), "reply_to": _public_quote(quote),
                     "reply_line": line}
        result = office.send(target, body, sender=sender, extra=extra)
        if not result.get("ok"):
            raise Refused(500, "queue_failed", str(result.get("detail") or ""))
        if sender == OWNER and skips_decisions:
            # K4: he said something else, so any card still asking is moot.
            decisions.skip_open(target, path=self.decisions_path)

        # Asked BEFORE the delivery: a miss starts the wake, and a desk mid-
        # wake may no longer read as asleep by the time the answer is built.
        waking = [target] if self._is_asleep(target) else []
        delivered = False
        if self._deliver is not None:
            try:
                # `office.attribute`, not `body`. This fast path goes straight
                # down the desk's socket and never touches `hooks/cc-office.js`,
                # which is where the "who sent this" frame is otherwise added --
                # so without this the owner is himself only when his desk
                # happened to be too busy to have a socket free. The QUEUED
                # record above keeps `body` unmarked on purpose: that record is
                # what the app draws back at him in his own thread.
                delivered = bool(self._deliver(
                    target, office.attribute(body, sender, reply=line)))
            except Exception:
                delivered = False
            if delivered:
                office.ack(result["id"])

        self._refresh_holding()
        message = next(
            (m for m in self._stamp(thread_id, self.messages_for(thread_id))
             if m["id"] == result["id"]),
            None,
        )
        # `delivered` stays, unchanged, for every client already reading it.
        # `delivery` is the same fact said in a way a screen can show -- see
        # `delivery_note` for why the boolean alone was not enough.
        return {"ok": True, "delivered": delivered,
                "delivery": delivery_note([target] if delivered else [],
                                          [] if delivered else [target],
                                          waking=waking),
                "message": message}

    def _is_test_desk(self, name: str) -> bool:
        return any(d.name == name and d.test
                   for d in roster.load_roster(self.roster_path))

    def _is_asleep(self, name: str) -> bool:
        """`asleep(name)`, for a desk the board shows with no session. Never
        raises: a broken probe reads as awake, which is the old answer."""
        state = self._reach.get(name, ("",))[0]
        if state == "ASLEEP":
            return True
        if state not in DARK_STATES:
            return False
        try:
            return bool(self._asleep(name))
        except Exception:
            return False

    def answer_decision(self, decision_id: str, value) -> dict:
        """His tap on a decision card (K4): settle the card, then say his pick
        into the thread as his own message -- the same door his typing uses,
        so it is framed as him and a sleeping desk is woken to read it."""
        try:
            card = decisions.answer(decision_id, value, path=self.decisions_path)
        except decisions.DecisionError as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from exc
        # A store approval card (server/connectors.py): his "Install" tap is
        # what installs an unverified item a desk asked for. Never fatal.
        try:
            from . import connectors
            connectors.on_decision(card)
        except Exception:  # noqa: BLE001 - the answer still reaches the desk
            pass
        sent = self.send(direct_id(self._canonical(str(card.get("desk") or ""))),
                         str(card["answer"]), skips_decisions=False)
        return {"ok": True, "decision": decisions.public(card),
                "delivery": sent["delivery"], "message": sent["message"]}

    def _quote(self, thread_id: str, reply_to) -> dict | None:
        """The message `reply_to` names, read off THIS thread, or None.

        The author and the excerpt are the deck's own copy: a client says
        which message, never what it said -- a quote the desk is handed as
        context is not something a caller gets to write.
        """
        if reply_to is None or reply_to == "":
            return None
        if isinstance(reply_to, dict):
            reply_to = reply_to.get("id")
        if not isinstance(reply_to, str) or not reply_to.strip():
            raise Refused(400, "bad_reply_to",
                          "`reply_to` is a message id, or an object with one")
        wanted = reply_to.strip()
        quoted = next((m for m in self.messages_for(thread_id)
                       if m.get("id") == wanted), None)
        if quoted is None:
            raise Refused(404, "unknown_reply_to",
                          f"this thread holds no message {wanted!r} to reply to")
        return {"id": wanted, "author": str(quoted.get("author") or ""),
                "text": str(quoted.get("text") or "")}

    def send_to_group(self, name: str, text: str, *,
                      quote: dict | None = None) -> dict:
        """Fan one message out to every member of a group, exactly once each.

        Down `office.send`, the same queue a direct message uses -- a member
        with nobody at its desk gets this queued for its next turn instead of
        losing it, which is the entire reason a group is a fanout rather than a
        new kind of mailbox.
        """
        body = (text or "").strip()
        if not body:
            raise Refused(400, "empty_text", "a message needs some text")
        group = groups.find(groups.load_groups(self.groups_path), name)
        if group is None:
            raise Refused(404, "unknown_group", f"no group named {name!r}")

        # Resolved HERE, at send time, not when the group was written: the
        # stored member list keeps the names the group was created with, and a
        # member that has since named itself must still get its copy. Read off
        # the alias file as it stands right now (one stat, cached on mtime) so
        # a rename that landed since the last tick is already in force.
        aliases = self._sidecar("aliases", onboard.aliases_path(self.roster_path),
                                onboard.load_aliases)
        rows = groups.broadcast(
            group, body, sender=OWNER, deliver=self._deliver,
            resolve=(lambda name: onboard.resolve(aliases, name)) if aliases else None,
            quote=quote and {**_public_quote(quote), "text": quote["text"]},
        )
        failed = [r["to"] for r in rows if not r["ok"]]
        if failed:
            raise Refused(500, "queue_failed",
                          f"could not queue for {', '.join(failed)}")

        self._refresh_holding()
        thread_id = groups.thread_id(name)
        shared = rows[0]["group_id"]
        message = next((m for m in self.messages_for(thread_id)
                        if m["id"] == shared), None)
        reached = [r["to"] for r in rows if r["delivered"]]
        waiting = [r["to"] for r in rows if not r["delivered"]]
        return {
            "ok": True,
            "delivered": reached,
            "queued": waiting,
            # The same object the direct door returns, so a client renders the
            # outcome of a send once rather than once per endpoint.
            "delivery": delivery_note(reached, waiting),
            "message": message,
        }

    # -- groups --------------------------------------------------------------

    def group_rows(self) -> list[dict]:
        return [{"name": g.name, "members": list(g.members),
                 "thread_id": groups.thread_id(g.name),
                 "created_at": _num(g.created_at) or 0.0}
                for g in groups.load_groups(self.groups_path)]

    def create_group(self, payload: dict) -> dict:
        members = payload.get("members")
        with self._write_lock:
            try:
                group = groups.create(
                    self.groups_path,
                    name=str(payload.get("name") or ""),
                    members=list(members) if isinstance(members, list) else [],
                    known={d.name for d in roster.load_roster(self.roster_path)},
                )
            except groups.GroupError as exc:
                raise Refused(_GROUP_STATUS.get(exc.reason, 409),
                              exc.reason, exc.detail) from exc
        self._refresh_holding()
        return next(row for row in self.group_rows() if row["name"] == group.name)

    def delete_group(self, name: str) -> dict:
        with self._write_lock:
            try:
                groups.remove(self.groups_path, name)
            except groups.GroupError as exc:
                raise Refused(_GROUP_STATUS.get(exc.reason, 409),
                              exc.reason, exc.detail) from exc
        self._refresh_holding()
        return {"ok": True, "name": name}

    def mark_read(self, name: str, *, up_to: str | None,
                  cursor: str | None) -> dict:
        self.seen()  # a read is him looking: hold pushes (`push_policy`)
        agent = self.agent(name)
        target = cursor
        if target is None and up_to:
            for thread_id in self._thread_ids_for(name):
                for message in self.messages_for(thread_id):
                    if message["id"] == up_to:
                        target = message["cursor"]
                        break
                if target:
                    break
            if target is None:
                raise Refused(404, "unknown_message",
                              f"no message {up_to!r} in {name}'s threads")
        if target is None:
            # No id and no cursor means "everything I can currently see".
            target = max(
                (t["last_cursor"] for t in self._threads.values()
                 if name in t["participants"] and t["last_cursor"]),
                default="",
            )
        self.update_pref(name, {"read_cursor": target})
        self._rebuild(roster.load_roster(self.roster_path),
                      [c for c in (self._snapshot() or {}).get("sessions") or []
                       if isinstance(c, dict)])
        return {"ok": True, "name": name, "read_cursor": target,
                "unread": self.agent(name)["unread"], "was": agent["unread"]}

    def _thread_ids_for(self, name: str) -> list[str]:
        return [t["id"] for t in self._threads.values() if name in t["participants"]]

    def patch_agent(self, name: str, payload: dict) -> dict:
        if "reports_to" in payload or "boss" in payload:
            raise Refused(
                409, "reports_to_is_not_a_setting",
                "who a desk reports to is the org chart, not a preference; "
                "use the roster to change it",
            )
        self.agent(name)

        desk_patch: dict = {key: str(payload[key])
                            for key in ("label", "charter", "mission", "model")
                            if key in payload and payload[key] is not None}
        if "voice" in payload:
            desk_patch["voice"] = _voice(payload["voice"])
        if "description" in payload:
            desk_patch["description"] = _description(payload["description"])
        # K6 `avatar` as an OBJECT is the drawn character, stored as the
        # roster's `avatar_look`; as a string or null it is still the picture
        # preference below, which the installed app sends on every save.
        if isinstance(payload.get("avatar"), dict):
            desk_patch["avatar_look"] = _avatar_look(payload["avatar"])
        if "avatar_look" in payload:
            desk_patch["avatar_look"] = _avatar_look(payload["avatar_look"])
        if desk_patch:
            desks = roster.load_roster(self.roster_path)
            desk = next((d for d in desks if d.name == name), None)
            if desk is None:
                raise Refused(409, "not_a_desk",
                              f"{name!r} is a live session, not a roster desk")
            # The charter IS the mission (see server/hire.hire); keeping them
            # equal is what stops a desk's brief drifting from what its process
            # was actually started with.
            if "charter" in desk_patch and "mission" not in desk_patch:
                desk_patch["mission"] = desk_patch["charter"]
            roster.upsert(self.roster_path, replace(desk, **desk_patch))

        pref_patch: dict = {}
        for key in ("avatar", "section"):
            if key in payload and not isinstance(payload[key], dict):
                value = payload[key]
                pref_patch[key] = None if value is None else str(value)
        for key in ("notifications", "pinned"):
            if key in payload:
                pref_patch[key] = bool(payload[key])
        if pref_patch:
            self.update_pref(name, pref_patch)

        self._refresh_holding()
        return self.settings(name)

    def settings(self, name: str) -> dict:
        """The per-agent settings panel: avatar, Name, Title, Description."""
        agent = self.agent(name)
        desk = next((d for d in roster.load_roster(self.roster_path)
                     if d.name == name), None)
        return {
            "name": name,
            "label": agent["label"],
            "description": agent["description"],
            "charter": desk.charter if desk else "",
            "mission": desk.mission if desk else "",
            "avatar": agent["avatar"],
            # K6. None means "derive it from the name" on the client.
            "voice": desk.voice if desk else None,
            "avatar_look": desk.avatar_look if desk else None,
            "section": agent["section"],
            "boss": desk.reports_to if desk else None,
            "reports": agent["reports"],
            "notifications": agent["notifications"],
            "pinned": agent["pinned"],
            "state": agent["state"],
            # Carried through to the detail panel as well as the board: this is
            # the one screen a human opens when a desk looks dead.
            "blocked": agent["blocked"],
            "unread": agent["unread"],
            "thread_id": agent["thread_id"],
            "engine": desk.engine if desk else "",
            "cwd": desk.cwd if desk else "",
            "created_at": _num(desk.created_at) if desk else None,
            "is_desk": desk is not None,
        }

    # -- hiring and firing ---------------------------------------------------
    #
    # Every refusal below is `hire`'s own slug, passed through untouched. The
    # client shows a different sentence for each -- "that name is taken", "that
    # boss is already three deep", "eight sessions is the cap" -- and flattening
    # them into one "it didn't work" is what makes a UI useless.

    def desk(self, name: str) -> roster.Desk | None:
        return next((d for d in roster.load_roster(self.roster_path)
                     if d.name == name), None)

    def create_agent(self, payload: dict) -> dict:
        """Hire a desk. Returns the row `GET /v1/agents` shows for it.

        `reports_to` IS accepted here, and only here: naming a new desk's boss
        is what hiring means. Changing it afterwards is the org chart, not a
        setting, and `patch_agent` still refuses it.
        """
        name = _required(payload, "name")
        # STATED, not seated. `hire.hire` moves a seat where work cannot happen
        # into a workspace of the deck's own -- see `server.seat` -- so the two
        # are kept apart here and both are reported.
        stated = _required(payload, "cwd")
        engine = _required(payload, "engine")
        if engine not in ENGINES:
            raise Refused(400, "unknown_engine",
                          f"{engine!r} is not one of {', '.join(ENGINES)}")
        boss = payload.get("reports_to")
        cards = [c for c in (self._snapshot() or {}).get("sessions") or []
                 if isinstance(c, dict)]
        # MAX_LIVE caps the org, not the machine -- count seated desks, not
        # every Claude Code session this Mac happens to be running.
        live = roster.live_desks(roster.load_roster(self.roster_path), cards)

        with self._write_lock:
            try:
                desk = hire.hire(
                    self.roster_path,
                    name=name,
                    label=str(payload.get("label") or ""),
                    charter=str(payload.get("charter") or ""),
                    description=_description(payload.get("description")),
                    cwd=stated,
                    engine=engine,
                    reports_to=None if boss is None else str(boss),
                    model=str(payload.get("model") or ""),
                    live_count=live,
                    test=payload.get("test") is True,
                )
            except hire.HireError as exc:
                raise Refused(409, exc.reason, exc.detail) from exc

        self._refresh_holding()
        row = self.agent(desk.name)
        # **`row["cwd"]` is where the desk went, not what was posted.** MEASURED
        # on the box, 2026-09-07: four of his six desks were seated in `/tmp`,
        # which `pretrust` will not pre-accept, so each one stalled on Claude
        # Code's trust dialog the moment it started -- four cards reading
        # "Waiting for you" for a folder he never chose, and 13 pending
        # approvals from desks trying to work out where they were.
        #
        # Warning him about it was the previous repair and it was not enough: a
        # warning published beside a desk that is already dead is a task for
        # him, not a fix. `hire.hire` now moves a seat where no work can happen
        # into a workspace the deck allocates -- see `server.seat`.
        #
        # Still NOT a refusal, and a real repository is still left alone. That
        # was tried and it turned 41 tests red, correctly: a desk that works on
        # a real project has to be seated in it, and `pretrust` declining for
        # such a folder is a supported state -- auto-trusting a directory the
        # owner named is a decision the deck must not take for him.
        return row

    def seat_report(self, stated: str, landed: str) -> dict:
        """Where this desk was asked to sit, and where it actually sits.

        **Substitution must be said out loud.** `hire.hire` moves a seat where
        work cannot happen -- `/tmp` was the measured one -- into a workspace
        the deck allocates, because a desk that cannot start is worse than a
        desk in a folder nobody named. But a caller that asked for one place,
        got another, and is never told has no way to find its own agent's work.
        So `stated` is carried beside `cwd`, and `substituted` says plainly
        which of the two happened.

        Beside the row, not inside it: `POST /v1/agents` promises the row is
        byte-identical to the one `GET /v1/agents` returns, and a test holds it
        to that.
        """
        return seat.describe(self.roster_path, stated, landed).as_dict()

    def seat_warning(self, cwd: str) -> dict:
        """Whether a desk seated here can actually start. Beside the row, never
        inside it -- `POST /v1/agents` promises the row is byte-identical to the
        one `GET /v1/agents` returns, and a test holds it to that.

        Shaped like the `pretrust` block `POST .../start` already returns, so a
        client reads the same three keys in both places.
        """
        # OWNER RULING 2026-09-30, agents never dead-end: any existing folder
        # a desk is seated in is vouched for at start (`pretrust.seat_verdict`).
        trusted, reason, detail = pretrust.seat_verdict(cwd, self.roster_path)
        return {
            "ok": trusted,
            "reason": "already_trusted" if trusted else reason,
            "detail": cwd if trusted else (
                f"{detail}. This desk will stop on the trust dialog the first "
                "time it runs a tool; hire it into a project folder or into "
                f"{pretrust.workspaces_root(self.roster_path)}/<name>."),
        }

    def _placeholder_name(self) -> str:
        """A free name to hold the desk until the agent picks its own."""
        taken = {d.name for d in roster.load_roster(self.roster_path)}
        for _ in range(64):
            candidate = f"{PLACEHOLDER_PREFIX}-{secrets.token_hex(3)}"
            if candidate not in taken:
                return candidate
        raise Refused(409, "no_free_name", "could not allocate a placeholder name")

    # `_workspace` used to live here and allocate `workspaces/<name>` at 0700
    # for the interview door alone. `seat.workspace_for` does that now for every
    # door, called from `hire.hire`. Two allocators, one of which only some
    # hires went through, is how four desks ended up in `/tmp`.

    def interview_agent(self, payload: dict) -> dict:
        """The second door: say what you want, and the agent works out the rest.

        A provisional desk with a placeholder name, and a session seeded with
        `onboard.interview_prompt` -- which asks what the job actually is and
        then tells the new hire to name *itself* with a `YOS_DESK` line. The
        harvester reads that line back and `onboard.apply_patch` applies it, at
        which point the placeholder becomes the real name and this desk's
        thread, read cursor and org edge move with it.

        `POST /v1/agents` is untouched; some callers want to state everything.
        """
        engine = str(payload.get("engine") or "claude")
        if engine not in ENGINES:
            raise Refused(400, "unknown_engine",
                          f"{engine!r} is not one of {', '.join(ENGINES)}")
        boss = payload.get("reports_to")
        cards = [c for c in (self._snapshot() or {}).get("sessions") or []
                 if isinstance(c, dict)]
        # MAX_LIVE caps the org, not the machine -- count seated desks, not
        # every Claude Code session this Mac happens to be running.
        live = roster.live_desks(roster.load_roster(self.roster_path), cards)
        # Read before the desk exists: an invented mandate is worse than none,
        # because a new hire would go and act on it.
        opening = str(payload.get("role_hint") or "").strip()
        seed = onboard.interview_prompt(opening, self.mandate())

        stated = str(payload.get("cwd") or "").strip()
        with self._write_lock:
            name = self._placeholder_name()
            # No `or self._workspace(name)` here any more: `hire.hire` resolves
            # every seat, so an empty `cwd` and a scratch `cwd` land in the same
            # allocated workspace, by the same rule the other two doors get.
            try:
                desk = hire.hire(
                    self.roster_path,
                    name=name,
                    label=PROVISIONAL_LABEL,
                    charter=PROVISIONAL_CHARTER,
                    cwd=stated,
                    engine=engine,
                    reports_to=None if boss is None else str(boss),
                    model=str(payload.get("model") or ""),
                    live_count=live,
                )
            except hire.HireError as exc:
                raise Refused(409, exc.reason, exc.detail) from exc

        try:
            started = self._start(desk, seed=seed)
        except (spawn.SpawnError, ValueError) as exc:
            # Reversible. A desk nobody is sitting at, that the owner never
            # named and cannot be talked to, is litter on the board.
            with self._write_lock:
                hire.fire(self.roster_path, desk.name)
            raise Refused(409, getattr(exc, "reason", "unknown_engine"),
                          str(exc)) from exc

        self._record_opening(desk.name, opening)
        self._refresh_holding()
        return {"ok": True, "provisional": True, "name": desk.name,
                "thread_id": direct_id(desk.name),
                "pretrust": started["pretrust"],
                "seat": self.seat_report(stated, desk.cwd),
                "agent": self.agent(desk.name)}

    def _record_opening(self, name: str, opening: str) -> None:
        """Put the owner's first instruction in the desk's thread. Once.

        **The defect.** MEASURED on the box on 2026-09-06: desk
        `new-hire-82d9ab` held exactly two records, and the first was the agent
        asking `What do you want me to actually do with "what's going on across
        projec...` -- quoting words of his that were in no record anywhere. He
        typed them into the "+" door, `onboard.interview_prompt` built them
        into the seed, and `spawn.start(..., seed=...)` put them in front of
        the session. That is a delivery. Nothing on the path was a recording,
        so the opening line of every conversation started at this door was
        missing from the conversation.

        **Recorded AND acked, in the same breath.** The seed has already
        carried these words into the session; the record exists so the thread
        he reopens contains what he said. Leaving it unacked would put it back
        in front of the desk through `hooks/cc-office.js` on its first turn --
        now that that hook is actually installed -- and he would have said the
        same thing twice, once invisibly. The ack is what makes this a written
        record rather than a second delivery.

        **After the spawn, not before.** A `_start` that raises fires the desk
        again, and a message addressed to a desk that no longer exists is
        litter in `messages.jsonl` that no thread will ever show.

        Never raises. A hire must not fail because the transcript of it could
        not be written -- the desk is real, the session is up, and the worst
        case is the thread he sees now, which is the thread he saw before.
        """
        if not opening:
            return  # he opened the door and said nothing; there is no message
        try:
            queued = office.send(name, opening, sender=OWNER)
            if queued.get("ok"):
                office.ack(queued["id"])
        except OSError:
            pass

    def _start(self, desk, *, seed: str = "") -> dict:
        """The ONE way this surface puts a process at a desk.

        Both doors -- `POST /v1/agents/interview` and
        `POST /v1/agents/{name}/start` -- come through here, because they did
        not, and the second one skipped the workspace trust gate the first one
        closed. Measured: an agent hired `branch-scout`, the start call
        answered `{"ok": true, "detail": ""}`, and the window sat on Claude
        Code's trust dialog behind a row reading `state: OFFLINE, blocked:
        null` -- indistinguishable from a desk nobody had started.

        The vouching itself now lives in `spawn.start`, so a caller cannot
        skip it. What lives here is the half only this surface can do: putting
        the reason on the desk row. A failed vouch does not cancel the start --
        the session still opens, on the dialog -- it just stops being
        invisible. And a start that succeeds *clears* the reason, or a desk
        that now works goes on looking broken.

        `spawn.start`, never `spawn_terminal`: the channel is chosen there, so
        this door works on the Linux install too. It called `spawn_terminal`
        directly, which shells out to `osascript`, and that is why the box --
        deployed, authenticated, answering 200 -- had hired nobody, ever.
        """
        started = spawn.start(desk, roster_path=self.roster_path, seed=seed)
        vouched = started.get("pretrust") or {}
        self.update_pref(desk.name, {"blocked": None if vouched.get("ok") else {
            "what": "workspace trust was not pre-accepted; the session is "
                    "waiting on Claude Code's trust dialog",
            "reason": vouched.get("reason", ""),
            "detail": vouched.get("detail", ""),
            "at": time.time()}})
        return started

    def remove_agent(self, name: str) -> dict:
        with self._write_lock:
            if self.desk(name) is None:
                raise Refused(404, "unknown_agent", f"no desk named {name!r}")
            try:
                hire.fire(self.roster_path, name)
            except hire.HireError as exc:
                raise Refused(409, exc.reason, exc.detail) from exc
        # Firing a desk takes its machinery with it. Only after the roster
        # accepted the fire (a refused fire must touch nothing). Transcripts and
        # the container's home directory are left in place on purpose.
        for job in spawn.jobs_of_desk(name):
            spawn.stop_job(job)
        sandbox.stop(name)  # `docker rm --force deck-desk-<name>`; never raises
        self._refresh_holding()
        return {"ok": True, "name": name}

    def start_agent(self, name: str) -> dict:
        """Put a process at this desk. OPENS A REAL TERMINAL WINDOW.

        This is the door an agent goes through to start the colleague the
        harvester hired for it, so it is the door the whole agents-hiring-
        agents claim rests on. It shares `_start` with the interview door
        rather than repeating it: the reason this was broken is that it did
        not.
        """
        desk = self.desk(name)
        if desk is None:
            raise Refused(404, "unknown_agent", f"no desk named {name!r}")
        try:
            started = self._start(desk)
        except spawn.SpawnError as exc:
            raise Refused(409, exc.reason, exc.detail) from exc
        except ValueError as exc:  # an engine build_argv does not know
            raise Refused(409, "unknown_engine", str(exc)) from exc
        self._refresh_holding()  # so the row a client reads next carries the verdict
        return started

    # -- approvals -----------------------------------------------------------

    def approvals(self) -> list[dict]:
        """Every live question, newest first.

        Sweeping first is deliberate: a question that timed out grants nothing
        and cannot usefully be acted on any more, so it leaves the board rather
        than sitting there inviting a decision that no longer applies.
        """
        asking.expire(self.asks_path)
        return [self._approval(a)
                for a in reversed(asking.pending(self.asks_path))]

    def _approval(self, ask: asking.Ask) -> dict:
        """One question, addressed to a DESK the app can draw it against.

        **The join, done here, because only the deck can do it.** The app
        filters cards with `agentName == <the desk whose conversation is open>`
        (`Approvals.swift`), and `agentName` decodes this `agent` field. The
        permission hook does not send a desk name, so this used to publish a
        raw session UUID -- which equals no desk, in no thread, for anyone.
        Measured live with four questions pending and every desk stalled behind
        them: the WhatsApp message went out, the app drew nothing, and there
        was no way to answer from the product.

        `asking.Ask.agent` has always been documented as "the desk/session name
        that asked". This is that contract being honoured on the way out rather
        than assumed on the way in.

        Three fields, so nothing is lost and nothing is invented:

        `agent`       the desk, when the deck can work out which one; otherwise
                      exactly what was recorded, unchanged.
        `asked_by`    always what the hook wrote down. The WhatsApp text and
                      anything reading the ledger are unaffected.
        `desk_known`  whether `agent` is a desk on the board. A client that
                      cannot tell a resolved desk from a fallback would file
                      the card under a name that is not one.
        """
        from . import standing_api  # it imports this module

        desk = self._desk_for_ask(ask)
        return {
            "id": ask.id,
            "ts": _num(ask.ts),
            "agent": desk or ask.agent,
            "asked_by": ask.agent,
            "desk_known": bool(desk),
            "tool": ask.tool,
            # Redacted on the way out, like every other thing this machine
            # sends: a `--token=` in a command line is a secret by
            # construction, and this API is reachable from a phone.
            "subject": asking.redact(ask.subject),
            "cwd": ask.cwd,
            "cwd_short": asking.short_cwd(ask.cwd),
            "status": ask.status,
            "options": self._options(ask),
            # "Always, up to a limit": `POST /v1/approvals/{id}/standing`.
            "standing_option": standing_api.standing_option(ask, desk),
        }

    def _options(self, ask: asking.Ask) -> list[dict]:
        """The three replies, each carrying the exact rule it would create.

        "Always allow" with nothing under it is how somebody grants far more
        than they meant to. The card has to be able to say *allow `gh pr*` in
        ~/Projects/acme* before he taps it, so every option states its own
        consequence -- in the same words the WhatsApp message uses.
        """
        where = asking.short_cwd(ask.cwd)
        options = [{"reply": "once", "available": True, "rule": None,
                    "summary": "just this time"}]
        desk = self._desk_for_ask(ask)

        # "Always" for a desk the deck can name: this desk, this class, this
        # folder and below -- and `rules` is the exact list `answer_ask` will
        # write, built by the same function, so the card cannot promise one
        # thing and store another. Offered on the handoff floor too: that is
        # the owner deciding, per desk, that a class he has looked at need not
        # come back to him. An elicitation has no rule engine behind it.
        if desk and ask.tool != asking.ELICITATION_TOOL:
            made = asking.desk_rules(ask, desk)
            options.append({
                "reply": "always", "available": True,
                "summary": asking.describe_class(ask, desk),
                "rule": asdict(made[0]),
                "rules": [asdict(r) for r in made],
            })
            replies = (("never", "refuse `{pattern}` in {where} from now on"),)
        else:
            replies = (
                ("always", "allow `{pattern}` in {where}, never ask again"),
                ("never", "refuse `{pattern}` in {where} from now on"),
            )

        for reply, phrasing in replies:
            # The secure-handoff floor, for a question no desk can be named
            # for: a global "always" on a credential is still not on offer.
            if reply == "always" and asking.is_handoff_subject(ask.subject):
                options.append({"reply": reply, "available": False, "rule": None,
                                "summary": asking.HANDOFF_NOTE})
                continue
            try:
                rule = asking.rule_from(ask, reply)
            except ValueError as exc:  # nothing to widen: an empty subject
                options.append({"reply": reply, "available": False,
                                "rule": None, "summary": str(exc)})
                continue
            options.append({
                "reply": reply,
                "available": True,
                "summary": phrasing.format(pattern=rule.pattern, where=where),
                "rule": asdict(rule),
            })
        return options

    def answer_ask(self, ask_id: str, reply) -> dict:
        asking.expire(self.asks_path)
        ask = asking.find(self.asks_path, ask_id)
        if ask is None:
            raise Refused(404, "unknown_ask", f"no ask {ask_id!r}")
        # Two distinct refusals, never a silent success: a reply that lands on
        # a settled question must not read as though it decided anything.
        if ask.status == "answered":
            raise Refused(409, "already_answered",
                          f"{ask.id} was already answered {ask.answered!r}")
        if ask.status == "expired":
            raise Refused(409, "expired",
                          f"{ask.id} timed out unanswered; it granted nothing")

        kind = asking.parse_reply(reply if isinstance(reply, str) else "")
        if kind is None:
            raise Refused(400, "bad_reply",
                          f"{reply!r} is not once, always or never")
        # The desk this "always" is FOR. Resolved here, by the same join the
        # card was drawn with, so the rule lands on the desk he was looking at.
        desk = self._desk_for_ask(ask) if kind == "always" else ""
        if (kind == "always" and not desk
                and asking.is_handoff_subject(ask.subject)):
            raise Refused(409, "always_not_available",
                          f"{asking.HANDOFF_NOTE}; answer it once instead")

        settled, rule = asking.answer(self.asks_path, ask.id, kind,
                                      self.rules_path, desk=desk)
        resumed = self._resume_desk(settled)
        # Re-read, do not reuse `settled`: `_resume_desk` stamps `resumed_at`
        # on the ledger, and handing back the pre-stamp copy made the response
        # say `resumed: true` beside `resumed_at: null` -- the API disagreeing
        # with itself about whether the desk was told. Measured live.
        settled = asking.find(self.asks_path, ask.id) or settled
        return {
            "ok": True,
            "ask": {**asdict(settled), "subject": asking.redact(settled.subject),
                    "status": settled.status},
            "rule": asdict(rule) if rule is not None else None,
            "rules": ([asdict(r) for r in asking.desk_rules(settled, desk)]
                      if desk and settled.answered == "always"
                      else [asdict(rule)] if rule is not None else []),
            # False for a `never`, and for a desk that has since left the
            # board. The card can then say "restarted" or "recorded" instead
            # of clearing and looking, either way, like the work went on.
            "resumed": resumed,
        }

    # -- standing permissions: what "Always allow" left behind ---------------

    def permissions(self, desk: str | None = None) -> list[dict]:
        """Every standing per-desk permission, newest last.

        What each desk may do without asking, in the card's own words, so the
        owner can see what he granted and take it back. Global rules written
        by hand are not listed: they are configuration, not his answers.
        """
        rows = []
        for rule in autoreview.load_rules(self.rules_path):
            if not rule.desk or rule.kind != autoreview.ALWAYS_ALLOW:
                continue
            if desk is not None and rule.desk != desk:
                continue
            rows.append({
                **asdict(rule),
                "cwd_short": asking.short_cwd(rule.cwd),
                "summary": (f"{rule.desk} may {describe_tool_short(rule.tool)}"
                            f" `{rule.pattern}` in "
                            f"{asking.short_cwd(rule.cwd)} and below"),
            })
        return rows

    def revoke_permission(self, rule_id: str) -> dict:
        """Remove one standing per-desk permission. The next call asks."""
        rules = autoreview.load_rules(self.rules_path)
        keep = [r for r in rules if not (r.desk and r.id == rule_id)]
        if len(keep) == len(rules):
            raise Refused(404, "unknown_permission",
                          f"no standing permission {rule_id!r}")
        autoreview.save_rules(self.rules_path, keep)
        return {"ok": True, "removed": rule_id}

    def _resume_desk(self, settled: asking.Ask) -> bool:
        """Tell the desk its question was answered yes. True if it was told.

        **This is the half of the approve loop that was missing.** Writing the
        rule unblocks the NEXT attempt; nothing was making the desk attempt
        again. `app._blocked_message` explicitly tells a denied agent not to
        retry in a loop and to carry on with something else -- correct advice,
        and it means a well-behaved agent is parked by instruction until
        something speaks to it. Measured three times in one run: he tapped
        Approve, the rule landed, and the ledger stayed silent until a human
        typed a follow-up chat message.

        Down `office.send`, not a new channel: it is the same queue his own
        replies use, so the desk's existing delivery hook picks it up on its
        next turn if the socket is shut, and the same fold puts it in his
        thread where he can see what his tap did. Sent as `deck` -- one of
        `OWNER_SENDERS` -- because it IS his decision arriving, not a third
        party talking.

        Three guards, in order:

        * A reply that grants nothing sends nothing. `never` writes a deny and
          a desk told to resume something just forbidden would be worse off
          than one told nothing.
        * `asking.mark_resumed` is the loop guard and it is durable: one ask is
          resumed once, ever, even against two concurrent taps.
        * The message itself tells the agent that a second refusal is a new
          question rather than something to retry.
        """
        if settled.answered not in ("once", "always"):
            return False
        # `_desk_for_ask` first: it is the same resolution the card was drawn
        # with, so the desk that is told to carry on is the desk the owner
        # thought he was answering for. `_desk_named` stays as the fallback --
        # it consults the raw collector snapshot, and a session id that no
        # roster row claims is still better addressed than dropped.
        target = self._desk_for_ask(settled) or self._desk_named(settled.agent)
        if not target:
            return False
        if not asking.mark_resumed(self.asks_path, settled.id):
            return False  # already told; a duplicate tap must not re-nudge

        body = asking.resume_text(settled)
        result = office.send(target, body, sender="deck",
                             extra={"resumes": settled.id})
        if not result.get("ok"):
            return False
        # Straight into the live session when there is one, exactly as
        # `send()` does -- otherwise the desk waits for a turn it has no
        # reason to take, which is the stall this method exists to end.
        if self._deliver is not None:
            try:
                # Marked as `deck`, matching the `sender` on the queued record
                # one line up. It IS his tap on Approve arriving, so a desk
                # about to resume something it was refused reads the owner's
                # word rather than an unattributed nudge from nowhere.
                if self._deliver(target, office.attribute(body, "deck")):
                    office.ack(result["id"])
            except Exception:  # a dead socket is not a failed approval
                pass
        return True

    def _desk_named(self, agent: str) -> str:
        """The DESK name to address, given whatever an ask recorded as `agent`.

        An ask does not reliably carry one. `app._permission` fills `agent`
        from the hook payload and `hooks/cc-permission.js` sends no desk name,
        so it falls through to the session id -- measured on a live hire, ask
        `ny8z4` recorded `agent: "8edb89dc-6be9-4daf-b90e-cf88c4630410"`.

        Addressing that id is not harmless. The office hook happens to match
        `to` against the session id as well as the name, so delivery survives;
        but the owner's thread, his unread count and `app._try_inject`'s socket
        lookup all key on the NAME, so the message he is meant to see lands in
        a thread titled with a UUID and the live session is never injected.

        Resolved off the collector's own snapshot, which already holds both,
        and only when the name it finds is a desk on the board -- a session id
        that resolves to nobody employed here is left exactly as it was rather
        than guessed at.
        """
        agent = str(agent or "").strip()
        if not agent:
            return ""
        if any(a["name"] == agent for a in self._agents):
            return agent
        for card in (self._snapshot() or {}).get("sessions") or []:
            if not isinstance(card, dict) or card.get("session_id") != agent:
                continue
            name = self._canonical(str(card.get("name") or ""))
            if name and any(a["name"] == name for a in self._agents):
                return name
        return agent

    # -- handoffs ------------------------------------------------------------
    #
    # The other half of "why is this desk stopped". An approval is a permission
    # question -- the desk could act, it needs a yes. A handoff is the case
    # where no yes helps because the desk CANNOT act: a 2FA code, a CAPTCHA, an
    # SMS confirmation, `gh auth login`. `server/handoff.py` has done all of
    # this since it was written and none of it was exposed, so the one block a
    # human must clear by hand reached him on WhatsApp or not at all.
    #
    # Shaped like an approval on purpose: same `agent` / `asked_by` /
    # `desk_known` join, same `options` list carrying its own words. One tray
    # in the app draws both, and it filters both on the same field.

    #: The two answers offered here, and the sentence each one puts on its
    #: button. `handoff.OUTCOMES` also holds `taken_over` -- deliberately NOT
    #: offered: it is the WhatsApp verb for "I have the keyboard right now",
    #: which leaves the work unfinished and the card on the board, and a tray
    #: button that changes nothing he can see is a button that gets tapped
    #: twice.
    HANDOFF_OPTIONS = (
        ("done", "I'm done, continue — the desk goes back and checks the step "
                 "actually worked before carrying on"),
        ("skipped", "Skip this step — the desk abandons that path for good and "
                    "reports what it can no longer finish"),
    )

    def handoffs(self) -> list[dict]:
        """Every block still waiting on a human, oldest first.

        Oldest first, unlike approvals: `handoff.waiting` orders them that way
        because the oldest block is the one that has been costing the most,
        and a handoff cannot be answered by a rule that makes the next one
        cheaper. Swept first, for the same reason approvals are -- a handoff
        that timed out grants nothing and must not invite a decision.
        """
        handoff.expire(self.handoffs_path)
        return [self._handoff_row(h) for h in handoff.waiting(self.handoffs_path)]

    def _handoff_row(self, blocked: handoff.Handoff) -> dict:
        """One handoff as the app's card.

        **Everything textual goes through `handoff.redact()`**, which is rule 3
        of that module and not a precaution: this payload reaches a phone, and
        a phone syncs. A one-time code sitting in a chat on a second device is
        the precise failure a secure handoff exists to prevent, so the door
        that leaves the machine is where the scrub has to happen -- the record
        on disk keeps whatever the desk wrote.

        Not clipped, unlike `handoff.compose`. That clips to fit a lock screen;
        a card can scroll, and `evidence` is the whole point of the card.
        """
        desk = self._desk_for_ask(blocked)
        return {
            "id": blocked.id,
            "ts": _num(blocked.ts),
            # Same three fields as an approval, same meanings. See `_approval`.
            "agent": desk or blocked.agent,
            "asked_by": blocked.agent,
            "desk_known": bool(desk),
            "kind": blocked.kind,
            "needs": handoff.redact(blocked.needs),
            # The load-bearing line: what has already happened, which is what
            # decides whether he opens a laptop now or after dinner.
            "state": handoff.redact(blocked.state),
            "where": handoff.redact(blocked.where),
            "evidence": handoff.redact(blocked.evidence),
            "status": blocked.status,
            "options": self._handoff_options(blocked),
            **self._always_option(blocked),
        }

    #: Also accepted on a browser card: `approve` is `done` by its own name.
    #: `always` is offered beside `options`, not in it -- a client that decodes
    #: `options` as exactly done|skipped keeps working.
    GRANT_REPLIES = ("approve", "always")

    def _handoff_options(self, blocked: handoff.Handoff) -> list[dict]:
        scope = browser_takeover.scope_of(blocked)
        rows = []
        for reply, summary in self.HANDOFF_OPTIONS:
            if scope and reply == "done":
                summary = (f"Allow — {scope['desk']} carries on by itself on "
                           f"{scope['origin']}; no take-over needed")
            elif scope and reply == "skipped":
                summary = ("No — the desk abandons this step and reports what "
                           "it can no longer finish")
            rows.append({"reply": reply, "available": True, "summary": summary})
        return rows

    def _always_option(self, blocked: handoff.Handoff) -> dict:
        scope = browser_takeover.scope_of(blocked)
        if not scope:
            return {}
        return {"always_option": {
            "reply": "always", "available": True,
            "summary": (f"Allow always — {scope['desk']} may do this "
                        f"({scope['kind']}) on {scope['origin']} from now on "
                        f"without asking")}}

    def resolve_handoff(self, handoff_id: str, reply) -> dict:
        """Record what the human did, and tell the desk which of the two it was.

        **`done` and `skipped` are different instructions, not different
        words** -- rule 2 of `server/handoff.py`. `done` orders a re-check,
        because "I did it" from a human is a claim and the code may have timed
        out; `skipped` orders abandonment, because an agent that reads a skip
        as retry-with-a-pause hammers a login screen until something locks.
        `handoff.resume_message` owns both texts, so this route cannot drift
        from the WhatsApp reply that means the same thing.

        And unlike an approval, the loop genuinely closes here: resuming after
        a handoff is an ordinary user message into the session, not the
        already-drawn permission prompt spec M1 proved unanswerable.
        """
        handoff.expire(self.handoffs_path)
        wanted = str(handoff_id or "").strip().lower()
        rows = handoff.load(self.handoffs_path)
        found = next((h for h in rows if h.id.lower() == wanted), None)
        if found is None:
            raise Refused(404, "unknown_handoff", f"no handoff {handoff_id!r}")

        outcome = str(reply or "").strip().lower()
        scope = browser_takeover.scope_of(found)
        grant = outcome if scope and outcome in self.GRANT_REPLIES else ""
        if grant:
            outcome = "done"
        if outcome not in {r for r, _ in self.HANDOFF_OPTIONS}:
            raise Refused(400, "bad_outcome",
                          f"{reply!r} is not done or skipped")
        # After the outcome check, not before: a mistyped verb on a live
        # handoff must leave it live and answerable rather than reporting it
        # as settled by somebody else.
        if found.status != "waiting":
            raise Refused(409, "already_resolved",
                          f"{found.id} was already {found.status}")

        # A browser card answered yes unlocks the desk's page at once: the
        # guard reads this grant on its very next check. `done` is his yes
        # too -- the page may still look the same after he acted on it.
        always = grant == "always"
        if scope and outcome == "done":
            browser_takeover.grant(
                browser_takeover.grants_path(self.handoffs_path),
                desk=scope["desk"], origin=scope["origin"],
                kind=scope["kind"], always=always)
        settled = handoff.resolve(self.handoffs_path, found.id, outcome)
        # He just signed in on one desk's screen: every desk gets that login.
        # Here, after the 409 above, so one card shares at most once.
        try:
            login_vault.after_handoff(settled, outcome)
        except Exception:  # noqa: BLE001 - sharing must never fail his answer
            _LOG.warning("login vault: sharing after %s failed", settled.id)
        return {
            "ok": True,
            "handoff": self._handoff_row(settled),
            "allowed": ("always" if always else "once")
                       if scope and outcome == "done" else None,
            "resumed": self._resume_after_handoff(settled, outcome),
        }

    def _allowed_body(self, settled: handoff.Handoff, outcome: str) -> str:
        """A browser card answered yes: tell the desk it is unlocked. PURE-ish
        (reads the grant `resolve_handoff` just wrote)."""
        scope = browser_takeover.scope_of(settled)
        if not scope or outcome != "done":
            return ""
        held = browser_takeover.granted(
            browser_takeover.grants_path(self.handoffs_path),
            desk=scope["desk"], origin=scope["origin"], kind=scope["kind"])
        return browser_takeover.allowed_brief(settled, always=held == "always")

    def _resume_after_handoff(self, settled: handoff.Handoff,
                              outcome: str) -> bool:
        """Tell the blocked desk what happened. True if it was told.

        The same two hops `_resume_desk` uses, for the same reason: down
        `office.send` so a desk with a shut socket still reads it on its next
        turn and the owner sees his own tap in the thread, then straight into
        the live session when there is one. Sent as `deck` -- it IS his
        decision arriving, not a third party talking.

        No `mark_resumed` guard here, unlike an approval: `resolve_handoff`
        already refuses a second answer with a 409, so a handoff can only reach
        this once.
        """
        target = self._desk_for_ask(settled) or self._desk_named(settled.agent)
        if not target:
            return False
        body = self._allowed_body(settled, outcome) or \
            handoff.resume_message(settled, outcome)
        result = office.send(target, body, sender="deck",
                             extra={"resolves": settled.id})
        if not result.get("ok"):
            return False
        if self._deliver is not None:
            try:
                if self._deliver(target, office.attribute(body, "deck")):
                    office.ack(result["id"])
            except Exception:  # a dead socket is not a failed handoff
                pass
        return True

    # -- routines ------------------------------------------------------------

    def routine_rows(self) -> list[dict]:
        path = self.routines_path
        return [self._routine_row(r, path) for r in routines.load_routines(path)]

    def _routine_row(self, routine: routines.Routine, path: Path) -> dict:
        history = routines.runs(path, routine.id)
        return {
            "id": routine.id,
            "agent": routine.agent,
            "prompt": routine.prompt,
            "trigger": routine.trigger,
            "next_run_at": _num(routine.next_run_at),
            "enabled": bool(routine.enabled),
            "runs": list(reversed(history))[:ROUTINE_RUNS_SHOWN],
        }

    @staticmethod
    def _no_client_schedule(payload: dict) -> None:
        """`next_run_at` is computed, never accepted.

        One next-run in the past from a client and the scheduler re-fires that
        routine on every tick, forever, into a session trying to work. The
        trigger is the input; when it next fires is an answer.
        """
        if "next_run_at" in payload:
            raise Refused(409, "next_run_at_is_computed",
                          "when a routine next fires comes from its trigger; "
                          "send the trigger and the server works it out")

    def _next_run(self, trigger: dict) -> float | None:
        if str(trigger.get("kind") or "") != "cron":
            return None
        try:
            return routines.next_fire(str(trigger.get("spec") or ""),
                                      str(trigger.get("tz") or "UTC"),
                                      time.time())
        except ValueError as exc:
            raise Refused(400, "bad_cron", f"trigger.spec: {exc}") from exc
        except KeyError as exc:  # ZoneInfoNotFoundError is a KeyError
            raise Refused(400, "bad_cron", f"trigger.tz: {exc}") from exc

    def create_routine(self, payload: dict) -> dict:
        self._no_client_schedule(payload)
        agent = _required(payload, "agent")
        prompt = _required(payload, "prompt")
        self.agent(agent)  # 404s for a name that is on nobody's board
        trigger = payload.get("trigger")
        if not isinstance(trigger, dict):
            raise Refused(400, "missing_field", "trigger is required")

        routine = routines.Routine(
            id=secrets.token_hex(6),
            agent=agent,
            prompt=prompt,
            trigger=trigger,
            next_run_at=self._next_run(trigger),
            enabled=bool(payload.get("enabled", True)),
        )
        path = self.routines_path
        with self._write_lock:
            routines.save_routines(path, routines.load_routines(path) + [routine])
        return self._routine_row(routine, path)

    def patch_routine(self, routine_id: str, payload: dict) -> dict:
        self._no_client_schedule(payload)
        path = self.routines_path
        with self._write_lock:
            loaded = routines.load_routines(path)
            current = next((r for r in loaded if r.id == routine_id), None)
            if current is None:
                raise Refused(404, "unknown_routine", f"no routine {routine_id!r}")

            patch: dict = {}
            if "agent" in payload:
                patch["agent"] = _required(payload, "agent")
                self.agent(patch["agent"])
            if "prompt" in payload:
                patch["prompt"] = _required(payload, "prompt")
            if "enabled" in payload:
                patch["enabled"] = bool(payload["enabled"])
            if "trigger" in payload:
                trigger = payload["trigger"]
                if not isinstance(trigger, dict):
                    raise Refused(400, "missing_field", "trigger must be an object")
                # A new trigger with the old next_run_at is a routine running
                # on yesterday's schedule, which is worse than not editing it.
                patch["trigger"] = trigger
                patch["next_run_at"] = self._next_run(trigger)

            updated = replace(current, **patch)
            routines.save_routines(
                path, [updated if r.id == routine_id else r for r in loaded])
        return self._routine_row(updated, path)

    def delete_routine(self, routine_id: str) -> dict:
        path = self.routines_path
        with self._write_lock:
            loaded = routines.load_routines(path)
            kept = [r for r in loaded if r.id != routine_id]
            if len(kept) == len(loaded):
                raise Refused(404, "unknown_routine", f"no routine {routine_id!r}")
            routines.save_routines(path, kept)
        return {"ok": True, "id": routine_id}


# ── auth ─────────────────────────────────────────────────────────────────────


#: The K3 gate (`pair_api.BearerGate`), installed by `server/app.py`. It lives
#: there rather than here because `pair_api` imports this module. Until one is
#: installed, `_authorise` is the master-token check it always was.
_GATE = None


def set_gate(gate) -> None:
    """Route every `_authorise` through `gate.check(request)` (None = master only)."""
    global _GATE
    _GATE = gate


def _authorise(request: Request) -> None:
    """Bearer token on every /v1 route, fail closed.

    With the K3 gate installed it accepts the master token or a paired device's
    token, and rate-limits wrong guesses per client address.

    No token configured is 503, never open access: this daemon binds
    127.0.0.1 and the only sanctioned route from a phone is an SSH tunnel or a
    private mesh. If someone later exposes the port, an unset env var must not
    be the thing that decides whether the roster is public.
    """
    if _GATE is not None:
        _GATE.check(request)
        return
    expected = (os.environ.get(TOKEN_ENV) or "").strip()
    if not expected:
        raise Refused(503, "auth_not_configured",
                      f"{TOKEN_ENV} is unset; /v1 is closed until it is set")
    scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip(), expected
    ):
        raise Refused(401, "unauthorized", "bearer token missing or wrong",
                      headers={"WWW-Authenticate": "Bearer"})


# ── the router ───────────────────────────────────────────────────────────────


def build_router(surface: Surface) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)
    subscribers: set[asyncio.Queue] = set()
    surface._subscribers = subscribers  # the background task fans out here

    async def _fresh() -> None:
        await asyncio.to_thread(surface._refresh_holding)

    @router.get("/agents")
    async def list_agents(request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON({"agents": surface.agents(),
                           "generated_at": time.time()})

    @router.get("/agents/{name}")
    async def get_agent(name: str, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON(surface.settings(name))

    @router.patch("/agents/{name}")
    async def patch_agent(name: str, payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON(await asyncio.to_thread(surface.patch_agent, name, payload))

    @router.post("/agents/{name}/read")
    async def mark_read(name: str, payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON(await asyncio.to_thread(
            surface.mark_read, name,
            up_to=str(payload.get("up_to") or "") or None,
            cursor=_check_cursor(payload.get("cursor"), "cursor"),
        ))

    # -- the "+" button ------------------------------------------------------

    @router.post("/agents", status_code=201)
    async def create_agent(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        row = await asyncio.to_thread(surface.create_agent, payload)
        # Beside the row, not in it. See `Surface.seat_warning`.
        warning = await asyncio.to_thread(surface.seat_warning, row.get("cwd") or "")
        # `row["cwd"]` is not necessarily the `cwd` that was posted. See
        # `Surface.seat_report`.
        where = await asyncio.to_thread(surface.seat_report,
                                        str(payload.get("cwd") or ""),
                                        row.get("cwd") or "")
        return StrictJSON({"ok": True, "agent": row, "pretrust": warning,
                           "seat": where}, status_code=201)

    @router.post("/agents/interview", status_code=201)
    async def interview_agent(payload: dict, request: Request) -> StrictJSON:
        """The conversational "+": no form, no project folder. OPENS A REAL
        TERMINAL WINDOW, so it is never exercised by the default suite."""
        _authorise(request)
        await _fresh()
        body = await asyncio.to_thread(surface.interview_agent, payload)
        return StrictJSON(body, status_code=201)

    # -- groups --------------------------------------------------------------

    @router.get("/groups")
    async def list_groups(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"groups": await asyncio.to_thread(surface.group_rows),
                           "generated_at": time.time()})

    @router.post("/groups", status_code=201)
    async def create_group(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        row = await asyncio.to_thread(surface.create_group, payload)
        return StrictJSON({"ok": True, "group": row}, status_code=201)

    @router.delete("/groups/{name}")
    async def delete_group(name: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(surface.delete_group, name))

    @router.delete("/agents/{name}")
    async def remove_agent(name: str, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON(await asyncio.to_thread(surface.remove_agent, name))

    @router.post("/agents/{name}/start")
    async def start_agent(name: str, request: Request) -> StrictJSON:
        """Opens a real Terminal window. Never exercised by the default suite."""
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(surface.start_agent, name))

    # -- "<Agent>'s screen" ---------------------------------------------------
    #
    # The panel in the owner's screenshot: the agent's live desktop, and an
    # Open button that takes it over. `server/sandbox.py` is the machine; this
    # is the only way to reach it from outside the Mac, and it is behind the
    # same bearer token as everything else on /v1.
    #
    # Deliberately NOT on `server/app.py`'s `/api/*`. That surface has no
    # token -- it is loopback-only and the board is the whole point -- and a
    # route that photographs a signed-in browser and injects keystrokes is
    # strictly more dangerous than the board. See docs/the-agents-computer.md.

    def _known_desk(name: str) -> str:
        """A desk on the roster, or 404. Never a string straight off the wire.

        `sandbox.container_name` validates too, but the failure would be a
        500 on a name that is merely unknown -- and a route that names a
        container from an unvalidated path segment is the shape of the bug
        `GET /v1/agents/../../x/screen` exploits.
        """
        if surface.desk(name) is None:
            raise Refused(404, "unknown_agent", f"no desk named {name!r}")
        return name

    def _sandbox_refusal(exc: sandbox.SandboxError) -> Refused:
        """One slug per cause, because each is a different thing for him to do.

        `docker_unavailable` is "start Docker Desktop"; `computer_not_running`
        is "the desk has no machine yet"; `no_frame` is "the machine is up and
        the capture failed", which is the one worth looking at.
        """
        status = {
            "docker_unavailable": 503,
            "computer_not_responding": 504,
            "computer_not_running": 409,
            "no_frame": 409,
            "input_refused": 409,
            "no_such_path": 404,
            "not_a_directory": 409,
            "not_a_file": 409,
            # 415, not 409: "I will not render this as text" is exactly what
            # Unsupported Media Type means, and a client can branch on it to
            # offer a download instead of showing an error.
            "not_text": 415,
            "list_failed": 409,
            "read_failed": 409,
        }.get(exc.reason, 409)
        return Refused(status, exc.reason, exc.detail)

    def _bad_request(exc: ValueError) -> Refused:
        """The client's fault, in two slugs rather than one.

        "You sent no command" and "you sent a path that is not a path" are
        different things to fix, and a single `bad_input` makes the client
        show the same sentence for both.
        """
        if isinstance(exc, sandbox.BadPath):
            return Refused(400, "bad_path", str(exc))
        return Refused(400, "bad_input", str(exc))

    @router.get("/agents/{name}/screen")
    async def screen_status(name: str, request: Request) -> StrictJSON:
        """Is there a screen to look at, and how big is it?

        Answered even when the computer is down -- "off" is the state the
        panel most needs to render, and a 404 here would make the client guess
        between "no such desk" and "no machine yet".
        """
        _authorise(request)
        desk = _known_desk(name)
        try:
            running = await asyncio.to_thread(sandbox.is_up, desk)
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc
        # A browser the reaper stopped comes back when he STAYS on its screen:
        # `ask` wakes it after a few seconds of polls, so clicking through
        # desks does not start one per desk (nine at once, 2026-10-01 19:24).
        waking = (not running) and (browser_reaper.ask(desk)
                                    or browser_reaper.is_waking(desk))
        width, height = sandbox.SIZE
        return StrictJSON({
            "desk": desk,
            "computer": {"running": running, "waking": waking,
                         "image": sandbox.IMAGE,
                         "container": sandbox.container_name(desk)},
            "display": sandbox.DISPLAY,
            "width": width, "height": height,
            "stale_after": screen.STALE_AFTER,
            "frame_url": f"/v1/agents/{desk}/screen.jpg",
            "input_url": f"/v1/agents/{desk}/screen/input",
            "stream_url": f"/v1/agents/{desk}/screen/stream",
            "terminal_url": f"/v1/agents/{desk}/terminal/stream",
            "generated_at": time.time(),
        })

    @router.get("/agents/{name}/screen.jpg")
    async def screen_frame(name: str, request: Request) -> Response:
        """One JPEG of the desk's display, captured now.

        Bytes rather than a base64 field in JSON: this is polled once a second
        while the panel is open, and base64 is a third more bytes plus a
        decode on the phone for a payload the platform's image loader would
        otherwise handle.

        `X-Frame-Age` travels with it because `screen.py`'s whole argument is
        that a frame without an age is a lie -- a checkout page looks the same
        a second old and twenty minutes dead. It is near zero here by
        construction (the capture is synchronous), and the header exists so it
        stays honest when a cache goes in front of this.

        `no-store`, not merely `no-cache`: this is a photograph of a signed-in
        browser and it has no business in a disk cache on the phone.
        """
        _authorise(request)
        desk = _known_desk(name)
        browser_reaper.touch(desk, view=True)  # he is looking: not idle
        started = time.time()
        try:
            blob = await asyncio.to_thread(sandbox.frame, desk)
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc
        return Response(
            content=blob, media_type="image/jpeg",
            headers={"Cache-Control": "no-store",
                     "X-Frame-Age": f"{max(0.0, time.time() - started):.3f}",
                     "X-Frame-Display": sandbox.DISPLAY},
        )

    @router.websocket("/agents/{name}/screen/stream")
    async def screen_stream_socket(websocket: WebSocket, name: str) -> None:
        """The screen pushed as it changes, and input on the same socket.

        `server/screen_stream.py` is the argument and the wire. Same bearer
        token as every route here, checked BEFORE the socket is accepted; a
        refusal closes with 4000 + the HTTP status the route would have
        answered (4401, 4404, 4503...). A computer that is down is woken and
        the client is told to fall back to polling, which says `waking`.
        """
        try:
            _authorise(websocket)
            desk = _known_desk(name)
            running = await asyncio.to_thread(sandbox.is_up, desk)
        except Refused as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        except sandbox.SandboxError as exc:
            await websocket.close(code=4000 + _sandbox_refusal(exc).status_code)
            return
        await websocket.accept()
        if not running:
            browser_reaper.wake(desk)
            await websocket.send_json({"type": "error",
                                       "reason": "computer_not_running",
                                       "fallback": "poll"})
            await websocket.close(code=4409)
            return
        await screen_stream.serve(websocket, desk)

    @router.post("/agents/{name}/screen/input")
    async def screen_input(name: str, payload: dict,
                           request: Request) -> StrictJSON:
        """The Open button: one gesture -- click, move, scroll, drag, a
        keystroke, or a string typed.

        Coordinates are in DISPLAY space (see `width`/`height` from
        `GET .../screen`), so a client that scaled the JPEG to fit a phone
        scales them back before sending. Off-screen is refused rather than
        clamped -- clamping hides the client's arithmetic bug by acting
        somewhere plausible instead. A `drag` checks *both* endpoints for the
        same reason.

        `bad_input` is a 400 and is the client's fault; a `SandboxError` is the
        machine's. `xdotool key --file` is the reason the split exists.
        """
        _authorise(request)
        desk = _known_desk(name)
        browser_reaper.touch(desk, view=True)
        try:
            await asyncio.to_thread(sandbox.send_input, desk, payload or {})
        except ValueError as exc:
            raise Refused(400, "bad_input", str(exc)) from exc
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc
        return StrictJSON({"ok": True,
                           "action": str((payload or {}).get("action") or "")})

    # -- the terminal and the file tree --------------------------------------
    #
    # The other two thirds of the same panel. `server/sandbox.py` is the
    # machine and holds every `docker` word; these three routes are the wire
    # and build no argv of their own, which is what keeps the uid, the
    # display and the container name stated in exactly one place.
    #
    # Both are behind the same bearer token as the screen, for a stronger
    # reason: a shell on the agent's computer is the most privileged thing
    # this daemon serves.

    @router.post("/agents/{name}/terminal")
    async def desk_terminal(name: str, payload: dict,
                            request: Request) -> StrictJSON:
        """Run one shell line inside that desk's container.

        **Never on the Mac.** The whole point of the desk's computer is that
        an agent's shell is not the owner's laptop, his keychain and his ssh
        agent; every command here leaves through `sandbox.exec_argv`.

        A non-zero `exit` is a **200** with the code in the body. `grep`
        finding nothing is a result the owner asked for, and a terminal that
        turns every failing command into an error page is not a terminal.
        What is *not* a 200 is the machine being unreachable -- that is a
        slug, and `computer_not_running` is the one the panel renders as
        "that agent's computer is not running".

        `cwd` is optional and absolute. It becomes `docker exec --workdir`,
        not a `cd` in the shell line, so a directory name can never be read
        as a command.
        """
        _authorise(request)
        desk = _known_desk(name)
        body = payload or {}
        try:
            result = await asyncio.to_thread(
                sandbox.run_command, desk,
                str(body.get("command") or ""),
                cwd=str(body.get("cwd") or "") or None)
        except ValueError as exc:
            raise _bad_request(exc) from exc
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc
        return StrictJSON(result)

    @router.websocket("/agents/{name}/terminal/stream")
    async def terminal_stream_socket(websocket: WebSocket, name: str,
                                     window: str = "deck", cols: int = 80,
                                     rows: int = 24) -> None:
        """A real terminal in that desk's container: bash in tmux on a PTY.

        `server/terminal_stream.py` is the argument and the wire. Same bearer
        token, checked BEFORE accept; refusals close with 4000 + the status
        (4401, 4404, and 4400 for a window or size that is not one). A
        computer that is down is woken and the client is told so with 4409.
        `window=agent` is the read-only mirror of the agent's own commands.
        """
        try:
            _authorise(websocket)
            desk = _known_desk(name)
            terminal_stream.check_window(window)
            terminal_stream.check_size(cols, rows)
            running = await asyncio.to_thread(sandbox.is_up, desk)
        except Refused as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        except ValueError:
            await websocket.close(code=4400)
            return
        except sandbox.SandboxError as exc:
            await websocket.close(code=4000 + _sandbox_refusal(exc).status_code)
            return
        await websocket.accept()
        if not running:
            browser_reaper.wake(desk)
            await websocket.send_json({"type": "error",
                                       "reason": "computer_not_running",
                                       "detail": "waking it; try again shortly"})
            await websocket.close(code=4409)
            return
        await terminal_stream.serve(websocket, desk, window=window,
                                    cols=cols, rows=rows)

    @router.get("/agents/{name}/files")
    async def desk_files(request: Request, name: str,
                         path: str = sandbox.DESK_HOME) -> StrictJSON:
        """One directory inside that desk's container, one level down.

        `path` defaults to the agent's own home, because that is the panel's
        first paint and it has to open somewhere real. Entries are sorted
        directories-first then by name: `find` returns directory order, and a
        tree that reorders itself on every refresh is unusable.

        A path that is not there is `404 no_such_path`, not an empty list.
        Those render identically and are opposite facts about whether the
        agent did any work.
        """
        _authorise(request)
        desk = _known_desk(name)
        try:
            return StrictJSON(await asyncio.to_thread(
                sandbox.list_dir, desk, path))
        except ValueError as exc:
            raise _bad_request(exc) from exc
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc

    @router.get("/agents/{name}/files/read")
    async def desk_file(request: Request, name: str, path: str) -> StrictJSON:
        """A text file inside that desk's container, capped and marked.

        The cap is `sandbox.READ_MAX` -- `office.RECORD_MAX`, the same 64 000
        as a queued message -- and the cut says so twice: in `truncated` and
        in the text itself, so a client that never learns to read the field
        still shows the owner that something was taken away.

        A binary file is `415 not_text`, never mojibake. A JPEG decoded
        leniently is a screenful of U+FFFD that reads like a corrupt text
        file, and a client can branch on 415 to offer a download instead.
        """
        _authorise(request)
        desk = _known_desk(name)
        try:
            return StrictJSON(await asyncio.to_thread(
                sandbox.read_file, desk, path))
        except ValueError as exc:
            raise _bad_request(exc) from exc
        except sandbox.SandboxError as exc:
            raise _sandbox_refusal(exc) from exc

    @router.get("/agents/{name}/files/download")
    async def desk_file_download(request: Request, name: str,
                                 path: str) -> Response:
        """The whole file, raw bytes, for a client to save -- not to render.

        `.../files/read` decodes and caps at `sandbox.READ_MAX` because it
        renders inline as JSON text; this route has nowhere to render
        anything, so it hands back exactly what `cat` produced and never
        guesses a `Content-Type` from the name -- always
        `application/octet-stream`.

        The size is decided from a `stat` **before** `cat` is ever built:
        `sandbox.download_file` raises `too_large` off that `stat` alone, so
        an 80 GB file is a `413`, not an 80 GB read holding one of the
        daemon's threads for as long as the copy takes.

        The filename in `Content-Disposition` is sanitised, not trusted: a
        Linux filename may legally contain a `"` or a newline -- the same
        bytes `sandbox._check_path` lets through on purpose -- and a raw
        newline in a header is a response-splitting shape.
        """
        _authorise(request)
        desk = _known_desk(name)
        try:
            result = await asyncio.to_thread(
                sandbox.download_file, desk, path)
        except ValueError as exc:
            raise _bad_request(exc) from exc
        except sandbox.SandboxError as exc:
            if exc.reason == "too_large":
                raise Refused(413, exc.reason, exc.detail) from exc
            raise _sandbox_refusal(exc) from exc
        blob = result["blob"]
        raw_name = result["path"].rsplit("/", 1)[-1] or "download"
        safe_name = re.sub(r'[\x00-\x1f\x7f]', "_", raw_name).replace(
            "\\", "\\\\").replace('"', '\\"')
        return Response(
            content=blob, media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "Content-Length": str(len(blob)),
                "Cache-Control": "no-store",
            },
        )

    # -- the approval card ---------------------------------------------------

    @router.get("/approvals")
    async def list_approvals(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"approvals": await asyncio.to_thread(surface.approvals),
                           "generated_at": time.time()})

    @router.post("/approvals/{ask_id}")
    async def answer_approval(ask_id: str, payload: dict,
                              request: Request) -> StrictJSON:
        _authorise(request)
        # Like every other write route, and now for a reason: answering an ask
        # sends the desk a message, and working out WHICH desk means resolving
        # the session id the permission hook recorded against the live board.
        # A stale board resolves it to nothing and the resume goes to a UUID.
        await _fresh()
        return StrictJSON(await asyncio.to_thread(
            surface.answer_ask, ask_id, payload.get("reply")))

    # -- standing permissions ------------------------------------------------

    @router.get("/permissions")
    async def list_permissions(request: Request,
                               desk: str | None = Query(None)) -> StrictJSON:
        _authorise(request)
        return StrictJSON({
            "permissions": await asyncio.to_thread(surface.permissions, desk),
            "generated_at": time.time()})

    @router.delete("/permissions/{rule_id}")
    async def revoke_permission(rule_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(surface.revoke_permission,
                                                  rule_id))

    # -- the other half of the attention tray --------------------------------

    @router.get("/handoffs")
    async def list_handoffs(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"handoffs": await asyncio.to_thread(surface.handoffs),
                           "generated_at": time.time()})

    @router.post("/handoffs/{handoff_id}")
    async def resolve_handoff(handoff_id: str, payload: dict,
                              request: Request) -> StrictJSON:
        _authorise(request)
        # Same reason `POST /v1/approvals/{id}` refreshes: clearing a handoff
        # sends the blocked desk an instruction, and working out WHICH desk
        # means resolving what it recorded against the live board.
        await _fresh()
        return StrictJSON(await asyncio.to_thread(
            surface.resolve_handoff, handoff_id, payload.get("reply")))

    @router.post("/logins")
    async def share_logins(payload: dict, request: Request) -> StrictJSON:
        """A sign-in done on the owner's Mac (his iCloud passkey lives there,
        never in a desk) reaches every desk's browser and the login vault.
        Write-only: nothing here reads a cookie back. Counts only."""
        _authorise(request)
        try:
            shared = await asyncio.to_thread(
                login_vault.share_in, payload.get("cookies"),
                source={"chrome": "mac-chrome", "fresh": "mac-fresh"}.get(
                    str(payload.get("via") or ""), "mac"))
        except login_vault.ShareRefused as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from None
        return StrictJSON({"ok": True, **shared})

    # -- the phone asks the Mac to sign a desk in (server/login_requests.py) --

    def _login_requests_path():
        return login_requests.path_for(surface.handoffs_path)

    async def _login_call(fn, *args):
        try:
            return await asyncio.to_thread(fn, _login_requests_path(), *args)
        except login_requests.RequestRefused as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from None

    @router.post("/logins/requests", status_code=201)
    async def file_login_request(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        row = await _login_call(login_requests.file_request, payload)
        return StrictJSON({"ok": True, "request": row}, status_code=201)

    # Declared before `/{request_id}` so "next" is never read as an id.
    @router.get("/logins/requests/next")
    async def claim_login_request(request: Request,
                                  node: str = Query(default=""),
                                  wait: float = Query(default=0.0)) -> StrictJSON:
        """The Mac's long-poll: holds up to `wait` seconds (max 25) for a
        request, claims it for `node` and returns it, else `request: null`."""
        _authorise(request)
        deadline = time.monotonic() + max(0.0, min(float(wait), 25.0))
        while True:
            row = await _login_call(login_requests.claim, node)
            if row is not None or time.monotonic() >= deadline:
                return StrictJSON({"request": row})
            await asyncio.sleep(0.5)

    @router.get("/logins/requests/{request_id}")
    async def login_request_status(request_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"request": await _login_call(login_requests.get,
                                                        request_id)})

    @router.post("/logins/requests/{request_id}/result")
    async def login_request_result(request_id: str, payload: dict,
                                   request: Request) -> StrictJSON:
        _authorise(request)
        row = await _login_call(login_requests.report, request_id, payload)
        return StrictJSON({"ok": True, "request": row})

    # -- the routines panel --------------------------------------------------

    @router.get("/routines")
    async def list_routines(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"routines": await asyncio.to_thread(surface.routine_rows),
                           "generated_at": time.time()})

    @router.post("/routines", status_code=201)
    async def create_routine(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        row = await asyncio.to_thread(surface.create_routine, payload)
        return StrictJSON({"ok": True, "routine": row}, status_code=201)

    @router.patch("/routines/{routine_id}")
    async def patch_routine(routine_id: str, payload: dict,
                            request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        row = await asyncio.to_thread(surface.patch_routine, routine_id, payload)
        return StrictJSON({"ok": True, "routine": row})

    @router.delete("/routines/{routine_id}")
    async def delete_routine(routine_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(surface.delete_routine,
                                                  routine_id))

    @router.get("/threads")
    async def list_threads(request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON({"threads": surface.threads(),
                           "generated_at": time.time()})

    @router.get("/threads/{thread_id}/messages")
    async def list_messages(
        thread_id: str, request: Request,
        since: str | None = Query(default=None),
        before: str | None = Query(default=None),
        limit: str = Query(default=str(DEFAULT_LIMIT)),
    ) -> StrictJSON:
        _authorise(request)
        await _fresh()
        return StrictJSON(surface.page(
            thread_id,
            since=_check_cursor(since, "since"),
            before=_check_cursor(before, "before"),
            limit=_clamp_limit(limit),
        ))

    @router.post("/threads/{thread_id}/messages", status_code=201)
    async def post_message(thread_id: str, payload: dict,
                           request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        result = await asyncio.to_thread(
            surface.send, thread_id, str(payload.get("text") or ""),
            sender=OWNER if payload.get("as") is None else payload["as"],
            channel=("text" if payload.get("channel") is None
                     else payload["channel"]),
            call_id=payload.get("call_id") or "",
            reply_to=payload.get("reply_to"),
        )
        await _publish(await asyncio.to_thread(surface.refresh))
        return StrictJSON(result, status_code=201)

    @router.get("/owner/alerts")
    async def list_owner_alerts(
        request: Request,
        since: str | None = Query(default=None),
        limit: str = Query(default=str(Surface.ALERTS_MAX)),
        active: str = Query(default=""),
    ) -> StrictJSON:
        """What is new FOR HIM since a cursor: the phone's background poll,
        the Mac's notifier and the ntfy sender all read this one answer."""
        _authorise(request)
        await _fresh()
        body = await asyncio.to_thread(
            surface.owner_alerts, _check_cursor(since, "since"),
            limit=min(_clamp_limit(limit), Surface.ALERTS_MAX),
            active=active in ("1", "true", "yes"))
        return StrictJSON({**body, "generated_at": time.time()})

    @router.post("/decisions/{decision_id}")
    async def answer_decision(decision_id: str, payload: dict,
                              request: Request) -> StrictJSON:
        _authorise(request)
        await _fresh()
        result = await asyncio.to_thread(
            surface.answer_decision, decision_id, payload.get("value"))
        await _publish(await asyncio.to_thread(surface.refresh))
        return StrictJSON(result)

    async def _publish(events: list[dict]) -> None:
        for event in events:
            payload = json.dumps(_clean(event), ensure_ascii=False,
                                 allow_nan=False)
            for queue in list(subscribers):
                if queue.full():
                    try:
                        queue.get_nowait()  # slow client: drop backlog, not it
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(payload)

    surface._publish = _publish

    @router.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        _authorise(request)
        await _fresh()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        subscribers.add(queue)

        async def gen():
            try:
                hello = {"type": "hello", "ts": time.time(),
                         "agents": surface.agents(), "threads": surface.threads()}
                yield "data: " + json.dumps(_clean(hello), ensure_ascii=False,
                                            allow_nan=False) + "\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # The client's connection indicator lives on this.
                        yield ("data: " + json.dumps(
                            {"type": "heartbeat", "ts": time.time()}) + "\n\n")
                        continue
                    yield f"data: {payload}\n\n"
            finally:
                subscribers.discard(queue)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


#: How often idle desk browsers are looked for. `docker ps` plus two small
#: files: cheap, and a browser past its idle window costs RAM every second.
REAP_SECONDS = 30.0
HEAL_SECONDS = 60.0


def _public_quote(quote: dict) -> dict:
    """`reply_to` as a client and the record carry it: the id, who wrote the
    quoted message, and a short one-line excerpt -- never the whole of it."""
    return {"id": quote["id"], "author": quote["author"],
            "excerpt": office.excerpt(quote["text"])}


def register(app, *, surface: Surface | None = None, snapshot=None, comms=None,
             deliver=None, roster_path=None, prefs_path=None, asks_path=None,
             rules_path=None, routines_path=None, handoffs_path=None,
             background: bool = True) -> Surface:
    """Mount `/v1` onto an existing app. The daemon's one call.

    `background=True` starts ONE shared task that folds the collector snapshot
    forward and fans events out to every SSE client. One task, not one per
    connection: a hundred phones must not become a hundred pollers.
    """
    if surface is None:
        surface = Surface(snapshot=snapshot, comms=comms, deliver=deliver,
                          roster_path=roster_path, prefs_path=prefs_path,
                          asks_path=asks_path, rules_path=rules_path,
                          routines_path=routines_path,
                          handoffs_path=handoffs_path)
    app.add_exception_handler(Refused, _refused_handler)
    app.include_router(build_router(surface))

    if background:
        async def _loop() -> None:
            while True:
                try:
                    events = await asyncio.to_thread(surface.refresh)
                    await surface._publish(events)
                except Exception as exc:  # never let one tick kill the stream
                    print(f"[agent-deck] /v1 refresh failed: {exc!r}")
                await asyncio.sleep(REFRESH_SECONDS)

        # `app.on_event`, not `app.add_event_handler`: this FastAPI has no such
        # attribute, and the failure is an ImportError at mount time rather than
        # anything a router-level test would ever see.
        app.on_event("startup")(lambda: asyncio.create_task(_loop()))

        async def _reap() -> None:
            """Idle desk browsers stopped (server/browser_reaper.py). Its own
            task: a slow `docker` must never delay the board's refresh."""
            await asyncio.sleep(REAP_SECONDS)  # states settle after a start
            while True:
                try:
                    states = {a["name"]: a["state"] for a in surface.agents()}
                    stopped = await asyncio.to_thread(browser_reaper.sweep, states)
                    for desk in stopped:
                        print(f"[agent-deck] reaper: stopped {desk}'s idle browser",
                              flush=True)
                except Exception as exc:
                    print(f"[agent-deck] reaper failed: {exc!r}")
                await asyncio.sleep(REAP_SECONDS)

        app.on_event("startup")(lambda: asyncio.create_task(_reap()))

        async def _heal() -> None:
            """A live desk that lost its computer tools is respawned in place,
            between turns (server/computer_heal.py)."""
            await asyncio.sleep(HEAL_SECONDS)
            while True:
                try:
                    await asyncio.to_thread(computer_heal.sweep)
                except Exception as exc:
                    print(f"[agent-deck] heal failed: {exc!r}")
                await asyncio.sleep(HEAL_SECONDS)

        app.on_event("startup")(lambda: asyncio.create_task(_heal()))

    return surface
