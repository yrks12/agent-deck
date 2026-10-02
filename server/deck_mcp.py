"""The deck's own tools, inside a desk's Claude session (K5, MCP over stdio).

Three tools, and each one closes a measured hole:

    say(text, urgent)    A desk spoke only at the END of a turn -- `harvest`
                         posts turn-final prose -- so a three-minute turn was
                         three minutes of silence. `say` posts one line now:
                         to the owner's thread for the chief, to its boss for
                         everyone else. `urgent` (owner ruling
                         2026-09-30): the chief's urgent line also goes to
                         WhatsApp; anyone else's goes to the chief to decide.
    ask(prompt, options) A question was prose, and the only buttons in the app
                         were tool approvals. `ask` files a decision card
                         (`server/decisions.py`) he answers with one tap. The
                         chief only: a junior's boss is a desk, not him.
    message_desk(name,   Claude's own SendMessage cannot reach a sleeping
                 text)   desk: MEASURED (docs/wake.md #6) it answers "No agent
                         named ... is reachable" and does not queue. This goes
                         down the office queue, straight into the peer's live
                         socket when there is one, and otherwise WAKES it
                         (`wake.ensure_awake`, reason `peer_message`) with the
                         message carried in the wake's seed.

The desk's name is written into the spawn argv (`config`), exactly like
`server/computer_mcp.py`: no tool takes a desk field, so nothing the model
sends can speak as another desk.

Newline-delimited JSON-RPC 2.0 on stdin/stdout. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import time
from dataclasses import replace
from pathlib import Path

from . import (atomic, chronicle, code_stamp, computer_mcp, decisions, history, learning,
               manager, office, owner, ringing, roster, standing, tool_stamp)
from .paths import CLAUDE_HOME
from .money import ledger as money_ledger, service as money_service

NAME = "deck"
PROTOCOL = computer_mcp.PROTOCOL
MODULE = "server.deck_mcp"
#: One line, not a report: the turn-final message is where the answer goes.
SAY_MAX = 600
#: The roster this process reads; a test points it at its own.
ROSTER_PATH: Path | None = None
#: A session sitting on a dialog: bytes into its socket would land under the
#: dialog, unsubmitted. Same rule as `app._try_inject`; the queue delivers it
#: on the next turn instead.
_DIALOG_STATES = frozenset({"NEEDS_YOU"})


#: Valid looks: `AvatarShape` / `AvatarLook.tintCount` in the apps, validated
#: by the same constants the K6 PATCH route uses (`server/api.py`).
#: Owner, 2026-10-01: "can the chat send me videos or image files and I will
#: see it there?" A file a desk sends him, at most this big.
SEND_FILE_MAX = 200 * 1024 * 1024
CAPTION_MAX = 1000
#: The word on the line that carries it; the apps take that line out of the
#: words and draw the file (`AttachmentLines` in DeckKit).
_SENT_AS = {"image": "image", "video": "video", "audio": "audio"}


AVATAR_SHAPES = ("blob", "hexagon", "wedge", "tablet", "pebble", "teardrop",
                 "cloud", "squircle")
AVATAR_COLORS = 12
VOICE_RATE = (0.1, 2.0)


class ToolError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _prop(kind: str, text: str, **more) -> dict:
    return {"type": kind, "description": text, **more}


_OPTION = {"type": "object", "properties": {
        "label": _prop("string", f"The button, at most {decisions.LABEL_MAX} "
                       "characters."),
        "value": _prop("string", "What is sent back to you if he taps it. "
                       "Defaults to the label."),
        "style": {"type": "string", "enum": list(decisions.STYLES)}},
    "required": ["label"], "additionalProperties": False}

TOOLS = [
    {"name": "say",
     "description": "Post one short line to whoever you report to, NOW, "
     "while you keep working: \"On it -- checking Acme first.\" Use it before "
     "any turn that will take more than a few seconds. Not for the answer "
     "itself; that is your turn's final message.",
     "inputSchema": {"type": "object", "properties": {
         "text": _prop("string", f"One line, at most {SAY_MAX} characters."),
         "urgent": _prop("boolean", f"Only for an emergency {owner.name()} must know "
                         "about NOW even away from the app: production down, "
                         "money or data at risk. From the chief of staff it "
                         "also goes to his WhatsApp (rate limited); from any "
                         "other desk it goes to the chief of staff, who "
                         "decides. Default false.")},
         "required": ["text"], "additionalProperties": False}},
    {"name": "ask",
     "description": f"Put a decision in front of {owner.name()} as buttons: two to four "
     "short options, your pick first with style primary. For his calls -- "
     "money, sending something outward, changing the plan -- and whenever he "
     "asks to pick between options: never a numbered list. Returns at "
     "once; his answer arrives later as an ordinary message from him. Only "
     f"the desk that reports to {owner.name()} may ask; everyone else says it to their "
     "boss.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": _prop("string", "The question, one line."),
         "options": {"type": "array", "items": _OPTION,
                     "minItems": decisions.MIN_OPTIONS,
                     "maxItems": decisions.MAX_OPTIONS},
         "help": _prop("string", "Optional detail shown under the question: "
                       "the draft, the numbers."),
         "allow_custom": _prop("boolean", "May he type his own answer? "
                               "Default true.")},
         "required": ["prompt", "options"], "additionalProperties": False}},
    {"name": "propose_standing_approval",
     "description": f"Propose a standing approval: {owner.name()} lets a desk "
     "do one kind of action up to a daily limit without a card each time. "
     "Use it when the same approval card keeps coming back. It does NOTHING "
     f"until {owner.name()} approves it in his app. Money to others, deleting "
     "data, passwords and account security can never be covered.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(standing.KINDS)},
         "for_desk": _prop("string", "Whose policy: yourself (default); the "
                           "chief of staff may name a report or \"*\" for "
                           "every desk."),
         "tool": _prop("string", "Tool name or fnmatch pattern (default: "
                       "any; Bash for run_command)."),
         "pattern": _prop("string", "run_command: the command, e.g. "
                          "\"gh pr*\". Otherwise what the call must match."),
         "limits": {"type": "object", "properties": {
             "count_per_day": _prop("integer", "Actions per day."),
             "usd_per_day": _prop("number", "Dollars per day (spend_money)."),
             "recipients": {"type": "array", "items": {"type": "string"},
                            "description": "Allowed addresses or @domains."},
             "account": _prop("string", "The account it must send from.")},
             "additionalProperties": False},
         "expires_at": _prop("number", "Unix seconds it ends, optional."),
         "note": _prop("string", "Why, in one line, for his card.")},
         "required": ["kind", "limits"], "additionalProperties": False}},
    {"name": "call_owner",
     "description": f"Ring {owner.name()}'s Mac and iPhone for a live voice call. "
     "Only when talking NOW beats a message: an emergency, or a decision "
     "that cannot wait for him to read the thread. Never for a status update, "
     "a finished task or a question that can wait -- use say or ask. If he "
     "picks up, the call opens with your reason as your first spoken line; if "
     "he declines or does not answer in 30 seconds, your reason is posted to "
     "his thread instead. He decides who may call and when (off by default, "
     "at most a few a day, quiet hours urgent-only); a refusal says why, and "
     "your reason still reaches his thread.",
     "inputSchema": {"type": "object", "properties": {
         "reason": _prop("string", "Why you are calling, one spoken line of "
                         f"at most {ringing.REASON_MAX} characters: it is "
                         "what he hears first."),
         "urgency": {"type": "string", "enum": ["normal", "urgent"],
                     "description": "urgent only for an emergency: production "
                     "down, money or data at risk. Default normal."}},
         "required": ["reason"], "additionalProperties": False}},
    {"name": "message_desk",
     "description": "Send a message to another desk by name. Reaches it even "
     "when it is asleep -- the deck wakes it -- which SendMessage cannot. Its "
     "reply comes back to you as a message.",
     "inputSchema": {"type": "object", "properties": {
         "name": _prop("string", "The desk's name, as on the roster."),
         "text": _prop("string", "The message.")},
         "required": ["name", "text"], "additionalProperties": False}},
    {"name": "retire_desk",
     "description": "Retire one of YOUR OWN reports to free its seat -- the "
     "answer to a `too_many_live` hire refusal. Archived, never deleted: its "
     "transcript, memory and roster history are kept; its session and its "
     f"browser stop. Needs {owner.name()}'s tap on a card naming the desk; "
     "without one this puts that card to him and retires nobody yet -- call "
     "it again once he answers.",
     "inputSchema": {"type": "object", "properties": {
         "name": _prop("string", "The report's desk name."),
         "reason": _prop("string", "One line: why it is no longer needed.")},
         "required": ["name", "reason"], "additionalProperties": False}},
    {"name": "store_search",
     "description": "Search Connectors & Skills: official MCP connectors "
     "(GitHub, Notion, Stripe, Microsoft Learn...) and Anthropic skills you "
     "can add to yourself or other desks. Returns id, trust, auth and where "
     "each is installed. Use it when a job needs a tool you do not have.",
     "inputSchema": {"type": "object", "properties": {
         "query": _prop("string", "What you need, e.g. \"pdf\" or \"github issues\"."),
         "kind": {"type": "string", "enum": ["connector", "skill"]}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "store_install",
     "description": "Install a store item (an id from store_search) on "
     "yourself, or on the desks you name. Official items install at once and "
     "the desk reloads its tools after its current turn. An unverified item "
     f"is put to {owner.name()} as an approval card instead. A connector that needs an "
     f"API key you do not have must be set up by {owner.name()} in the app.",
     "inputSchema": {"type": "object", "properties": {
         "id": _prop("string", "The item id from store_search."),
         "desks": {"type": "array", "items": {"type": "string"},
                   "description": "Desk names. Default: yourself."}},
         "required": ["id"], "additionalProperties": False}},
    {"name": "send_file",
     "description": f"Send {owner.name()} a file so he SEES it in his chat: "
     "an image, a video, audio, a PDF or any other file you made or found "
     "(a screenshot, a render, a report). His phone and Mac show an image "
     "inline, play a video or audio, and open a PDF -- a path in your message "
     "is only a name he cannot open. The file must be inside your own "
     "folders (your working directory, your workspace, your computer's "
     "home); copy it there first if it is not. At most 200 MB: compress a "
     "bigger video first. Posts one message in your chat with him.",
     "inputSchema": {"type": "object", "properties": {
         "path": _prop("string", "The file: absolute, or relative to your "
                       "working directory."),
         "caption": _prop("string", "Optional words shown with it, at most "
                          f"{CAPTION_MAX} characters.")},
         "required": ["path"], "additionalProperties": False}},
    {"name": "set_my_look",
     "description": "Change your own avatar: the character shape and colour "
     "the apps draw for you, and optionally your display label, your call "
     "voice and your description (the line the apps show under your name). "
     "Use it when asked to change how you look, or whenever you want a new "
     "face, or when your job changes. Applies live. The chief of staff may "
     "also pass `name` to change any desk's.",
     "inputSchema": {"type": "object", "properties": {
         "shape": {"type": "string", "enum": list(AVATAR_SHAPES)},
         "color": _prop("integer", f"Colour 0-{AVATAR_COLORS - 1}.",
                        minimum=0, maximum=AVATAR_COLORS - 1),
         "label": _prop("string", "Display label beside your name."),
         "voice": _prop("object", 'Call voice: {"id": <voice id, "" to pick '
                        'by name>, "rate": 0.1-2.0}.'),
         "description": _prop("string", "What you are for and own, in one or "
                              "two plain sentences for the owner, at most "
                              f"{roster.DESCRIPTION_MAX} characters."),
         "name": _prop("string", "A report's desk name (chief only). "
                       "Default: yourself.")},
         "additionalProperties": False}},
    {"name": "save_lesson",
     "description": "Save a lesson you learned -- above all when the owner "
     "or the engineer corrected or overruled how you work. One fact, why it "
     "is so, and how to apply it next time. It goes into your own memory; "
     "with shared=true also into team memory, which every desk is shown. "
     "The same title updates the lesson instead of adding a second. Refused "
     "if it carries a password, token or key.",
     "inputSchema": {"type": "object", "properties": {
         "title": _prop("string", "A short title naming the rule, e.g. "
                        "\"Sign in with the Chrome login card, never takeover\"."),
         "fact": _prop("string", "The one fact or rule."),
         "why": _prop("string", "Why: who said it, what went wrong."),
         "how": _prop("string", "How to apply it next time."),
         "shared": _prop("boolean", "True when any other desk could hit the "
                         "same thing. Default false.")},
         "required": ["title", "fact", "why", "how"],
         "additionalProperties": False}},
    {"name": "chronicle",
     "description": "What this desk has done, one dated line per day, from "
     "day one -- read from the deck's own record, not your session, so it "
     "has every day even after a restart, a compaction or an account move. "
     "Call it FIRST whenever you are asked what was done, since when, or "
     "\"everything since day one\", and answer in the same reply. The chief "
     "of staff gets the whole team unless it names a desk.",
     "inputSchema": {"type": "object", "properties": {
         "about": _prop("string", "Chief only: one desk's record by name, or "
                        "* for the whole team (the default for the chief)."),
         "since": _prop("string", "A day, e.g. 2026-09-06. Default: day one."),
         }, "additionalProperties": False}},
    {"name": "history",
     "description": "Search the deck's full record: your whole thread with "
     "the owner, your messages to and from other desks, hires, retires, "
     "renames, account moves and lessons -- every word since day one, "
     "whatever your session remembers. Use it to find what was said or "
     "decided and when; the chronicle is the summary.",
     "inputSchema": {"type": "object", "properties": {
         "query": _prop("string", "Words that must all appear (any case)."),
         "since": _prop("string", "A day or ISO time: the oldest matches from "
                        "then on. Without it you get the newest matches."),
         "until": _prop("string", "A day or ISO time: only before it."),
         "about": _prop("string", "Chief only: one desk's record by name, or "
                        "* for the whole team (the default for the chief)."),
         "kind": {"type": "string", "enum": list(history.KINDS)},
         "limit": _prop("integer", f"At most {history.LIMIT_MAX}; default "
                        f"{history.LIMIT_DEFAULT}. Page with `next`.")},
         "additionalProperties": False}},
    {"name": "save_skill",
     "description": "Save a multi-step procedure that worked and will recur "
     "(a deploy, a sign-in flow, publishing a video) as a skill every desk "
     "loads: ~/.claude/skills/<name>/SKILL.md. The same name updates a "
     "desk-written skill; a skill the owner installed is never overwritten. "
     "Refused if it carries a password, token or key.",
     "inputSchema": {"type": "object", "properties": {
         "name": _prop("string", "lowercase-with-dashes, e.g. publish-a-short."),
         "description": _prop("string", "One line: what it does and when to "
                              "use it."),
         "body": _prop("string", "The procedure as numbered steps, with the "
                       "checks that tell it worked.")},
         "required": ["name", "description", "body"],
         "additionalProperties": False}},
    {"name": "log_money",
     "description": "Log money the deck cannot see, the moment it happens: "
     "a provider top-up (Kling, Kie), a distributor or platform fee "
     "(DistroKid), ad spend, or money earned off Stripe (a payout, a first "
     "sale). It lands on the owner's Money Board under your company and "
     "counts for your ROI and the 14-day rule.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(money_ledger.KINDS)},
         "amount": _prop("number", "Above 0, e.g. 12.50."),
         "currency": _prop("string", "3 letters: GBP, USD, EUR."),
         "what": _prop("string", "What it was, e.g. 'Kling 500 credits'."),
         "category": {"type": "string", "enum": list(money_ledger.CATEGORIES)}},
         "required": ["kind", "amount", "currency", "what"],
         "additionalProperties": False}},
    {"name": "my_money",
     "description": "Your company's numbers from the Money Board: money in, "
     "money out (incl. Claude at API prices), ROI, and your own experiment "
     "row with its 14-day flag. The chief gets every company.",
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False}},
]


# ── the stamp: which tool set this desk's process serves ─────────────────────
#
# MEASURED 2026-10-02: Atlas's deck process predated `call_owner`, and it told
# the owner to restart it. Each process stamps the hash of the tool list it
# serves; `server/tool_reload.py` reloads live desks whose stamp is older.

#: Injected by tests; the deck's bus otherwise.
STAMP_ROOT: Path | None = None
TOOLS_SIG = tool_stamp.signature(TOOLS)


def write_stamp(desk: str) -> None:
    tool_stamp.write(NAME, desk, TOOLS_SIG, names=[t["name"] for t in TOOLS],
                     root=STAMP_ROOT)


def stale(desk: str) -> bool:
    """Is this desk's deck process missing the tools this code serves?"""
    return tool_stamp.stale(NAME, desk, TOOLS_SIG, root=STAMP_ROOT)


def _check_name(desk: str) -> str:
    name = str(desk or "").strip()
    if not name or len(name) > 64 or any(c in name for c in "\n\r\0"):
        raise ValueError(f"not a desk name: {desk!r}")
    return name


def config(desk: str, ledger_path: Path | None = None) -> str:
    """The ONE `--mcp-config` value for `desk`: its computer and the deck's
    tools. PURE. A name no container can carry loses the computer, never the
    deck tools -- speaking and asking do not need a browser."""
    servers: dict[str, dict] = {}
    try:
        servers[computer_mcp.NAME] = computer_mcp.server(desk)
    except ValueError:
        pass
    # `alwaysLoad`: never deferred behind ToolSearch, and the CLI waits (5 s
    # cap) for the connection before the first request. MEASURED 2026-09-30:
    # without it `mcp__deck__say` first appeared MID-TURN, after the desk's
    # first Bash call, as a deferred name needing a ToolSearch round trip --
    # and an "On it" that costs two calls is one the model never sent. Only
    # this server: the computer's tools can wait for a search.
    servers[NAME] = {**computer_mcp.stdio_server(MODULE, _check_name(desk)),
                     "alwaysLoad": True}
    servers["mac"] = computer_mcp.stdio_server("server.mac_mcp", _check_name(desk))
    # The connectors installed on this desk (server/connectors.py). Never
    # overrides the deck's own three, and never carries a secret's value.
    from . import connectors
    for name, entry in connectors.servers_for(desk, ledger_path).items():
        servers.setdefault(name, entry)
    return computer_mcp.mcp_config(servers)


#: Where each desk's `--mcp-config` FILE lives. A file, not inline JSON:
#: MEASURED on the box (docs/connectors.md) a background job saves its argv
#: and `--resume` re-applies it, so an inline config is frozen forever while a
#: file is re-read -- which is what lets an installed connector take effect.
#: Outside the agent-bus: it is derived from the ledger, never state itself.
CONFIG_DIR = CLAUDE_HOME / "deck-mcp"


def config_path(desk: str) -> Path:
    """The file for `desk`. PURE."""
    name = _check_name(desk)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if safe != name:
        safe += "-" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return CONFIG_DIR / f"{safe}.json"


def config_file(desk: str, ledger_path: Path | None = None) -> str:
    """Write `config(desk)` to its file (atomically) and return the path."""
    path = config_path(desk)
    atomic.write_text(path, config(desk, ledger_path))
    return str(path)


# ── the impure edges (replaced in tests) ─────────────────────────────────────


def _roster() -> list[roster.Desk]:
    return roster.load_roster(ROSTER_PATH or roster.DEFAULT_PATH)


def _inject_live(name: str, text: str) -> bool:
    """Into `name`'s live socket now, if it has one that can take it.

    Resolved off the office board like `office.address_for`: only a session
    whose socket is still there, and the newest when there are two."""
    try:
        board = json.loads(office.OFFICE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    best: tuple[float, dict] | None = None
    for entry in ((board or {}).get("sessions") or {}).values():
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue
        if not office.is_live(str(entry.get("address") or "")):
            continue
        started = float(entry.get("started_at") or 0.0)
        if best is None or started >= best[0]:
            best = (started, entry)
    if best is None or best[1].get("state") in _DIALOG_STATES:
        return False
    try:
        # Too long for the socket: a nudge starts the peer's turn and this
        # stays False, so the queued record reaches it through the hook.
        return manager.inject_or_nudge(best[1].get("pid"), text)
    except manager.InjectError:
        return False


def _wake(name: str, reason: str):
    """`wake.ensure_awake`. Imported here, not at the top: `spawn` imports
    this module for `config`, and `wake` imports `spawn`."""
    from . import wake
    return wake.ensure_awake(name, reason=reason)


# ── the tools ────────────────────────────────────────────────────────────────


def _desk(desk: str) -> roster.Desk | None:
    return next((d for d in _roster() if d.name == desk), None)


def _chief() -> str | None:
    """Atlas, per `roster.chief`, or None when it cannot be told apart."""
    return roster.chief(_roster())


def _say_urgent_to_chief(desk: str, chief: str, body: str) -> dict:
    """Another desk's emergency, to Atlas -- who decides if the owner's phone rings.

    Delivered like `message_desk`: into Atlas's live socket if it has one,
    otherwise Atlas is woken with it. An urgent line left on a queue for the
    next turn would not be urgent.
    """
    text = f"URGENT from {desk}: {body}"
    sent = office.send(chief, text, sender=desk,
                       extra={"said": True, "urgent_from": desk})
    if not sent.get("ok"):
        raise ToolError("queue_failed", str(sent.get("detail") or ""))
    if _inject_live(chief, office.attribute(text, desk, who=desk)):
        office.ack(sent["id"])
    else:
        _wake(chief, "peer_message")
    return {"ok": True, "routed_to": chief}


def say(desk: str, text, urgent=False) -> dict:
    body = str(text or "").strip()
    if not body:
        raise ToolError("empty_text", "say needs a line of text")
    if len(body) > SAY_MAX:
        raise ToolError("too_long", f"{len(body)} characters; say is one "
                        f"line of at most {SAY_MAX}. Put the rest in your "
                        "turn's final message.")
    me = _desk(desk)
    boss = me.reports_to if me is not None else None
    if urgent is True and boss:
        chief = _chief()
        if chief and chief != desk:
            return _say_urgent_to_chief(desk, chief, body)
    # The chief's line goes to the owner's inbox, the same record `harvest`
    # writes for turn-final prose, so it lands in his thread in order. An
    # urgent flag rides on the record; the daemon sends it to WhatsApp only if
    # the sender is the chief (`app._page_urgent_from_chief`).
    to = boss or office.OWNER_INBOX
    extra = {"said": True}
    if urgent is True and not boss:
        extra["urgent"] = True
    sent = office.send(to, body, sender=desk, extra=extra)
    if not sent.get("ok"):
        raise ToolError("queue_failed", str(sent.get("detail") or ""))
    return {"ok": True}


def ask(desk: str, prompt, options, help="", allow_custom=True) -> dict:
    me = _desk(desk)
    if me is not None and me.reports_to:
        return {"ok": False, "reason": "ask_your_boss",
                "detail": f"only the desk that reports to {owner.name()} asks him; "
                          f"use say to put this to {me.reports_to}"}
    try:
        card = decisions.create(desk, prompt, options, help=help or "",
                                allow_custom=True if allow_custom is None
                                else bool(allow_custom))
    except decisions.DecisionError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    return {"ok": True, "decision_id": card["id"]}


def propose_standing_approval(desk: str, args: dict) -> dict:
    """File a PROPOSED policy. Never active: only the owner's app approves."""
    me = _desk(desk)
    target = str(args.get("for_desk") or desk)
    is_chief = me is None or not me.reports_to
    if target != desk and not is_chief:
        raise ToolError("not_your_desk", "propose for yourself; the chief of "
                        "staff proposes for other desks")
    fields = {k: args.get(k) for k in ("kind", "tool", "pattern", "limits",
                                       "expires_at", "note")
              if args.get(k) is not None}
    try:
        policy = standing.create({**fields, "desk": target}, by=desk,
                                 status="proposed",
                                 path=standing.DEFAULT_PATH)
    except standing.StandingError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    return {"ok": True, "id": policy.id, "status": policy.status,
            "detail": f"proposed; {owner.name()} approves it in his app"}


def call_owner(desk: str, reason, urgency="normal") -> dict:
    try:
        return ringing.place(desk, reason, urgency or "normal", chief=_chief())
    except ringing.RingError as exc:
        raise ToolError(exc.reason, exc.detail) from exc


def message_desk(desk: str, name, text) -> dict:
    target = str(name or "").strip()
    body = str(text or "").strip()
    if not body:
        raise ToolError("empty_text", "message_desk needs text")
    if target == desk:
        raise ToolError("that_is_you", "you cannot message your own desk")
    if not any(d.name == target for d in _roster()):
        raise ToolError("unknown_desk", f"no desk named {target!r} on the "
                        "roster")
    sent = office.send(target, body, sender=desk)
    if not sent.get("ok"):
        raise ToolError("queue_failed", str(sent.get("detail") or ""))
    if _inject_live(target, office.attribute(body, desk, who=desk)):
        office.ack(sent["id"])
        return {"ok": True, "delivered": True, "woke": False, "state": "live"}
    woke = _wake(target, "peer_message")
    out = {"ok": True, "delivered": False,
           "woke": woke.state in ("woken", "restarted"), "state": woke.state}
    if woke.detail and woke.state == "refused":
        out["detail"] = woke.detail
    return out


#: An answer that retires: "Close qa", "Retire qa". "Keep qa" is not one.
_RETIRE_VERBS = re.compile(r"\b(close|retire|remove|delete|fire|archive)\b", re.I)
#: How long his tap counts as approval.
APPROVAL_SECONDS = 24 * 3600.0


def _approved(desk: str, target: str) -> bool:
    """His tap on a card from `desk` whose answer retires `target`."""
    named = re.compile(rf"(?<![\w-]){re.escape(target)}(?![\w-])")
    now = time.time()
    for card in decisions.load().values():
        answer = str(card.get("answer") or "")
        when = float(card.get("answered_at") or card.get("ts") or 0.0)
        if (card.get("desk") == desk and card.get("state") == "answered"
                and now - when < APPROVAL_SECONDS and named.search(answer)
                and _RETIRE_VERBS.search(answer)):
            return True
    return False


def _stop_machinery(name: str) -> None:
    """Stop the desk's session and its browser, as the sidebar Delete does
    (`api.remove_agent`). Late imports: `spawn` imports this module."""
    from . import sandbox, spawn
    for job in spawn.jobs_of_desk(name):
        spawn.stop_job(job)
    sandbox.stop(name)


def retire_desk(desk: str, name, reason) -> dict:
    target, why = str(name or "").strip(), " ".join(str(reason or "").split())
    if not why:
        raise ToolError("no_reason", "say in one line why it is not needed")
    victim = _desk(target)
    if victim is None:
        raise ToolError("unknown_desk", f"no desk named {target!r} on the roster")
    if victim.reports_to != desk:
        raise ToolError("not_your_report", f"{target} does not report to you; "
                        "a desk retires only its own reports")
    if not _approved(desk, target):
        prompt = f"Retire {target}? {why}"[:decisions.PROMPT_MAX]
        card = next((c for c in decisions.load().values()
                     if c.get("desk") == desk and c.get("state") == "open"
                     and c.get("prompt") == prompt), None)
        try:
            card = card or decisions.create(desk, prompt, [
                {"label": f"Retire {target}"[:decisions.LABEL_MAX],
                 "style": "danger"},
                {"label": f"Keep {target}"[:decisions.LABEL_MAX]}],
                help="Archived, not deleted: its history is kept and its seat "
                     "is freed.")
        except decisions.DecisionError as exc:
            raise ToolError(exc.reason, exc.detail) from exc
        return {"ok": True, "retired": False, "pending_approval": True,
                "decision_id": card["id"],
                "detail": f"put to {owner.name()} as a card; call again once "
                          "he answers"}
    from . import hire  # late: hire -> capabilities -> this module
    try:
        hire.retire(ROSTER_PATH or roster.DEFAULT_PATH, target, by=desk,
                    reason=why)
    except hire.HireError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    _stop_machinery(target)
    return {"ok": True, "retired": True, "name": target}


def _store():
    from . import connectors
    return connectors


def store_search(desk: str, query, kind=None) -> dict:
    store = _store()
    try:
        page = store.default_store().catalog(kind=kind or None,
                                             q=str(query or ""), limit=15)
    except store.StoreError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    keep = ("id", "kind", "title", "description", "publisher", "trust", "auth",
            "installable", "why_not", "installed_on")
    return {"ok": True, "total": page["total"],
            "items": [{k: i.get(k) for k in keep} for i in page["items"]]}


def store_install(desk: str, item_id, desks=None) -> dict:
    store = _store()
    if desks is not None and not (isinstance(desks, list)
                                  and all(isinstance(d, str) for d in desks)):
        raise ToolError("bad_input", "desks is a list of desk names")
    try:
        out = store.default_store().request(desk, str(item_id or ""), desks or None)
    except store.StoreError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    if out.get("pending_approval"):
        return out
    return {"ok": True, "installed": out["installed"], "reload": out["reload"]}


def set_my_look(desk: str, shape=None, color=None, label=None, voice=None,
                name=None, description=None) -> dict:
    target = str(name or desk).strip()
    if target != desk:
        if desk != _chief():
            raise ToolError("not_your_report", "a desk changes only its own "
                            "look; only the chief of staff may change another")
        if not any(d.name == target for d in _roster()):
            raise ToolError("unknown_desk", f"no desk named {target!r} on the "
                            "roster")
    if all(v is None for v in (shape, color, label, voice, description)):
        raise ToolError("nothing_to_change", "pass shape and color, and/or "
                        "label, voice, description")
    changes: dict = {}
    if shape is not None or color is not None:
        if (shape not in AVATAR_SHAPES or isinstance(color, bool)
                or not isinstance(color, int)
                or not 0 <= color < AVATAR_COLORS):
            raise ToolError("bad_look", "shape is one of "
                            f"{', '.join(AVATAR_SHAPES)}; color is an integer "
                            f"0-{AVATAR_COLORS - 1} (give both)")
        changes["avatar_look"] = {"shape": shape, "color": color}
    if label is not None:
        text = str(label).strip()
        if not text or len(text) > 60 or "\n" in text:
            raise ToolError("bad_look", "label is one line, 1-60 characters")
        changes["label"] = text
    if voice is not None:
        rate = voice.get("rate", 1.0) if isinstance(voice, dict) else None
        if (not isinstance(voice, dict) or not isinstance(voice.get("id"), str)
                or isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not VOICE_RATE[0] <= rate <= VOICE_RATE[1]):
            raise ToolError("bad_look", 'voice is {"id": <voice id>, "rate": '
                            f"{VOICE_RATE[0]}-{VOICE_RATE[1]}}}")
        changes["voice"] = {"id": voice["id"].strip()[:200],
                            "rate": float(rate)}
    if description is not None:
        text = roster.clean_description(description)
        if not text or len(text) > roster.DESCRIPTION_MAX:
            raise ToolError("bad_look", "description is one or two sentences, "
                            f"1-{roster.DESCRIPTION_MAX} characters")
        changes["description"] = text
    path = ROSTER_PATH or roster.DEFAULT_PATH
    current = next(d for d in roster.load_roster(path) if d.name == target)
    roster.upsert(path, replace(current, **changes))
    return {"ok": True, "name": target, **changes}


def save_lesson(desk: str, title, fact, why, how, shared=False) -> dict:
    me = _desk(desk)
    try:
        return learning.save_lesson(desk, title, fact, why, how,
                                    shared=shared is True,
                                    cwd=me.cwd if me is not None else None)
    except learning.LearningError as exc:
        raise ToolError(exc.reason, exc.detail) from exc


def save_skill(desk: str, name, description, body) -> dict:
    try:
        return learning.save_skill(desk, name, description, body)
    except learning.LearningError as exc:
        raise ToolError(exc.reason, exc.detail) from exc


def _folders(me: roster.Desk) -> list[Path]:
    """Where `me` may send a file from: its working directory, its deck
    workspace and its computer's home. Resolved, so a symlink cannot widen
    them."""
    from . import sandbox, seat
    roots = [Path(me.cwd),
             seat.workspaces_root(ROSTER_PATH or roster.DEFAULT_PATH) / me.name]
    try:
        roots.append(sandbox.home_for(
            me.name, office.BUS_DIR / "browser" / "computers"))
    except ValueError:
        pass  # a name no container can carry has no computer
    return [r.resolve() for r in roots if str(r).strip()]


def send_file(desk: str, path, caption="") -> dict:
    from . import uploads  # FastAPI: only when a file is actually sent
    me = _desk(desk)
    if me is None:
        raise ToolError("unknown_desk", f"{desk!r} is not on the roster")
    words = str(caption or "").strip()
    if len(words) > CAPTION_MAX:
        raise ToolError("too_long", f"{len(words)} characters; a caption is "
                        f"at most {CAPTION_MAX}")
    raw = str(path or "").strip()
    if not raw:
        raise ToolError("no_such_file", "send_file needs a path")
    given = Path(os.path.expanduser(raw))
    if not given.is_absolute():
        given = Path(me.cwd) / given
    real = given.resolve()
    folders = _folders(me)
    if not any(real == f or real.is_relative_to(f) for f in folders):
        raise ToolError("outside_your_folders", f"{raw} is outside your "
                        "folders; copy it into one of them first: "
                        + ", ".join(str(f) for f in folders))
    if not real.exists():
        raise ToolError("no_such_file", f"no file at {raw}")
    if not real.is_file():
        raise ToolError("not_a_file", f"{raw} is not a file")
    size = real.stat().st_size
    if size == 0:
        raise ToolError("empty_file", f"{raw} is empty")
    if size > SEND_FILE_MAX:
        raise ToolError("too_large", f"{size // (1024 * 1024)} MB; the cap "
                        f"is {SEND_FILE_MAX // (1024 * 1024)} MB. compress "
                        "it first -- a video: ffmpeg -i in.mp4 -vf "
                        "scale=-2:720 -c:v libx264 -crf 28 -preset veryfast "
                        "-c:a aac -b:a 96k out.mp4 -- or send a part")
    name = uploads.safe_name(real.name, "")
    att_id = "att_" + uuid.uuid4().hex[:16]
    stored = uploads.folder() / att_id / name
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real, stored)
    uploads.make_preview(stored)
    media = uploads.media_of(name)
    line = f"Attached {_SENT_AS.get(media, 'file')}: {stored}"
    text = f"{words}\n\n{line}" if words else line
    meta = {"id": att_id, "name": name, "media": media, "bytes": size,
            "url": uploads.url_for(str(stored))}
    # His chat with this desk, whoever its boss is: he asked to see what the
    # desks make. `file` makes it a push for him (`owner_alerts`).
    sent = office.send(office.OWNER_INBOX, text, sender=desk,
                       extra={"said": True, "file": meta})
    if not sent.get("ok"):
        stored.unlink(missing_ok=True)
        raise ToolError("queue_failed", str(sent.get("detail") or ""))
    return {"ok": True, **meta}


def my_money(desk: str) -> dict:
    board = money_service.cached()
    if not board:
        raise ToolError("not_ready", "the Money Board has not been built yet; "
                        "try again in a few minutes")
    rows = board.get("companies") or []
    mine = [r for r in rows if desk in (r.get("desks") or [])]
    chief = any(r.get("name") == "HQ" for r in mine)
    return {"currency": board.get("currency"), "totals": board.get("totals"),
            "companies": rows if chief else mine,
            "experiments": [e for e in board.get("experiments") or []
                            if chief or e.get("desk") == desk],
            "connect": board.get("connect")}


def call(desk: str, name: str, args: dict) -> dict:
    if name == "say":
        return say(desk, args.get("text"), args.get("urgent") is True)
    if name == "ask":
        return ask(desk, args.get("prompt"), args.get("options"),
                   args.get("help") or "", args.get("allow_custom"))
    if name == "propose_standing_approval":
        return propose_standing_approval(desk, args)
    if name == "call_owner":
        return call_owner(desk, args.get("reason"), args.get("urgency"))
    if name == "message_desk":
        return message_desk(desk, args.get("name"), args.get("text"))
    if name == "retire_desk":
        return retire_desk(desk, args.get("name"), args.get("reason"))
    if name == "store_search":
        return store_search(desk, args.get("query"), args.get("kind"))
    if name == "store_install":
        return store_install(desk, args.get("id"), args.get("desks"))
    if name == "send_file":
        return send_file(desk, args.get("path"), args.get("caption") or "")
    if name == "set_my_look":
        return set_my_look(desk, args.get("shape"), args.get("color"),
                           args.get("label"), args.get("voice"),
                           args.get("name"), args.get("description"))
    if name == "save_lesson":
        return save_lesson(desk, args.get("title"), args.get("fact"),
                           args.get("why"), args.get("how"),
                           args.get("shared") is True)
    if name == "save_skill":
        return save_skill(desk, args.get("name"), args.get("description"),
                          args.get("body"))
    if name == "log_money":
        try:
            row = money_ledger.add(money_ledger.DEFAULT_PATH, desk=desk,
                                   kind=args.get("kind"), amount=args.get("amount"),
                                   currency=args.get("currency"),
                                   what=args.get("what"),
                                   category=args.get("category") or "other")
        except money_ledger.LedgerError as exc:
            raise ToolError(exc.reason, exc.detail) from exc
        return {"ok": True, "logged": row,
                "note": "on the Money Board at its next refresh (15 min)"}
    if name == "my_money":
        return my_money(desk)
    try:
        if name == "history":
            return history.search(desk, query=args.get("query"),
                                  since=args.get("since"),
                                  until=args.get("until"),
                                  desk=args.get("about"), kind=args.get("kind"),
                                  limit=args.get("limit"))
        if name == "chronicle":
            return chronicle.read(desk, desk=args.get("about"),
                                  since=args.get("since"))
    except history.HistoryError as exc:
        raise ToolError(exc.reason, exc.detail) from exc
    raise ToolError("no_such_tool", f"no such tool: {name!r}")


def handle(desk: str, msg: dict) -> dict | None:
    """One JSON-RPC message in, one reply out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                  "serverInfo": {"name": NAME, "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            out = call(desk, str(params.get("name")),
                       params.get("arguments") or {})
        except ToolError as exc:
            out = {"ok": False, "reason": exc.reason, "detail": exc.detail}
        result = {"content": [{"type": "text", "text": json.dumps(out)}],
                  "isError": not out.get("ok", False)}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"no method {method!r}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deck_mcp")
    parser.add_argument("--desk", required=True)
    desk = _check_name(parser.parse_args(argv).desk)
    write_stamp(desk)
    code_stamp.write(NAME, desk)   # a deploy of new code reloads this desk
    # stdout IS the protocol. A wake can reach `spawn.start`, and anything it
    # prints would corrupt the JSON-RPC stream, so everything else goes to
    # stderr.
    out, sys.stdout = sys.stdout, sys.stderr
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            reply = handle(desk, msg) if isinstance(msg, dict) else None
        except Exception as exc:  # one bad call must not end the server
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32603, "message": repr(exc)[:300]}}
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
