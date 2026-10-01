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
import re
import sys
from pathlib import Path

from . import atomic, computer_mcp, decisions, manager, office, owner, roster
from .paths import CLAUDE_HOME

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
    {"name": "message_desk",
     "description": "Send a message to another desk by name. Reaches it even "
     "when it is asleep -- the deck wakes it -- which SendMessage cannot. Its "
     "reply comes back to you as a message.",
     "inputSchema": {"type": "object", "properties": {
         "name": _prop("string", "The desk's name, as on the roster."),
         "text": _prop("string", "The message.")},
         "required": ["name", "text"], "additionalProperties": False}},
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
]


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


def call(desk: str, name: str, args: dict) -> dict:
    if name == "say":
        return say(desk, args.get("text"), args.get("urgent") is True)
    if name == "ask":
        return ask(desk, args.get("prompt"), args.get("options"),
                   args.get("help") or "", args.get("allow_custom"))
    if name == "message_desk":
        return message_desk(desk, args.get("name"), args.get("text"))
    if name == "store_search":
        return store_search(desk, args.get("query"), args.get("kind"))
    if name == "store_install":
        return store_install(desk, args.get("id"), args.get("desks"))
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
