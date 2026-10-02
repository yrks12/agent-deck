"""The owner's Mac, as tools in a desk's own Claude session (MB3, MCP `mac`).

    run(command, cwd?, timeout_s?, background?, mac?)   one line in his login shell
    job(job_id, since_stdout?, since_stderr?)           a background job's state + output
    cancel(job_id)                                       kill it (whole process group)
    read / write / list (path, ...)                      his files, paged and capped
    open(target)                                         a URL, app or file
    screenshot(display?)                                 JPEG of one display (default main)
    list_displays()                                      every display: id, label, size
    click / move / drag / scroll / type_text / key       his mouse and keyboard
    status()                                             which Macs, and this desk's access

**Mac control** (owner, 2026-10-01). The six input tools run only under the
per-session control grant the owner turns on in the Mac app (30 min, one desk
or all). The Mac decides every gesture; the deck mirrors its grant from each
poll so a desk without one is refused at once (`control_off`) and ONE card
per desk asks him. Coordinates are pixels of the desk's latest screenshot:
the size of that picture rides every gesture so the Mac maps it to points.

The desk's name is written into the spawn argv (`deck_mcp.config`), exactly
like `server/computer_mcp.py`: **no tool takes a desk field**, so nothing the
model sends can use another desk's grant.

The Mac never listens. A tool enqueues a job in `server/mac_nodes.py`, the
Mac's long-poll claims it, the Mac reports events, and the tool watches the
job file (every `POLL_EVERY` s) until it settles. Every wait is bounded:

* claim -- the store expires a job nobody picks up (`mac_not_answering`);
* grant -- `GRANT_WAIT` s on the owner's card, then `awaiting_grant`;
* run   -- `timeout_s + RUN_GRACE`, then the job is cancelled: `timed_out`;
* and **no call ever blocks past `timeout_s + HARD_GRACE`**, whatever state
  the job is stuck in.

**Offline never hangs.** An offline, paused or absent Mac answers at once and
files ONE owner card (per desk + Mac while it waits; `no_mac` once per desk per
24 h). `mac_api` closes the card and tells the desk when the Mac polls again.

**Threaded.** MEASURED on the box (claude 2.1.286, docs/mac-bridge.md
"Measured"): Esc sends `notifications/cancelled` for the call's request id
while the call is in flight, and `claude stop` SIGTERMs this process with no
cancel and no EOF first. So stdin is read on the main thread while each call
runs on its own, a cancel reaches the job at once, and stdin EOF or SIGTERM
cancels every foreground job this process started. A background job is meant
to outlive the call and is left alone.

Newline-delimited JSON-RPC 2.0 on stdin/stdout. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from . import computer_mcp, handoff, mac_nodes, tool_stamp
from .paths import BUS_DIR

NAME = "mac"
MODULE = "server.mac_mcp"
PROTOCOL = computer_mcp.PROTOCOL

#: Injected by tests; the deck's store otherwise.
STORE: mac_nodes.Store | None = None
HANDOFFS_PATH: Path | None = None

POLL_EVERY = 0.2          # s between looks at the job file
SWEEP_EVERY = 1.0         # s between store sweeps while waiting
GRANT_WAIT = 60.0         # s on the owner's grant card
RUN_GRACE = 15.0          # s past timeout_s for the Mac to report its own timeout
HARD_GRACE = 90.0         # no call ever blocks past timeout_s + this
FILE_TIMEOUT = 60         # s for read/write/list/open/screenshot
RETURN_MAX = 64000        # chars of each stream a desk gets back
CUT_MARK = "[Agent Deck cut this message]"

#: The closed refusal set (MB3). Each carries a `detail` the desk can act on.
REFUSALS = frozenset({
    "no_mac", "mac_offline", "mac_paused", "ambiguous_mac", "unknown_mac",
    "mac_not_answering", "awaiting_grant", "denied", "out_of_scope",
    "blocked_path", "capability_off", "tcc_denied", "needs_admin", "bad_input",
    "bad_path", "no_such_path", "not_a_directory", "not_a_file", "not_text",
    "too_large", "timed_out", "cancelled", "lost", "node_busy", "rate_limited",
    "queue_failed",
    # Mac control
    "control_off", "owner_active", "secure_field", "needs_screenshot",
    # more than one display (2026-10-02)
    "no_such_display"})

INPUT_TIMEOUT = 30        # s for one gesture

#: (desk, node id, display id or None) -> {width, height[, display]} of that
#: desk's latest screenshot of that display. Process-wide: one process serves
#: one desk, and a call may build its own `Server`.
_SPACES: dict[tuple, dict] = {}
#: (desk, node id) -> the display of that desk's latest screenshot: where a
#: click with no `display` lands.
_LAST_DISPLAY: dict[tuple[str, str], int | None] = {}
CONTROL_TOOLS = ("click", "move", "drag", "scroll", "type_text", "key")
_SCROLL = {"up": ("dy", 1), "down": ("dy", -1), "left": ("dx", -1),
           "right": ("dx", 1)}

_OFFLINE = ("mac_offline", "mac_paused", "no_mac")
_OWNER = "the owner"
_SETTINGS = "Shaliach → Settings → Mac"


# ── tool schemas ─────────────────────────────────────────────────────────────


def _listed(node: dict) -> list[dict]:
    """The node's displays, main first, each with its viewer label."""
    return [{"id": d["id"], "label": mac_nodes.display_label(d, node),
             "is_main": d["is_main"], "width_px": d["width_px"],
             "height_px": d["height_px"], "scale": d["scale"],
             "origin_x": d["origin_x"], "origin_y": d["origin_y"]}
            for d in mac_nodes.ordered_displays(node)]


def _p(kind: str, text: str, **more) -> dict:
    return {"type": kind, "description": text, **more}


_MAC = _p("string", "Which Mac, by name or node id. Omit for his only (or "
          "primary) Mac.")
_DISPLAY = _p("integer", "Which display, by id from list_displays. Omit for "
              "the main display.", minimum=0)
_DISPLAY_INPUT = _p("integer", "The display your x, y are on, by id. Omit "
                    "for the display of your latest screenshot.", minimum=0)


def _tool(name: str, text: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": text, "inputSchema": {
        "type": "object", "properties": props, "required": required,
        "additionalProperties": False}}


TOOLS = [
    _tool("run", "Run one shell line on the owner's Mac, in his login shell. "
          "Returns exit, stdout and stderr (the last 64 000 characters of "
          "each). For anything longer than a couple of minutes pass "
          "background=true and poll with job. A refusal's detail is the next "
          "step; if the Mac is offline, do not retry until you are told it "
          "is back.",
          {"command": _p("string", "The shell line."),
           "cwd": _p("string", "Absolute or ~-prefixed working directory."),
           "timeout_s": _p("integer", "Seconds before it is killed. Default "
                           f"{mac_nodes.TIMEOUT_DEFAULT}.", minimum=1,
                           maximum=mac_nodes.TIMEOUT_MAX),
           "background": _p("boolean", "Return a job_id at once; read it "
                            "with job."),
           "mac": _MAC}, ["command"]),
    _tool("job", "A background Mac job: its state and the output since the "
          "offsets you pass (use next_stdout / next_stderr from the last "
          "answer).",
          {"job_id": _p("string", "From run(background=true)."),
           "since_stdout": _p("integer", "Byte offset.", minimum=0),
           "since_stderr": _p("integer", "Byte offset.", minimum=0)},
          ["job_id"]),
    _tool("cancel", "Kill a Mac job you started (its whole process group).",
          {"job_id": _p("string", "The job.")}, ["job_id"]),
    _tool("read", "Read a file on the owner's Mac, a page at a time.",
          {"path": _p("string", "Absolute or ~-prefixed."),
           "offset": _p("integer", "Byte offset.", minimum=0),
           "length": _p("integer", "Bytes, at most 64 000 as text.",
                        minimum=1),
           "encoding": {"type": "string", "enum": ["text", "base64"]},
           "mac": _MAC}, ["path"]),
    _tool("write", "Write a file on the owner's Mac (atomic). At most 5 MiB.",
          {"path": _p("string", "Absolute or ~-prefixed."),
           "content": _p("string", "The content."),
           "encoding": {"type": "string", "enum": ["text", "base64"]},
           "mode": {"type": "string", "enum": ["overwrite", "append", "create"]},
           "make_dirs": _p("boolean", "Create missing parent folders."),
           "mac": _MAC}, ["path", "content"]),
    _tool("list", "One directory level on the owner's Mac.",
          {"path": _p("string", "Absolute or ~-prefixed."),
           "hidden": _p("boolean", "Include dot files."),
           "mac": _MAC}, ["path"]),
    _tool("open", "Open a URL (http, https, mailto) or an app or file on the "
          "owner's Mac.",
          {"target": _p("string", "The URL, app or path."), "mac": _MAC},
          ["target"]),
    _tool("screenshot", "A JPEG of one of the owner's displays -- the main "
          "one unless you pass display (ids from list_displays; the result "
          "lists them too). Allowed if he turned screenshots on, or while Mac "
          "control is on for you. Its width and height are the pixel space "
          "every click/move/drag/scroll uses, on that display.",
          {"display": _DISPLAY, "mac": _MAC}, []),
    _tool("list_displays", "Every display on the owner's Mac, main first: "
          "id (pass it as display), label, pixel size, scale, origin. His Mac "
          "may have more than one; a window you cannot find on the main "
          "display may be on another.", {"mac": _MAC}, []),
    _tool("click", "Click on the owner's Mac at (x, y): pixels of your latest "
          "screenshot. Needs Mac control on for you (a refusal files the "
          "card asking him). Take a screenshot after to see the result.",
          {"x": _p("integer", "Pixels from the left of your last screenshot."),
           "y": _p("integer", "Pixels from the top of your last screenshot."),
           "button": {"type": "string", "enum": ["left", "right", "middle"]},
           "double": _p("boolean", "Double-click."),
           "display": _DISPLAY_INPUT, "mac": _MAC}, ["x", "y"]),
    _tool("move", "Move the owner's pointer to (x, y) without clicking "
          "(hover).", {"x": _p("integer", "Screenshot pixels."),
                       "y": _p("integer", "Screenshot pixels."),
                       "display": _DISPLAY_INPUT, "mac": _MAC},
          ["x", "y"]),
    _tool("drag", "Press at (x, y), move to (to_x, to_y), release.",
          {"x": _p("integer", "Start, screenshot pixels."),
           "y": _p("integer", "Start, screenshot pixels."),
           "to_x": _p("integer", "End, screenshot pixels."),
           "to_y": _p("integer", "End, screenshot pixels."),
           "display": _DISPLAY_INPUT, "mac": _MAC}, ["x", "y", "to_x", "to_y"]),
    _tool("scroll", "Scroll the wheel at (x, y).",
          {"x": _p("integer", "Screenshot pixels."),
           "y": _p("integer", "Screenshot pixels."),
           "direction": {"type": "string",
                         "enum": ["up", "down", "left", "right"]},
           "amount": _p("integer", "Notches, default 3.", minimum=1,
                        maximum=10),
           "display": _DISPLAY_INPUT, "mac": _MAC}, ["x", "y", "direction"]),
    _tool("type_text", "Type text into whatever has focus on the owner's "
          "Mac. Never types into a password field (secure_field).",
          {"text": _p("string", "At most 4096 characters."), "mac": _MAC},
          ["text"]),
    _tool("key", "Press a key or chord on the owner's Mac: Return, Escape, "
          "Tab, space, BackSpace, Delete, Up/Down/Left/Right, Home, End, "
          "Page_Up, F1-F12, a letter, or chords like cmd+s, cmd+shift+t, "
          "ctrl+a, alt+Left.",
          {"keys": _p("string", "One key or chord, joined with +."),
           "mac": _MAC}, ["keys"]),
    _tool("status", "Which Macs this deck has, whether each is online, and "
          "your access to it. Check this first when unsure.", {}, []),
]


# ── the stamp: which tool set this desk's process serves ─────────────────────
#
# MEASURED 2026-10-01: a desk's `mac` process keeps the code it started with,
# so desks running before the control tools shipped had no mcp__mac__click.
# Each process writes the tool set it serves; turning control on reloads the
# live desks whose stamp is missing or older (`mac_api.desks_to_reload`).

#: Injected by tests; the deck's bus otherwise.
STAMP_ROOT: Path | None = None
TOOLS_SIG_NAMES = ",".join(sorted(t["name"] for t in TOOLS))


def _stamps(root: Path | None) -> Path:
    return Path(root or STAMP_ROOT or tool_stamp.root_for("mac"))


def stamp_path(desk: str, *, root: Path | None = None) -> Path:
    return tool_stamp.path("mac", desk, root=_stamps(root))


def write_stamp(desk: str, *, root: Path | None = None) -> None:
    tool_stamp.write("mac", desk, TOOLS_SIG_NAMES,
                     names=[t["name"] for t in TOOLS], root=_stamps(root))


def stale(desk: str, *, root: Path | None = None) -> bool:
    """Is this desk's mac process missing the tools this code serves?"""
    return tool_stamp.stale("mac", desk, TOOLS_SIG_NAMES, root=_stamps(root))


# ── the impure edges (replaced in tests) ─────────────────────────────────────


def _store() -> mac_nodes.Store:
    return STORE if STORE is not None else mac_nodes.Store(BUS_DIR / "mac")


def _handoffs() -> Path:
    return Path(HANDOFFS_PATH or handoff.DEFAULT_PATH)


class _Refused(Exception):
    def __init__(self, reason: str, detail: str, **extra) -> None:
        super().__init__(detail)
        self.reason, self.detail, self.extra = reason, detail, extra


def _refusal(reason: str, detail: str, **extra) -> dict:
    return {"ok": False, "reason": reason, "detail": detail, **extra}


def _clock(ts) -> str:
    return time.strftime("%H:%M", time.localtime(float(ts))) if ts else "never"


def _cut(text: str) -> tuple[str, bool]:
    """The newest RETURN_MAX characters, marked when anything was cut."""
    if len(text) <= RETURN_MAX:
        return text, False
    keep = RETURN_MAX - len(CUT_MARK) - 1
    return f"{CUT_MARK}\n{text[-keep:]}", True


# ── offline: one card, never a wait ─────────────────────────────────────────


def _card_waiting(card_id: str | None) -> bool:
    if not card_id:
        return False
    return any(h.id == card_id and h.status == "waiting"
               for h in handoff.load(_handoffs()))


def _offline(store: mac_nodes.Store, desk: str, err: mac_nodes.MacError,
             summary: str) -> dict:
    """Answer an offline / paused / absent Mac at once, filing ONE card."""
    wanted = f"Nothing ran on the Mac. {desk} wanted to: {summary[:80]}"
    if err.reason == "no_mac":
        card_id = store.no_mac_card(desk)
        if card_id is None:
            card_id = handoff.raise_handoff(
                _handoffs(), agent=desk, kind="other",
                needs=f"Turn on Mac access in {_SETTINGS}", state=wanted,
                where=_SETTINGS).id
            store.set_no_mac_card(desk, card_id)
        return _refusal("no_mac", (
            f"No Mac is connected to this deck. {_OWNER.capitalize()} turns it "
            f"on in {_SETTINGS}; if he installed Shaliach from the App "
            "Store he needs the direct-download version of the app instead, "
            "because the App Store build cannot give desks a Mac. A card "
            f"asking him is on his screen ({card_id}). Do other work; do not "
            "retry unless he tells you the Mac is connected."))
    node = err.node or {}
    name = node.get("name") or "The Mac"
    card_id = store.offline_card(node["node_id"], desk)
    if not _card_waiting(card_id):
        needs = (f"Resume Mac access in Shaliach on {name} so {desk} can "
                 "use it" if err.reason == "mac_paused" else
                 f"Open Shaliach on {name} so {desk} can use it")
        card_id = handoff.raise_handoff(_handoffs(), agent=desk, kind="other",
                                        needs=needs, state=wanted,
                                        where=name).id
        store.set_offline_card(node["node_id"], desk, card_id)
    if err.reason == "mac_paused":
        why = f"{name} has Mac access paused."
        ask = f"A card asking {_OWNER} to resume it"
    else:
        why = (f"{name} is offline or asleep (last seen "
               f"{_clock(node.get('last_seen'))}).")
        ask = f"A card asking {_OWNER} to open Shaliach on it"
    return _refusal(err.reason, (
        f"{why} {ask} is on his screen ({card_id}). Do other work; you will "
        "get a message when the Mac is back. Do not retry until then."),
        mac=name)


# ── the server ───────────────────────────────────────────────────────────────


class _Call:
    """One tools/call in flight."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.job_id: str | None = None
        self.background = False


class Server:
    """One desk's `mac` server. Safe to call from several threads."""

    def __init__(self, desk: str) -> None:
        self.desk = desk
        self._lock = threading.Lock()
        self._calls: dict = {}
        self._out_lock = threading.Lock()

    # -- cancellation --

    def cancel(self, request_id) -> None:
        with self._lock:
            call = self._calls.get(request_id)
        if call is not None:
            call.cancelled.set()
            self._kill(call)

    def cancel_all(self) -> None:
        """stdin EOF / SIGTERM: every foreground job this process started."""
        with self._lock:
            calls = list(self._calls.values())
        for call in calls:
            call.cancelled.set()
            self._kill(call)

    def _kill(self, call: _Call) -> None:
        if call.job_id and not call.background:
            try:
                _store().request_cancel(call.job_id, self.desk)
            except (mac_nodes.MacError, OSError):
                pass

    # -- the tools --

    def tool(self, name: str, args: dict, call: _Call | None = None) -> dict:
        call = call or _Call()
        if not isinstance(args, dict):
            return _refusal("bad_input", "arguments must be an object")
        try:
            if name == "status":
                return self._status()
            if name == "list_displays":
                return self._displays(args)
            if name == "job":
                return self._job(args)
            if name == "cancel":
                return self._cancel(args)
            if name == "run":
                return self._submit("run", {k: args[k] for k in ("command", "cwd")
                                            if k in args}, args, call)
            if name in ("read", "write", "list", "open", "screenshot"):
                job_args = {k: v for k, v in args.items() if k != "mac"}
                return self._submit(name, job_args, args, call)
            if name in CONTROL_TOOLS:
                return self._input(name, args, call)
            return _refusal("bad_input", f"no such tool: {name!r}")
        except _Refused as exc:
            return _refusal(exc.reason, exc.detail, **exc.extra)
        except OSError as exc:
            return _refusal("queue_failed", f"the deck could not reach its Mac "
                            f"queue: {exc}")

    # -- Mac control --

    def _gesture(self, name: str, args: dict) -> dict:
        """The tool's arguments as one `input` gesture (unchecked)."""
        if name == "type_text":
            return {"action": "type", "text": args.get("text")}
        if name == "key":
            return {"action": "key", "key": args.get("keys")}
        out = {"action": name, "x": args.get("x"), "y": args.get("y")}
        if name == "click":
            out["button"] = args.get("button", "left")
            out["count"] = 2 if args.get("double") else 1
        elif name == "drag":
            out.update(to_x=args.get("to_x"), to_y=args.get("to_y"))
        elif name == "scroll":
            axis, sign = _SCROLL.get(args.get("direction"), ("dy", 0))
            amount = args.get("amount", 3)
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise _Refused("bad_input", "amount is 1..10 notches")
            out[axis] = sign * amount
        return out

    def _input(self, name: str, args: dict, call: _Call) -> dict:
        store = _store()
        gesture = self._gesture(name, args)
        try:
            node = store.resolve_node(args.get("mac"))
        except mac_nodes.MacError as err:
            if err.reason in _OFFLINE:
                return _offline(store, self.desk, err, name)
            return _refusal(err.reason, err.detail)
        now = store.now()
        online = mac_nodes.presence(node, now) == "online"
        if online and not mac_nodes.control_live(node, self.desk, now):
            try:
                summary = mac_nodes.summary_of(
                    "input", mac_nodes.validate_args("input", {
                        **gesture, "space": {"width": 1 << 14,
                                             "height": 1 << 14}}))
            except mac_nodes.MacError:
                summary = name
            return self._control_off(store, node, summary)
        if gesture["action"] not in ("type", "key"):
            key = (self.desk, node["node_id"])
            display = args.get("display", _LAST_DISPLAY.get(key))
            space = _SPACES.get((*key, display))
            if space is None:
                which = "" if args.get("display") is None \
                    else f" with display={args['display']}"
                return _refusal("needs_screenshot", (
                    f"Take mcp__mac__screenshot{which} first: x and y are "
                    "pixels of your latest screenshot of that display, and "
                    "you have none yet."))
            gesture["space"] = dict(space)
        try:
            mac_nodes.validate_args("input", gesture)
        except mac_nodes.MacError as err:
            return _refusal(err.reason, err.detail)
        return self._submit("input", gesture, {"mac": node["node_id"]}, call)

    def _control_off(self, store: mac_nodes.Store, node: dict,
                     summary: str) -> dict:
        """No live grant: one card per desk while it waits, never a wait."""
        name = node.get("name") or "his Mac"
        card_id = store.control_ask(node["node_id"], self.desk)
        if not _card_waiting(card_id):
            card_id = handoff.raise_handoff(
                _handoffs(), agent=self.desk, kind="other",
                needs=(f"Let {self.desk} control {name}: tap Allow on the "
                       "card on your Mac, or turn on \"Let agents control "
                       f"this Mac\" in {_SETTINGS}"),
                state=(f"Nothing was clicked or typed. {self.desk} wanted "
                       f"to: {summary[:80]}"),
                where=name).id
            store.set_control_ask(node["node_id"], self.desk, card_id)
        return _refusal("control_off", (
            f"Mac control is off for you on {name}. A card asking "
            f"{_OWNER} to Allow it (30 minutes) is on his screen ({card_id}). "
            "Do other work; you will get a message when he turns it on. Do "
            "not retry until then."), mac=name)

    def _submit(self, kind: str, job_args: dict, args: dict,
                call: _Call) -> dict:
        store = _store()
        background = bool(args.get("background", False)) if kind == "run" \
            else False
        timeout_s = args.get("timeout_s", mac_nodes.TIMEOUT_DEFAULT) \
            if kind == "run" else INPUT_TIMEOUT if kind == "input" \
            else FILE_TIMEOUT
        try:
            summary = mac_nodes.summary_of(kind, job_args)
        except Exception:  # summary is for the card only
            summary = kind
        try:
            job = store.enqueue(self.desk, kind, job_args, mac=args.get("mac"),
                                timeout_s=timeout_s, background=background)
        except mac_nodes.MacError as err:
            if err.reason in _OFFLINE:
                return _offline(store, self.desk, err, summary)
            return _refusal(err.reason, err.detail)
        call.job_id, call.background = job["id"], background
        if call.cancelled.is_set():   # cancelled while it was being queued
            self._kill(call)
        name = store.node(job["node"]).get("name", "")
        return self._wait(store, job, name, call)

    def _wait(self, store: mac_nodes.Store, job: dict, mac: str,
              call: _Call) -> dict:
        """Watch the job until it settles, a phase runs out, or the hard cap."""
        start = time.monotonic()
        hard = start + job["timeout_s"] + HARD_GRACE
        grant_since = run_since = None
        next_sweep = start + SWEEP_EVERY
        job_id = job["id"]
        while True:
            now = time.monotonic()
            if now >= next_sweep:
                store.sweep()
                next_sweep = now + SWEEP_EVERY
            row = store.job(job_id)
            state = row.get("state")
            if state in mac_nodes.SETTLED:
                return self._settled(store, row, mac)
            if call.cancelled.is_set():
                return _refusal("cancelled", "cancelled", job_id=job_id)
            if call.background and state != "queued":
                return {"ok": True, "job_id": job_id, "mac": mac,
                        "state": "awaiting_grant" if state == "awaiting_grant"
                        else "running"}
            if state == "awaiting_grant":
                grant_since = grant_since or now
                if now - grant_since >= GRANT_WAIT:
                    return _refusal("awaiting_grant", (
                        f"{_OWNER.capitalize()} has a card on his Mac asking "
                        "whether you may use it. Do other work; you will get "
                        "a message when he answers."), job_id=job_id, mac=mac)
            if state == "running":
                run_since = run_since or now
                if now - run_since >= job["timeout_s"] + RUN_GRACE:
                    return self._give_up(store, job_id, mac, "timed_out")
            if now >= hard:
                return self._give_up(store, job_id, mac,
                                     "node_busy" if state == "queued"
                                     else "timed_out")
            call.cancelled.wait(max(0.0, min(POLL_EVERY, hard - now)))

    def _give_up(self, store, job_id: str, mac: str, reason: str) -> dict:
        try:
            store.request_cancel(job_id, self.desk)
        except mac_nodes.MacError:
            pass
        out = self._streams(store, job_id)
        if reason == "node_busy":
            detail = (f"{mac} was busy with other jobs the whole time; nothing "
                      "ran. Retry later or use background=true.")
        else:
            detail = (f"It ran past its timeout on {mac} and was stopped. "
                      "Partial output is attached. For long work use "
                      "background=true and poll with job.")
        return {"ok": False, "reason": reason, "detail": detail,
                "job_id": job_id, "mac": mac, **out}

    def _streams(self, store, job_id: str) -> dict:
        out: dict = {"truncated": False}
        for stream in mac_nodes.STREAMS:
            text, _, cut_server = store.output(job_id, stream, 0)
            text, cut_here = _cut(text)
            out[stream] = text
            out["truncated"] = out["truncated"] or cut_server or cut_here
        return out

    def _settled(self, store, row: dict, mac: str) -> dict:
        state, kind, job_id = row["state"], row["kind"], row["id"]
        payload = row.get("payload") or {}
        if state != "done":
            reason = row.get("reason") or state
            if state == "timed_out":
                reason = "timed_out"
            detail = row.get("detail") or {
                "cancelled": "the job was cancelled",
                "timed_out": "it ran past its timeout and was stopped",
                "lost": "the Mac stopped reporting on this job",
            }.get(state, state)
            out = _refusal(reason, detail)
            if kind == "run" and state in ("timed_out", "cancelled", "lost",
                                           "failed"):
                out.update(job_id=job_id, mac=mac, exit=row.get("exit"),
                           **self._streams(store, job_id))
            return out
        if kind == "run":
            return {"ok": True, "job_id": job_id, "mac": mac,
                    "exit": row.get("exit"), **self._streams(store, job_id),
                    "duration_ms": row.get("duration_ms"),
                    "sandboxed": bool(row.get("sandboxed"))}
        if kind == "read":
            return {"ok": True, "mac": mac, "path": row["args"]["path"],
                    **payload}
        if kind == "write":
            return {"ok": True, "mac": mac,
                    "path": payload.get("path") or row["args"]["path"],
                    "bytes": payload.get("bytes")}
        if kind == "list":
            return {"ok": True, "mac": mac,
                    "path": payload.get("path") or row["args"]["path"],
                    "entries": payload.get("entries") or [],
                    "truncated": bool(payload.get("truncated"))}
        if kind == "input":
            return {"ok": True, "mac": mac, "did": row.get("summary") or ""}
        if kind == "screenshot":
            display = payload.get("display")
            display = display if isinstance(display, int) \
                and not isinstance(display, bool) else None
            if isinstance(payload.get("width"), int) \
                    and isinstance(payload.get("height"), int):
                space = {"width": payload["width"], "height": payload["height"]}
                if display is not None:
                    space["display"] = display
                key = (self.desk, row["node"])
                _SPACES[(*key, display)] = space
                if row["args"].get("display") is None:
                    _SPACES[(*key, None)] = space   # "the main one"
                _LAST_DISPLAY[key] = display
            out = {"ok": True, "mac": mac, "width": payload.get("width"),
                   "height": payload.get("height")}
            listed = _listed(store.node(row["node"]))
            if display is not None or listed:   # a Mac that reports displays
                out.update(display=display, displays=listed)
            return {**out, "_image": payload.get("base64") or "",
                    "_mime": payload.get("mime") or "image/jpeg"}
        return {"ok": True, "mac": mac}

    def _mine(self, job_id) -> dict:
        try:
            row = _store().job(str(job_id or ""))
        except mac_nodes.MacError:
            row = None
        if row is None or row.get("desk") != self.desk:
            raise _Refused("bad_input", f"you have no Mac job {job_id!r}")
        return row

    def _job(self, args: dict) -> dict:
        row = self._mine(args.get("job_id"))
        store, out = _store(), {"ok": True, "state": row["state"],
                                "exit": row.get("exit"), "truncated": False}
        for stream in mac_nodes.STREAMS:
            since = args.get(f"since_{stream}", 0)
            if isinstance(since, bool) or not isinstance(since, int) or since < 0:
                raise _Refused("bad_input", f"since_{stream} is a byte offset")
            text, nxt, cut_server = store.output(row["id"], stream, since)
            text, cut_here = _cut(text)
            out[stream], out[f"next_{stream}"] = text, nxt
            out["truncated"] = out["truncated"] or cut_server or cut_here
        return out

    def _cancel(self, args: dict) -> dict:
        row = self._mine(args.get("job_id"))
        row = _store().request_cancel(row["id"], self.desk)
        return {"ok": True, "state": row.get("state")}

    def _displays(self, args: dict) -> dict:
        store = _store()
        try:
            node = store.resolve_node(args.get("mac"))
        except mac_nodes.MacError as err:
            return _refusal(err.reason, err.detail)
        return {"ok": True, "mac": node.get("name", ""),
                "online": mac_nodes.presence(node, store.now()) == "online",
                "displays": _listed(node)}

    def _status(self) -> dict:
        store = _store()
        now = store.now()
        macs = []
        for node in store.nodes():
            mode = node.get("mode", "ask")
            grant = (node.get("grants") or {}).get(self.desk) or {}
            until = grant.get("until")
            access = grant.get("state") or "none"
            if mode == "full":
                access, until = "full", None
            elif access in ("hour", "denied") and until is not None \
                    and float(until) <= now:
                access, until = "none", None
            control = node.get("control") or {}
            yours = mac_nodes.control_live(node, self.desk, now)
            macs.append({"name": node.get("name", ""),
                         "control": {"yours": yours,
                                     "scope": control.get("scope") if yours
                                     else None,
                                     "until": control.get("until") if yours
                                     else None},
                         "online": mac_nodes.presence(node, now) == "online",
                         "mode": mode, "primary": bool(node.get("primary")),
                         "last_seen": node.get("last_seen"),
                         "your_access": access, "until": until})
        return {"ok": True, "macs": macs}

    # -- JSON-RPC --

    def handle(self, msg: dict) -> dict | None:
        """One message in, one reply out. None for a notification -- and for
        a call that was cancelled, which MCP says is never answered."""
        method, mid = msg.get("method"), msg.get("id")
        if method == "notifications/cancelled":
            self.cancel((msg.get("params") or {}).get("requestId"))
            return None
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
            with self._lock:   # `serve` may have registered it already
                call = self._calls.setdefault(mid, _Call())
            try:
                out = self.tool(str(params.get("name")),
                                params.get("arguments") or {}, call)
            finally:
                with self._lock:
                    self._calls.pop(mid, None)
            if call.cancelled.is_set():
                return None
            content = []
            if "_image" in out:
                content.append({"type": "image", "mimeType": out.pop("_mime"),
                                "data": out.pop("_image")})
            content.insert(0, {"type": "text", "text": json.dumps(out)})
            result = {"content": content, "isError": not out.get("ok", False)}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"no method {method!r}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _reply(self, out, msg: dict) -> None:
        try:
            reply = self.handle(msg)
        except Exception as exc:  # one bad call must not end the server
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32603, "message": repr(exc)[:300]}}
        if reply is not None:
            with self._out_lock:
                out.write(json.dumps(reply) + "\n")
                out.flush()

    def serve(self, infile, out) -> None:
        """Read stdin on this thread; run each tools/call on its own."""
        workers = []
        for line in infile:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("method") == "tools/call" and msg.get("id") is not None:
                with self._lock:   # registered now, so a cancel can't miss it
                    self._calls.setdefault(msg["id"], _Call())
                worker = threading.Thread(target=self._reply, args=(out, msg),
                                          daemon=True)
                worker.start()
                workers.append(worker)
            else:
                self._reply(out, msg)
        self.cancel_all()
        for worker in workers:
            worker.join(timeout=2.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mac_mcp")
    parser.add_argument("--desk", required=True)
    desk = parser.parse_args(argv).desk.strip()
    if not desk or len(desk) > 64 or any(c in desk for c in "\n\r\0") \
            or desk == mac_nodes.OWNER_DESK:
        parser.error(f"not a desk name: {desk!r}")
    server = Server(desk)
    write_stamp(desk)
    out, sys.stdout = sys.stdout, sys.stderr   # stdout IS the protocol

    def _stop(signum, frame):
        server.cancel_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    server.serve(sys.stdin, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
