"""The phone loop: page him when he is needed, and get his answer back.

Two halves that only mean anything together, which is why they share a file.

OUTBOUND. `server/notify.py` was complete, correct and called by NOTHING
(measured: `grep -rn notify server/` matched only itself), so every question the
deck recorded sat on a board nobody was looking at. Exactly two things buzz his
phone here:

  * `page_ask` -- an agent is blocked on something only he can lift.
  * `page_done` -- the session he actually talks to finished a run.

Everything else stays on the board. A phone that buzzes for every milestone is
a phone he mutes, and a muted phone is the same failure as no phone at all.

And the two are not the same kind of message, which is why only one of them
waits. `page_ask` is a QUESTION he has to answer, and the deck is where the
Approve button is -- so it is held for `notify.quiet_seconds()` and escalates
only if it is still unanswered then. `page_done` is a REPORT: he is being told
his run finished, there is nothing to answer, and holding it back would just
make the product slower at the one thing he asked it for.

INBOUND, and this is the load-bearing finding. Measured by reading
`~/Projects/comunicate_with_me`: the bridge HAS a complete inbound pipeline --
`src/wa-client.js` emits, `src/router.js` decides, `src/inject.js` writes a
`{"type":"user",...}` frame into a Claude Code session's UNIX socket under
`/tmp/cc-socks`. What it has NOT got is any way to call an HTTP endpoint:
grepping `src/`, `bin/` and `hooks/` for `fetch(`, `http.request` and `webhook`
turns up one hit, its own CLI talking to its own daemon. There is no webhook to
point at the deck.

So the deck is routed to the only way anything can be: **it binds its own socket
in that directory and is addressed like a session.** `page_ask` sends with that
socket as its return address (via `notify.send(reply_to=...)`, which the bridge
records against the WhatsApp message ids), and a reply -- quoting the message,
or riding sticky focus -- is injected straight into `Listener` below.

Which return address goes on which message is not cosmetic:

  * A BLOCKED ask answers to the DECK. Written down in `server/app.py` and
    measured before this slice: text injected into a session with a permission
    modal up lands UNDERNEATH the modal, unsubmitted. The stuck session cannot
    read an answer. The deck can -- it writes the rule and resumes the desk.
  * A FINISHED run answers to the SESSION ITSELF. A DONE session sits at its own
    composer with nothing drawn over it, which is the case the bridge's inject
    path was built for, so his next instruction goes straight in.

Nothing here knows a phone number, a token or a recipient; `notify.py` owns the
subprocess and the bridge owns the credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import asking, notify

#: The deck's own socket, living beside the real sessions' because
#: `src/inject.js` refuses to write anywhere outside that directory.
DECK_SOCKET_NAME = "agentdeck.sock"

#: What the bridge records this "session" as. Deliberately not a UUID: it shows
#: up in the bridge's own session menu on his phone, and "agent-deck" is what it
#: is.
DECK_SESSION_ID = "agent-deck"

#: `src/inject.js` prefixes every injected turn. Stripped when present, never
#: required -- the bridge owns that string and may reword it.
_BRIDGE_PREFIX = "[via WhatsApp"

#: Newest kept. This file is written on the collector's 1 Hz path.
MAX_PAGED = 200

#: In-process mirror of the ledger, so the common "already paged" answer costs
#: no read. Cleared by tests to simulate a restart.
_PAGED: dict[str, str] = {}

# Re-entrant: `page_ask` holds it in `_page` while the urgent gate, which
# guards the same ledger, takes it again.
_LOCK = threading.RLock()


# ── the once-only ledger ────────────────────────────────────────────────────


def _read(ledger: Path) -> dict:
    try:
        data = json.loads(Path(ledger).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(ledger: Path, data: dict) -> None:
    if len(data) > MAX_PAGED:
        keep = sorted(data.items(), key=lambda kv: kv[1])[-MAX_PAGED:]
        data = dict(keep)
    path = Path(ledger)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass  # a page he already got is not worth failing the tick over


def _mirror_key(key: str, ledger: Path) -> str:
    """The in-memory mirror is keyed by LEDGER as well as by key.

    Keyed by the key alone, the mirror answers "already paged" for a question
    recorded in a different ledger entirely. Production has one ledger so it
    would never have shown there -- it showed as one test poisoning the next,
    which is the same defect wearing a smaller hat.
    """
    return f"{Path(ledger)}\x00{key}"


def _already(key: str, ledger: Path) -> bool:
    if _mirror_key(key, ledger) in _PAGED:
        return True
    on_disk = _read(ledger)
    for known, stamp in on_disk.items():
        _PAGED[_mirror_key(known, ledger)] = stamp
    return key in on_disk


def _mark(key: str, ledger: Path) -> None:
    stamp = f"{time.time():.3f}"
    _PAGED[_mirror_key(key, ledger)] = stamp
    data = _read(ledger)
    data[key] = stamp
    _write(ledger, data)


def _forget(prefix: str, ledger: Path) -> None:
    """Drop every key under `prefix`, so the next transition pages again."""
    stale = _mirror_key(prefix, ledger)
    for key in [k for k in _PAGED if k.startswith(stale)]:
        _PAGED.pop(key, None)
    data = _read(ledger)
    dropped = {k: v for k, v in data.items() if not k.startswith(prefix)}
    if dropped != data:
        _write(ledger, dropped)


# ── outbound ────────────────────────────────────────────────────────────────


def _address(reply_socket: str | None, session_id: str) -> dict | None:
    if not reply_socket:
        return None
    return {"socket": str(reply_socket), "session_id": session_id}


#: How long to wait after the first failed attempt, and the ceiling the wait
#: grows to. Doubling from 30s reaches the 15-minute cap in six attempts, so a
#: bridge that is down for a day costs about a hundred attempts instead of
#: 86,400 -- and a message he cannot act on never arrives twice in a minute.
RETRY_AFTER_SECONDS = 30.0
RETRY_CEILING_SECONDS = 900.0

#: Prefix for the "we tried and it failed" record, kept in the same ledger and
#: deliberately NOT the same key as the sent record. A failed attempt must
#: never be mistaken for a delivery: that is the swallowing this whole module
#: refuses to do.
ATTEMPT_PREFIX = "try:"


def _due_at(key: str, ledger: Path) -> tuple[float, int]:
    """When this key may next be attempted, and how many times it has failed.

    (0.0, 0) for a key that has never failed -- so a brand-new question is
    always attempted immediately and never inherits another one's backoff.
    """
    raw = _read(ledger).get(ATTEMPT_PREFIX + key)
    if not raw:
        return 0.0, 0
    due, _, count = str(raw).partition("|")
    try:
        return float(due), int(count or 0)
    except ValueError:
        # An unreadable record must not silence the question forever. Treat it
        # as never attempted: worst case is one extra message.
        return 0.0, 0


def _mark_failed(key: str, ledger: Path, now: float) -> None:
    """Record the failure and widen the gap before the next attempt."""
    _, failures = _due_at(key, ledger)
    failures += 1
    wait = min(RETRY_AFTER_SECONDS * (2 ** (failures - 1)),
               RETRY_CEILING_SECONDS)
    data = _read(ledger)
    data[ATTEMPT_PREFIX + key] = f"{now + wait:.3f}|{failures}"
    _write(ledger, data)


def _page(key: str, text: str, *, ledger: Path, send, address,
          now: float | None = None) -> notify.Sent:
    """Send once, and record it as sent ONLY on a clean exit.

    The exit-code half is the point. `notify.send` refuses to claim sent when
    the bridge daemon is down (3) or unlinked (4); marking the ledger anyway
    would turn one dead daemon into one permanently swallowed question, and it
    would look identical to a question he chose to ignore.

    The BACKOFF is the other half of that same guarantee, and it was missing.
    "Not delivered" was retried as fast as the collector turns. Measured on the
    Linux box, which has no bridge: five pending questions produced 285 send
    attempts in 60 seconds. With a working bridge that is his phone buzzing
    once a second, which is the most reliable way to make him stop trusting
    this. So a failure is REMEMBERED -- under its own key, never the sent key
    -- and the gap doubles to a ceiling. The question is still owed, and still
    goes out the moment the bridge answers.

    `now` is injected rather than read, so the retry policy can be driven
    across hours in a test without sleeping through them.
    """
    # Resolved HERE, not as a default argument. `send=notify.send` in a
    # signature binds the function object at import time, so the sender can
    # never afterwards be replaced -- not by a test, and not by anything that
    # wants to route a page somewhere else.
    send = notify.send if send is None else send
    moment = time.time() if now is None else now
    with _LOCK:
        if _already(key, ledger):
            return notify.Sent(True, 0, "already paged")
        due, failures = _due_at(key, ledger)
        if moment < due:
            return notify.Sent(
                False, None,
                f"waiting {due - moment:.0f}s before retry {failures + 1}")
        result = send(text, reply_to=address)
        if result.ok:
            _mark(key, ledger)
        else:
            _mark_failed(key, ledger, moment)
        return result


def page_ask(ask: asking.Ask, *, ledger: Path, send=None,
             reply_socket: str | None = None,
             now: float | None = None,
             quiet: float | None = None) -> notify.Sent:
    """Buzz him about a question only he can answer. Once per ask, ever.

    THE APP GETS FIRST REFUSAL. His words: *"only if i have not confirmed for
    few min it should whatsapp me, other ways on the app."* A question appears
    on the deck the moment it is recorded -- that is unchanged and is the
    primary surface -- and nothing leaves for his phone until it has gone
    `notify.quiet_seconds()` without being answered.

    The hold is a comparison against `ask.ts`, deliberately, rather than a
    timer armed at the send site. `app._page_the_owner` re-reads
    `asking.pending()` on every collector tick, so an ask he settles on the
    deck simply stops being offered here and no message is ever composed for
    it. The decision is therefore taken from the item's LIVE state after the
    window, which is the only moment at which it can be right -- at the instant
    the question is recorded it is impossible to know whether he will answer,
    and sitting in front of the board he usually does.

    Held is not failed: `ok=True` with nothing sent, the same shape `_page`
    already uses for "already paged" and `page_done` for "not the manager", so
    the caller's "did NOT reach the phone" warning stays reserved for a bridge
    that actually refused.

    The text is `asking.compose` verbatim -- not a second phrasing of it. That
    module already redacts the subject, caps it to eight lines, bolds the agent
    and prints the id he quotes back; re-composing here would be a second place
    for a credential to escape.
    """
    window = notify.quiet_seconds() if quiet is None else float(quiet)
    moment = time.time() if now is None else now
    waited = moment - float(ask.ts)
    if waited < window:
        return notify.Sent(True, 0,
                           f"on the deck for another {window - waited:.0f}s")

    def urgent(text, *, reply_to=None):
        return page_urgent(text, ledger=ledger, send=send, address=reply_to,
                           now=now)

    return _page(f"ask:{ask.id}", asking.compose(ask), ledger=ledger,
                 send=urgent, address=_address(reply_socket, DECK_SESSION_ID),
                 now=now)


# ── the urgent channel ──────────────────────────────────────────────────────
#
# OWNER RULING 2026-09-30: *"we don't need the WhatsApp bridge on the deck
# anymore, we have clear communication in the app -- only in urgent cases
# should he send me; otherwise Atlas."* Everything the deck says to him goes
# to the app. What still reaches WhatsApp from the deck daemon goes through
# `page_urgent`, and only through it: a blocking ask left unanswered past the
# threshold, and a line the chief of staff marked urgent. (`bin/deckdoctor`
# is the other sender -- the deck being down is the one thing the app cannot
# tell him -- and it is its own process with its own once-per-outage guard.)

#: The first thing on his lock screen, so an urgent message never reads like
#: the traffic this channel no longer carries.
URGENT_MARK = "*URGENT*"

#: Same text within this long is the same emergency, not a new one.
URGENT_DEDUPE_SECONDS = 24 * 3600.0

_URGENT_LAST = "urgent:last"
_URGENT_SIG = "urgent:sig:"


def page_urgent(text: str, *, ledger: Path, send=None, address=None,
                now: float | None = None,
                gap: float | None = None) -> notify.Sent:
    """One urgent WhatsApp, rate limited and de-duplicated. Never raises.

    * At most one per `gap` seconds (deck.toml `[notify] urgent_gap_minutes`,
      default 10). A message held by the gap is NOT delivered and says so --
      `ok=False` -- so a caller with a retry (`_page`) tries again later
      rather than recording a delivery that never happened.
    * The same text is sent once per day. A repeat is `ok=True`: he already
      has it, and that is what the caller needs to stop asking.

    The gap and the de-dupe live in the same ledger as the once-only pages, so
    they survive a restart: a crash loop must not become a message loop.
    """
    body = str(text or "").strip()
    if not body:
        return notify.Sent(False, None, "nothing to send")
    send = notify.send if send is None else send
    moment = time.time() if now is None else float(now)
    window = notify.urgent_gap_seconds() if gap is None else float(gap)
    sig = _URGENT_SIG + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    with _LOCK:
        data = _read(ledger)
        try:
            seen = float(data.get(sig) or 0.0)
        except ValueError:
            seen = 0.0
        if seen and moment - seen < URGENT_DEDUPE_SECONDS:
            return notify.Sent(True, 0, "already sent as urgent")
        try:
            last = float(data.get(_URGENT_LAST) or 0.0)
        except ValueError:
            last = 0.0
        if last and moment - last < window:
            return notify.Sent(
                False, None,
                f"urgent rate limit: next in {window - (moment - last):.0f}s")
        result = send(f"{URGENT_MARK} {body}", reply_to=address)
        if getattr(result, "ok", False):
            data = _read(ledger)
            data[_URGENT_LAST] = f"{moment:.3f}"
            data[sig] = f"{moment:.3f}"
            _write(ledger, data)
        return result


def compose_done(card: dict) -> str:
    """What he is told when his own session finishes. PURE.

    First line is the whole answer; the rest is only there so he knows which
    tree it happened in. Redacted like everything else that leaves for the
    phone -- a branch name is user input.
    """
    name = asking.redact(str(card.get("name") or "a session"))
    where = " · ".join(p for p in (str(card.get("project") or ""),
                                   str(card.get("git_branch") or "")) if p)
    lines = [f"**{name}** finished and is waiting on you."]
    if where:
        lines.append(asking.redact(where))
    lines += ["", "Reply here and it goes straight into that session."]
    return "\n".join(lines)


def page_done(card: dict, *, crown: dict, ledger: Path, send=None,
              reply_socket: str | None = None) -> notify.Sent:
    """Buzz him when the session he talks to finishes a run.

    Narrow on purpose, and the narrowing is the design: per the working
    agreement he talks to ONE session and it runs the rest, so that session
    going DONE is a run of HIS finishing. Forty desks finishing forty slices is
    the manager's business, not his.

    Guarded per TRANSITION, not per session. A DONE session stays DONE until
    something speaks to it and the collector re-reads at 1 Hz, so a per-session
    guard would send one message a second; a guard that never clears would tell
    him about the first run and no other.
    """
    session_id = str(card.get("session_id") or "")
    if not session_id or session_id != str((crown or {}).get("session_id") or ""):
        return notify.Sent(True, 0, "not the manager")

    key = f"done:{session_id}"
    if str(card.get("state") or "") != "DONE":
        _forget(key, ledger)
        return notify.Sent(True, 0, "not finished")

    return _page(key, compose_done(card), ledger=ledger, send=send,
                 address=_address(reply_socket, session_id))


# ── inbound ─────────────────────────────────────────────────────────────────


def parse_frame(raw: str) -> str | None:
    """The words he typed, out of the frame `src/inject.js` writes.

    One compact JSON object per line: `{"type":"user","message":{"role":"user",
    "content":"<prefix>\\n<his text>"}}`. Anything else on this socket is not an
    answer -- returns None rather than raising, because the socket is reachable
    by any local process and a malformed write must not take the deck down.
    """
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "user":
            continue
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        body = content
        if body.startswith(_BRIDGE_PREFIX):
            _, _, body = body.partition("\n")
        body = body.strip()
        if body:
            return body
    return None


@dataclass(frozen=True)
class Resolution:
    """Which question his words settle, or which desk they are addressed to.

    `desk` defaults, so every existing caller and test that builds or reads a
    Resolution keeps working unchanged. Exactly one of `ask_id` and `desk` is
    ever set: an answer settles a question OR speaks to a desk, and a value in
    both would leave the caller to guess which it was.
    """
    ask_id: str | None
    reply: str | None
    refusal: str | None
    desk: str | None = None


#: `Name: text`, with the colon doing the work. It is what makes a desk called
#: "drift watch" parseable at all -- splitting on whitespace cannot tell a
#: two-word name from a name followed by an instruction. A leading `@` is
#: optional and stripped, because that is what he types out of habit.
_ADDRESSED = re.compile(r"^\s*@?\s*([^:\n]{1,60}?)\s*:\s*(.*)$", re.DOTALL)

#: `@Name text`, no colon. Only ever tried when the text opens with `@`, which
#: is unambiguous enough to be worth accepting without punctuation.
_AT_ADDRESSED = re.compile(r"^\s*@\s*(.+)$", re.DOTALL)


def _norm(name: str) -> str:
    """One spelling of a name, for comparison only. PURE.

    Case-folded and inner whitespace collapsed, because he types this
    one-handed on a phone and "  INITECH " is the same desk as "Initech".
    Never used for display -- what he is shown is always the roster's own
    spelling, so he learns the real name.
    """
    return " ".join(str(name or "").split()).casefold()


def match_desk(name: str, desks: list) -> tuple:
    """(desk, candidates). PURE.

    An EXACT name wins outright and is never called ambiguous -- "Product" is
    a prefix of "Producer", and refusing the exact name because a longer one
    starts with it would make the shorter desk unreachable forever.

    Otherwise a unique prefix wins, because a phone keyboard is why he typed
    three letters. Two or more matches return no desk and the candidates, so
    the caller can name them back at him rather than guessing -- a guess puts
    his instruction on the wrong desk and he never finds out.
    """
    wanted = _norm(name)
    if not wanted:
        return None, []
    known = [d for d in (desks or []) if str(d or "").strip()]
    for desk in known:
        if _norm(desk) == wanted:
            return desk, [desk]
    hits = [d for d in known if _norm(d).startswith(wanted)]
    if len(hits) == 1:
        return hits[0], hits
    return None, hits


def _to_desk(body: str, desks: list) -> Resolution | None:
    """Is this addressed to a desk? PURE. None when it is not for us.

    Returns a Resolution -- routed or refused -- for anything that LOOKS
    addressed, and None for everything else, so ordinary conversation falls
    through to the question-answering path exactly as it always has.
    """
    if not desks:
        return None

    name = reply = None
    match = _ADDRESSED.match(body)
    if match:
        name, reply = match.group(1), match.group(2)
    elif _AT_ADDRESSED.match(body):
        # `@Name some words`. The name is whichever known desk the text opens
        # with -- longest first, so "@drift watch go" is the two-word desk and
        # not a one-word desk called "drift" that happens to share a prefix.
        rest = _AT_ADDRESSED.match(body).group(1)
        for desk in sorted(desks, key=lambda d: -len(str(d))):
            if _norm(rest).startswith(_norm(desk)):
                name, reply = desk, rest[len(str(desk)):]
                break
        if name is None:
            # No known desk opens this. Take the first word as the name
            # anyway, so he gets "no desk called 'X'" instead of silence.
            head, _, tail = rest.partition(" ")
            name, reply = head, tail
    if name is None:
        return None

    desk, candidates = match_desk(name, desks)
    shown = " ".join(str(name).split())
    if desk is None:
        if len(candidates) > 1:
            return Resolution(None, None,
                              f"{shown!r} could be "
                              f"{' or '.join(candidates)} — say which",
                              None)
        return Resolution(None, None,
                          f"no desk called {shown!r} — you can reach: "
                          + ", ".join(str(d) for d in desks), None)

    said = (reply or "").strip()
    if not said:
        return Resolution(None, None,
                          f"nothing to send to {desk} — say what you want it "
                          "to do", None)
    return Resolution(None, said, None, desk)


def resolve(text: str, pending: list, desks: list | None = None) -> Resolution:
    """Attribute a reply to exactly one open question, or to one desk. PURE.

    `desks` is optional and defaults to none, so the two-argument call this
    has always had behaves exactly as it did -- there is a test that says so.

    ORDER MATTERS. An open ask id is checked BEFORE any desk name. A question
    he was paged about is the thing in front of him, and routing `Product:
    once` to a desk while an ask called `Product` sat open would leave an agent
    parked with him believing he had freed it.

    Answering the newest is the bug this exists to avoid: he is paged about
    one thing, another blocks while his thumb is moving, and his "1" would
    approve an action he was never shown. So a bare word settles a question
    only when there is exactly one open; otherwise it refuses and lists the
    ids, which is a sentence he can act on.

    `asking.parse_reply` is the vocabulary, unchanged and strict -- it already
    accepts a leading ask id and already refuses ordinary chat that merely
    contains the word "never".
    """
    body = (text or "").strip()
    if not body:
        return Resolution(None, None, None)

    open_asks = list(pending or [])
    head = body.split()[0].rstrip(":").lower()
    named = next((a for a in open_asks if a.id.lower() == head), None)

    if named is None:
        # After the ask lookup, before the silence. A desk name that matches
        # nothing must ANSWER him -- dropping it silently is the worst outcome
        # on this channel, because he goes on believing the desk was told.
        addressed = _to_desk(body, desks or [])
        if addressed is not None:
            return addressed

    if named is None and asking.parse_reply(body) is None:
        # Not an answer at all -- ordinary conversation. Silent: refusing every
        # chat message would make this channel unusable for talking.
        return Resolution(None, None, None)

    if named is not None:
        reply = body.split(None, 1)[1].strip() if len(body.split(None, 1)) > 1 else ""
        if asking.parse_reply(f"{named.id} {reply}") is None:
            return Resolution(None, None,
                              f"{reply!r} is not once, always or never "
                              f"(ask {named.id})")
        return Resolution(named.id, reply, None)

    # A bare once/always/never, or a bare 1/2/3.
    if len(body.split()) > 1 or ":" in body:
        # It led with something id-shaped that names nothing open.
        return Resolution(None, None,
                          f"no open question {head!r} — "
                          + (f"open: {', '.join(a.id for a in open_asks)}"
                             if open_asks else "nothing is waiting on you"))
    if not open_asks:
        return Resolution(None, None, "nothing is waiting on you")
    if len(open_asks) > 1:
        ids = ", ".join(a.id for a in open_asks)
        return Resolution(None, None,
                          f"{len(open_asks)} questions are open — "
                          f"say which: {ids}")
    return Resolution(open_asks[0].id, body, None)


def _alive(path: Path) -> bool:
    """True when some process is accepting on the UNIX socket at `path`."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class Listener:
    """A UNIX socket in the Claude socket directory, so the bridge can reach us.

    Not a general server: it accepts a connection, reads until the far end
    half-closes, hands the text to `on_text` and closes. That is the whole of
    what `src/inject.js` does to a session socket.

    Every path swallows its exception. This runs in a daemon thread inside the
    deck; a hostile or malformed write on a locally-reachable socket must
    degrade the reply path, never take the board down.
    """

    def __init__(self, path, on_text, backlog: int = 8) -> None:
        self.path = Path(path)
        self._on_text = on_text
        self._backlog = backlog
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: True only once THIS listener bound the path; stop() unlinks only then,
        #: so a refused listener never removes a live owner's socket.
        self._owned = False

    def start(self) -> bool:
        """Bind and serve. False when the socket could not be created."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A crash leaves the inode behind and bind() then fails
            # EADDRINUSE, which would bring the deck up healthy with no reply
            # path -- the silent failure this module exists to remove.
            #
            # But only a DEAD one. A second deck on the same account -- or a
            # test run -- used to unlink the owner's LIVE socket here and bind
            # its own, so his replies went to whoever started last (measured
            # 2026-09-30: a pytest run held it). A socket that accepts a
            # connection belongs to a live process and is left alone.
            if self.path.exists() or self.path.is_symlink():
                if _alive(self.path):
                    return False
                self.path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.path))
            sock.listen(self._backlog)
            sock.settimeout(0.5)
            # Owner only. Anyone who can write here can put a user turn into
            # the deck's answer path.
            os.chmod(self.path, 0o600)
        except OSError:
            return False

        self._sock = sock
        self._owned = True
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="deck-phone-listener")
        self._thread.start()
        return True

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                chunks = []
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                        if sum(len(c) for c in chunks) > 256 * 1024:
                            break
                except OSError:
                    pass
            text = parse_frame(b"".join(chunks).decode("utf-8", "replace"))
            if text is None:
                continue
            try:
                self._on_text(text)
            except Exception:  # a bad answer must not kill the loop
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        if not self._owned:
            return
        self._owned = False
        try:
            self.path.unlink()
        except OSError:
            pass
