"""The Mac bridge's server side: which Macs exist, and the jobs sent to them (MB2).

A desk on the box asks to run something on the owner's Mac. The Mac never
listens -- it dials out and long-polls the deck -- so the deck keeps a small
queue: a desk's `mac` MCP process enqueues a job, the Mac's poll claims it, the
Mac reports events back, the desk reads the result. This module is that queue
and nothing else. It has no HTTP in it (`server/mac_api.py` is the wire) and no
desk tools in it (`server/mac_mcp.py`).

Files under one injected root (the deck passes `BUS_DIR / "mac"`):

* `nodes.json` -- every paired Mac. The node secret is kept as `sha256` only;
  the plaintext exists once, in the `201` that pairs it.
* `jobs/<id>.json` -- one job's row; `jobs/<id>.stdout|.stderr` its output;
  `jobs/<id>.payload.json` its kind-specific result (file text, a listing, a
  screenshot).
* `.lock` -- an `flock` held around EVERY read-modify-write.

Two kinds of process write here: the deck (registration, polls, events) and
each desk's `mac` MCP process (enqueue, cancel). Same pattern as
`decisions.py`: the lock serialises the load-modify-save, `atomic.write_text`
makes each file whole on disk. Output files are appended by the deck only --
`record_events` is the one writer -- so a desk process never races an append.

Offline never hangs: a job is only accepted for a Mac that is polling, a
queued job nobody claims expires (`mac_not_answering`) and a claimed job that
stops reporting is `lost`. Every wait a caller does is therefore bounded by a
state this module will reach on its own via `sweep`.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import atomic

# ── the numbers (MB2) ────────────────────────────────────────────────────────

ONLINE_WINDOW = 40.0      # s since last poll -> online
CLAIM_WAIT = 10.0         # queued & unclaimed this long, no poll since -> expired
LOST_AFTER = 45.0         # claimed/running with no event this long -> lost
KEEP_DAYS = 7             # settled jobs purged on sweep
OUT_KEEP = 1 << 20        # bytes kept per stream
OUT_HEAD = 64 * 1024      # of which: the first 64 KiB, then the newest bytes
PER_NODE_CONCURRENT, PER_DESK_CONCURRENT = 4, 2
PER_DESK_PER_MIN = 60     # enqueue rate -> rate_limited
QUEUE_MAX_PER_NODE = 32   # -> node_busy

# ── wire limits (MB1) ────────────────────────────────────────────────────────

KINDS = ("run", "read", "write", "list", "open", "screenshot", "input")
CAPABILITIES = KINDS
MODES = ("ask", "full", "paused")
GRANT_STATES = ("hour", "always", "denied")
TIMEOUT_DEFAULT, TIMEOUT_MAX = 120, 600
COMMAND_MAX = 16384
ENV_MAX_KEYS = 32
READ_TEXT_MAX = 64000
READ_BASE64_MAX = 5 << 20     # product table: base64 reads page up to 5 MiB
WRITE_MAX = 5 << 20
CHUNK_MAX = 65536             # chars per stdout/stderr event
PAYLOAD_MAX = 8 << 20         # serialised result payload (5 MiB file as base64 fits)
NAME_MAX = 64
FIELD_MAX = 256
SUMMARY_MAX = 200
NO_MAC_CARD_TTL = 24 * 3600.0

# ── Mac control (owner, 2026-10-01) ──────────────────────────────────────────
#
# `input` is one gesture on his Mac: click, move, drag, scroll, type, key. It
# runs only under the per-session control grant the MAC holds (the Mac
# decides; the deck mirrors the grant from each poll so a desk is refused at
# once, with a card, instead of queueing a job the Mac will refuse).

INPUT_ACTIONS = ("click", "move", "drag", "scroll", "type", "key")
BUTTONS = {1: "left", 2: "middle", 3: "right",
           "left": "left", "middle": "middle", "right": "right"}
TYPE_MAX = 4096
KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_+]{0,39}\Z")
SPACE_MAX = 20000
#: His own hands on the phone's viewer. Not a legal desk name (desks are
#: letters, digits, `-` and `_`), so no desk's tool can borrow it.
OWNER_DESK = "@owner"
OWNER_PER_MIN = 900       # he types a key per job; a desk's 60 would stall him
INPUT_KEEP = 600.0        # settled input jobs are purged after 10 min, not 7 days
WATCH_TTL = 8.0           # s a viewer's last frame request keeps the Mac capturing

ACTIVE = ("claimed", "awaiting_grant", "running")
RESULT_STATES = ("done", "failed", "cancelled", "timed_out", "refused")
SETTLED = RESULT_STATES + ("lost", "expired")
STREAMS = ("stdout", "stderr")
EVENT_TYPES = ("awaiting_grant", "started", "stdout", "stderr", "result")


class MacError(Exception):
    """Refusal. `reason` is a stable slug, `status` the HTTP answer.

    `node` carries the resolved node row when there is one (an offline or
    paused Mac) so the desk tool can name it and file the owner's card.
    """

    def __init__(self, reason: str, detail: str = "", status: int = 400,
                 node: dict | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status = status
        self.node = node


# ── pure helpers ─────────────────────────────────────────────────────────────


def hash_secret(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def presence(node: dict, now: float) -> str:
    """`online` | `offline` | `paused`. A Mac that said it is paused is paused
    however long ago it said so -- it stops polling on purpose, and "offline"
    would send the owner to wake a Mac that is awake."""
    if node.get("mode") == "paused":
        return "paused"
    seen = node.get("last_seen")
    if seen is None:
        return "offline"
    return "online" if now - float(seen) <= ONLINE_WINDOW else "offline"


def _clock_str(ts) -> str:
    if not ts:
        return "never"
    return time.strftime("%H:%M", time.localtime(float(ts)))


def _bad(detail: str) -> MacError:
    return MacError("bad_input", detail, 400)


def _text(value, field: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _bad(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise _bad(f"{field} is required")
    if len(value) > limit:
        raise _bad(f"{field} is over {limit} characters")
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise _bad(f"{field} has control characters")
    return value


def _int(value, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{field} must be an integer")
    if not lo <= value <= hi:
        raise _bad(f"{field} must be {lo}..{hi}")
    return value


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{field} must be true or false")
    return value


def _path_arg(value, field: str = "path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{field} is required")
    if len(value) > 4096 or "\x00" in value:
        raise MacError("bad_path", f"{field} is not a usable path", 400)
    return value


def validate_args(kind: str, args) -> dict:
    """The job's `args`, checked against MB1 and normalised. Raises bad_input."""
    if kind not in KINDS:
        raise _bad(f"kind is one of {', '.join(KINDS)}")
    if not isinstance(args, dict):
        raise _bad("args must be an object")
    if kind == "run":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise _bad("command is required")
        if len(command) > COMMAND_MAX:
            raise _bad(f"command is over {COMMAND_MAX} characters")
        cwd = args.get("cwd")
        if cwd is not None:
            cwd = _path_arg(cwd, "cwd")
            if not (cwd.startswith("/") or cwd == "~" or cwd.startswith("~/")):
                raise MacError("bad_path", "cwd must be absolute or start with ~",
                               400)
        env = args.get("env") or {}
        if not isinstance(env, dict) or len(env) > ENV_MAX_KEYS or not all(
                isinstance(k, str) and k and isinstance(v, str)
                for k, v in env.items()):
            raise _bad(f"env is up to {ENV_MAX_KEYS} string keys to string values")
        return {"command": command, "cwd": cwd, "env": dict(env)}
    if kind == "read":
        encoding = args.get("encoding", "text")
        if encoding not in ("text", "base64"):
            raise _bad("encoding is text or base64")
        cap = READ_TEXT_MAX if encoding == "text" else READ_BASE64_MAX
        return {"path": _path_arg(args.get("path")),
                "offset": _int(args.get("offset", 0), "offset", 0, 1 << 62),
                "length": _int(args.get("length", READ_TEXT_MAX), "length", 1, cap),
                "encoding": encoding}
    if kind == "write":
        encoding = args.get("encoding", "text")
        if encoding not in ("text", "base64"):
            raise _bad("encoding is text or base64")
        content = args.get("content")
        if not isinstance(content, str):
            raise _bad("content must be a string")
        if encoding == "base64":
            try:
                size = len(base64.b64decode(content, validate=True))
            except (binascii.Error, ValueError):
                raise _bad("content is not valid base64") from None
        else:
            size = len(content.encode("utf-8"))
        if size > WRITE_MAX:
            raise MacError("too_large", f"a write is at most {WRITE_MAX} bytes",
                           400)
        mode = args.get("mode", "overwrite")
        if mode not in ("overwrite", "append", "create"):
            raise _bad("mode is overwrite, append or create")
        return {"path": _path_arg(args.get("path")), "content": content,
                "encoding": encoding, "mode": mode,
                "make_dirs": _bool(args.get("make_dirs", False), "make_dirs")}
    if kind == "list":
        return {"path": _path_arg(args.get("path")),
                "hidden": _bool(args.get("hidden", False), "hidden")}
    if kind == "input":
        return _input_args(args)
    if kind == "open":
        target = args.get("target")
        if not isinstance(target, str) or not target.strip():
            raise _bad("target is required")
        if len(target) > 4096 or "\x00" in target:
            raise _bad("target is not usable")
        return {"target": target}
    # screenshot: of one display (default: the main one)
    if args.get("display") is None:
        return {}
    return {"display": _display_id(args["display"])}


def _space(value) -> dict:
    if not isinstance(value, dict):
        raise _bad("space is {width, height}: the pixels of the picture the "
                   "coordinates are in")
    out = {"width": _int(value.get("width"), "space.width", 1, SPACE_MAX),
           "height": _int(value.get("height"), "space.height", 1, SPACE_MAX)}
    if value.get("display") is not None:   # which display it is a picture of
        out["display"] = _display_id(value["display"], "space.display")
    return out


def _display_id(value, what: str = "display") -> int:
    return _int(value, what, 0, (1 << 32) - 1)


def _point(args: dict, space: dict, xk: str = "x", yk: str = "y") -> tuple:
    """A coordinate inside `space`. Off-picture is refused, never clamped."""
    x = _int(args.get(xk), xk, -(1 << 30), 1 << 30)
    y = _int(args.get(yk), yk, -(1 << 30), 1 << 30)
    if not (0 <= x < space["width"] and 0 <= y < space["height"]):
        raise _bad(f"({x},{y}) is outside the {space['width']}x"
                   f"{space['height']} picture")
    return x, y


def _button(value) -> str:
    if isinstance(value, bool) or value not in BUTTONS:
        raise _bad("button is left, middle or right (or 1, 2, 3)")
    return BUTTONS[value]


def _input_args(args: dict) -> dict:
    """One gesture, checked. Coordinates are in `space` (screenshot pixels)."""
    action = args.get("action")
    if action not in INPUT_ACTIONS:
        raise _bad(f"action is one of {', '.join(INPUT_ACTIONS)}")
    if action == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise _bad("nothing to type")
        if len(text) > TYPE_MAX or "\x00" in text:
            raise _bad(f"type is at most {TYPE_MAX} characters")
        return {"action": "type", "text": text}
    if action == "key":
        key = args.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise _bad("key is a name or chord like Return, Escape, cmd+s")
        return {"action": "key", "key": key}
    space = _space(args.get("space"))
    x, y = _point(args, space)
    out = {"action": action, "x": x, "y": y}
    if action == "click":
        count = _int(args.get("count", 1), "count", 1, 3)
        out.update(button=_button(args.get("button", "left")), count=count)
    elif action == "drag":
        out["to_x"], out["to_y"] = _point(args, space, "to_x", "to_y")
        out["button"] = _button(args.get("button", "left"))
    elif action == "scroll":
        dy, dx = args.get("dy"), args.get("dx")
        if (dy is None) == (dx is None):
            raise _bad("scroll needs exactly one of dy or dx")
        axis, raw = ("dy", dy) if dy is not None else ("dx", dx)
        amount = _int(raw, axis, -10, 10)
        if amount == 0:
            raise _bad(f"{axis} is 1..10 or -1..-10")
        out[axis] = amount
    out["space"] = space
    return out


def _input_summary(args: dict) -> str:
    action = args.get("action")
    at = f"{args.get('x')},{args.get('y')}"
    if action == "type":
        text = str(args.get("text", ""))
        shown = text if len(text) <= 40 else text[:39] + "…"
        return f"type {shown!r} ({len(text)} chars)"
    if action == "key":
        return f"key {args.get('key')}"
    if action == "click":
        word = {1: "click", 2: "double-click", 3: "triple-click"}.get(
            args.get("count", 1), "click")
        if args.get("button", "left") != "left":
            word = f"{args['button']}-{word}"
        return f"{word} {at}"
    if action == "drag":
        return f"drag {at} -> {args.get('to_x')},{args.get('to_y')}"
    if action == "scroll":
        return f"scroll {at} " + (f"dy {args['dy']}" if "dy" in args
                                  else f"dx {args.get('dx')}")
    return f"{action} {at}"


def control_live(node: dict, desk: str, now: float) -> bool:
    """Does the Mac's last-reported control grant cover `desk` right now?

    Mirrors the Mac's own check so a desk is refused before anything is
    queued; the Mac still decides every job itself. The owner's own hands
    (`OWNER_DESK`) ride any live grant."""
    control = node.get("control")
    if not isinstance(control, dict):
        return False
    until = control.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)) \
            or float(until) <= now:
        return False
    scope = control.get("scope")
    return desk == OWNER_DESK or scope == "all" or scope == desk


def watching(node: dict, now: float) -> bool:
    """Has someone asked for the live screen in the last `WATCH_TTL` s?"""
    until = node.get("watch_until")
    return isinstance(until, (int, float)) and float(until) > now


def _control_report(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad("control is {scope, until} or null")
    scope = _text(value.get("scope"), "control.scope", NAME_MAX)
    until = value.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        raise _bad("control.until is a timestamp")
    return {"scope": scope, "until": float(until)}


def _screen_report(value) -> dict | None:
    if value is None:
        return None
    return _space(value)


DISPLAYS_MAX = 16


def _displays_report(value) -> list | None:
    """Every display the Mac has: id, name, pixels, scale, origin (global
    points) and which is main. Mirrored as sent, checked."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > DISPLAYS_MAX:
        raise _bad(f"displays is a list of at most {DISPLAYS_MAX} displays")
    out = []
    for d in value:
        if not isinstance(d, dict):
            raise _bad("each display is an object")
        scale = d.get("scale")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) \
                or not 0.25 <= float(scale) <= 8:
            raise _bad("display.scale is pixels per point")
        origin = []
        for k in ("origin_x", "origin_y"):
            v = d.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or abs(float(v)) > 1 << 20:
                raise _bad(f"display.{k} is global points")
            origin.append(float(v))
        out.append({
            "id": _display_id(d.get("id"), "display.id"),
            "name": (d["name"].strip()[:NAME_MAX] if isinstance(d.get("name"), str)
                     else "") or "Display",
            "width_px": _int(d.get("width_px"), "display.width_px", 1, SPACE_MAX),
            "height_px": _int(d.get("height_px"), "display.height_px", 1,
                              SPACE_MAX),
            "scale": float(scale), "origin_x": origin[0],
            "origin_y": origin[1], "is_main": d.get("is_main") is True})
    return out


def ordered_displays(node: dict) -> list[dict]:
    """Main first (Display 1), then left to right, top to bottom -- the order
    every viewer numbers them in (DeckKit `MacDisplays.ordered`)."""
    return sorted(node.get("displays") or [], key=lambda d: (
        not d.get("is_main"), d.get("origin_x", 0), d.get("origin_y", 0),
        d.get("id", 0)))


def display_label(display: dict, node: dict) -> str:
    """"Display 2 · PM1561P"; the laptop's own panel is "Built-in"."""
    ids = [d["id"] for d in ordered_displays(node)]
    n = ids.index(display["id"]) + 1 if display["id"] in ids else 1
    name = str(display.get("name") or "Display")
    short = "Built-in" if name.lower().startswith("built-in") else name
    return f"Display {n} · {short}"


def main_display(node: dict) -> dict | None:
    """The main display, when the Mac reports its displays."""
    listed = ordered_displays(node)
    return listed[0] if listed else None


def watched_displays(node: dict, now: float) -> list[int]:
    """The displays a viewer asked for in the last `WATCH_TTL` s."""
    marks = node.get("watch_displays") or {}
    return sorted(int(k) for k, until in marks.items()
                  if isinstance(until, (int, float)) and float(until) > now)


def _perms_report(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad("perms is {accessibility, screen_recording}")
    return {k: bool(value.get(k)) for k in ("accessibility", "screen_recording")}


def summary_of(kind: str, args: dict) -> str:
    """One line for the audit copy and the owner's card. Never file content."""
    if kind == "run":
        text = args.get("command", "")
    elif kind in ("read", "write", "list"):
        text = f"{kind} {args.get('path', '')}"
    elif kind == "open":
        text = f"open {args.get('target', '')}"
    elif kind == "input":
        text = _input_summary(args)
    else:
        text = "screenshot"
    text = " ".join(str(text).split())
    return text[:SUMMARY_MAX]


def wire(job: dict) -> dict:
    """The job as a Mac receives it (MB1)."""
    return {k: job[k] for k in ("id", "desk", "kind", "args", "timeout_s",
                                "background", "created_at")}


def audit_row(job: dict) -> dict:
    """The owner's audit copy: what ran, never its output."""
    return {k: job.get(k) for k in (
        "id", "desk", "kind", "summary", "state", "exit", "reason",
        "created_at", "claimed_at", "started_at", "settled_at")}


# ── the store ────────────────────────────────────────────────────────────────


class Store:
    """Everything under one root. Safe across processes (see module doc)."""

    def __init__(self, root, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.nodes_path = self.root / "nodes.json"
        self.lock_path = self.root / ".lock"
        self.clock = clock

    # -- plumbing --

    def now(self, now: float | None = None) -> float:
        return float(self.clock() if now is None else now)

    @contextmanager
    def _locked(self):
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.nodes_path.read_text())
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        nodes = raw.get("nodes")
        cards = raw.get("no_mac_cards")
        return {"version": 1,
                "nodes": nodes if isinstance(nodes, dict) else {},
                "no_mac_cards": cards if isinstance(cards, dict) else {}}

    def _save_state(self, state: dict) -> None:
        atomic.write_text(self.nodes_path, json.dumps(state), mode=0o600)

    def _job_path(self, job_id: str, suffix: str = ".json") -> Path:
        job_id = str(job_id or "")
        if not job_id.startswith("mj_") or not job_id[3:].isalnum():
            raise MacError("unknown_job", f"no job {job_id!r}", 404)
        return self.jobs_dir / f"{job_id}{suffix}"

    def _load_job(self, job_id: str) -> dict | None:
        try:
            row = json.loads(self._job_path(job_id).read_text())
        except MacError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        return row if isinstance(row, dict) else None

    def _save_job(self, job: dict) -> None:
        atomic.write_text(self._job_path(job["id"]), json.dumps(job), mode=0o600)

    def _all_jobs(self) -> list[dict]:
        rows = []
        if not self.jobs_dir.is_dir():
            return rows
        for path in self.jobs_dir.glob("mj_*.json"):
            if path.name.endswith(".payload.json"):
                continue
            try:
                row = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
        rows.sort(key=lambda r: (r.get("created_at") or 0.0, r.get("order") or 0,
                                 r["id"]))
        return rows

    def stamp(self) -> tuple:
        """Cheap change marker for a long-poll: moves whenever a job or the
        node file is written (atomic writes rename into the directory)."""
        out = []
        for path in (self.jobs_dir, self.nodes_path):
            try:
                out.append(path.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    @staticmethod
    def _settle(job: dict, state: str, now: float, *, reason: str | None = None,
                detail: str = "") -> None:
        job.update(state=state, settled_at=now)
        if reason is not None:
            job["reason"] = reason
        if detail:
            job["detail"] = detail

    # -- nodes --

    def register(self, machine_id, name, os, app_version, capabilities, mode,
                 presented: str | None, *, now: float | None = None) -> dict:
        """Pair a new Mac, re-pair a known one, or refresh one that proved itself.

        Returns the node's public row plus `status` (201 on a pairing, 200 on a
        refresh), `event` (`"new"` | `"re-paired"` | None) and, on a pairing
        only, the plaintext `node_secret` -- which is never stored.
        """
        at = self.now(now)
        machine_id = _text(machine_id, "machine_id", FIELD_MAX)
        name = _text(name, "name", NAME_MAX)
        os_text = _text(os, "os", FIELD_MAX, required=False)
        app_version = _text(app_version, "app_version", FIELD_MAX, required=False)
        if capabilities is None:
            capabilities = []
        if not isinstance(capabilities, list) or not all(
                c in CAPABILITIES for c in capabilities):
            raise _bad(f"capabilities are some of {', '.join(CAPABILITIES)}")
        mode = mode or "ask"
        if mode not in MODES:
            raise _bad(f"mode is one of {', '.join(MODES)}")
        with self._locked():
            state = self._load_state()
            nodes = state["nodes"]
            row = next((n for n in nodes.values()
                        if n.get("machine_id") == machine_id), None)
            proven = row is not None and self._proves(row, presented)
            secret = None
            if row is None:
                event = "new"
                node_id = f"mac_{secrets.token_hex(6)}"
                while node_id in nodes:
                    node_id = f"mac_{secrets.token_hex(6)}"
                row = {"node_id": node_id, "machine_id": machine_id,
                       "created_at": at, "order": time.time_ns(), "primary": not nodes,
                       "last_seen": None, "offline_cards": {}, "grants": {},
                       "running": []}
                nodes[node_id] = row
            elif proven:
                event = None
            else:
                event = "re-paired"
            if event is not None:
                secret = secrets.token_urlsafe(32)
                row["secret_sha256"] = hash_secret(secret)
            row.update(name=name, os=os_text, app_version=app_version,
                       capabilities=list(dict.fromkeys(capabilities)),
                       mode=mode, registered_at=at)
            self._save_state(state)
        out = self.public_node(row, at)
        out.update(status=201 if secret else 200, event=event)
        if secret:
            out["node_secret"] = secret
        return out

    @staticmethod
    def _parse(header: str | None) -> tuple[str, str] | None:
        node_id, dot, secret = str(header or "").strip().partition(".")
        if not dot or not node_id.startswith("mac_") or not secret:
            return None
        return node_id, secret

    @classmethod
    def _proves(cls, row: dict, header: str | None) -> bool:
        parsed = cls._parse(header)
        if parsed is None or parsed[0] != row.get("node_id"):
            return False
        return hmac.compare_digest(hash_secret(parsed[1]),
                                   str(row.get("secret_sha256") or ""))

    def check_node(self, header: str | None) -> str:
        """The node id `X-Deck-Node` proves, or MacError(unknown_node, 401)."""
        parsed = self._parse(header)
        row = self._load_state()["nodes"].get(parsed[0]) if parsed else None
        if row is None or not self._proves(row, header):
            raise MacError("unknown_node", "X-Deck-Node is missing or wrong", 401)
        return row["node_id"]

    def node(self, node_id: str) -> dict:
        row = self._load_state()["nodes"].get(str(node_id or ""))
        if row is None:
            raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
        return row

    def nodes(self) -> list[dict]:
        rows = list(self._load_state()["nodes"].values())
        rows.sort(key=lambda n: (n.get("created_at") or 0.0, n.get("order") or 0,
                                 n["node_id"]))
        return rows

    def public_node(self, row: dict, now: float | None = None) -> dict:
        return {"node_id": row["node_id"], "name": row.get("name", ""),
                "primary": bool(row.get("primary"))}

    def listing(self, *, now: float | None = None) -> list[dict]:
        """`GET /v1/nodes` rows."""
        at = self.now(now)
        active: dict[str, int] = {}
        for job in self._all_jobs():
            if job.get("state") in ACTIVE:
                active[job["node"]] = active.get(job["node"], 0) + 1
        return [{"node_id": n["node_id"], "name": n.get("name", ""),
                 "os": n.get("os", ""),
                 "online": presence(n, at) == "online",
                 "mode": n.get("mode", "ask"), "last_seen": n.get("last_seen"),
                 "primary": bool(n.get("primary")),
                 "running": active.get(n["node_id"], 0),
                 "control": self._control_view(n, at),
                 "screen": n.get("screen"), "perms": n.get("perms"),
                 "displays": ordered_displays(n)}
                for n in self.nodes()]

    @staticmethod
    def _control_view(node: dict, at: float) -> dict:
        control = node.get("control") or {}
        live = control_live(node, str(control.get("scope") or ""), at)
        return {"live": live, "scope": control.get("scope") if live else None,
                "until": control.get("until") if live else None}

    # -- Mac control: asks and the watch flag --

    def _with_node(self, node_id: str, fn):
        with self._locked():
            state = self._load_state()
            row = state["nodes"].get(str(node_id or ""))
            if row is None:
                raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
            out, dirty = fn(row)
            if dirty:
                self._save_state(state)
            return out

    def control_ask(self, node_id: str, desk: str) -> str | None:
        """The open "turn on Mac control" card for this desk, if any."""
        asks = self.node(node_id).get("control_asks") or {}
        return (asks.get(desk) or {}).get("card")

    def set_control_ask(self, node_id: str, desk: str, card_id: str) -> None:
        at = self.now()

        def put(row):
            row.setdefault("control_asks", {})[desk] = {"card": card_id,
                                                        "ts": at}
            return None, True
        self._with_node(node_id, put)

    def control_asks(self, node_id: str) -> list[str]:
        """Desks waiting on the owner to turn control on (the Mac shows them)."""
        return sorted((self.node(node_id).get("control_asks") or {}).keys())

    def take_control_granted(self, node_id: str) -> dict[str, str]:
        """Asks the Mac's live grant now covers: {desk: card}, removed here."""
        at = self.now()

        def take(row):
            asks = row.get("control_asks") or {}
            done = {d: str(a.get("card") or "") for d, a in asks.items()
                    if control_live(row, d, at)}
            for desk in done:
                asks.pop(desk, None)
            return done, bool(done)
        return self._with_node(node_id, take)

    def take_new_grant(self, node_id: str) -> str | None:
        """The live grant's scope the first time it is seen, else None.

        One answer per grant (a grant is its `until`), so the deck reloads
        stale desks once when he turns control on, not on every poll."""
        at = self.now()

        def take(row):
            control = row.get("control") or {}
            scope = str(control.get("scope") or "")
            if not scope or not control_live(row, scope, at):
                return None, False
            if row.get("control_seen") == control.get("until"):
                return None, False
            row["control_seen"] = control.get("until")
            return scope, True
        return self._with_node(node_id, take)

    def watch(self, node_id: str, display: int | None = None) -> None:
        """A viewer asked for a frame of `display` (None = main): keep the Mac
        capturing that display a while longer.

        Written only when the flag would lapse within half its life, so a
        viewer polling every second does not rewrite nodes.json (and wake
        every long-poll) on every frame."""
        at = self.now()

        def mark(row):
            marks = {k: v for k, v in (row.get("watch_displays") or {}).items()
                     if isinstance(v, (int, float)) and float(v) > at}
            key = None if display is None else str(display)
            fresh = watching(row, at + WATCH_TTL / 2) and (
                key is None or float(marks.get(key, 0)) > at + WATCH_TTL / 2)
            if fresh:
                return None, False
            row["watch_until"] = at + WATCH_TTL
            if key is not None:
                marks[key] = at + WATCH_TTL
            row["watch_displays"] = marks
            return None, True
        self._with_node(node_id, mark)

    def update_node(self, node_id: str, *, name=None, primary=None) -> dict:
        if name is not None:
            name = _text(name, "name", NAME_MAX)
        if primary is not None and primary is not True:
            raise _bad("primary can only be set to true; pick another Mac to move it")
        with self._locked():
            state = self._load_state()
            row = state["nodes"].get(str(node_id or ""))
            if row is None:
                raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
            if name is not None:
                row["name"] = name
            if primary:
                for other in state["nodes"].values():
                    other["primary"] = other is row
            self._save_state(state)
        return row

    def forget(self, node_id: str, *, now: float | None = None) -> list[dict]:
        """Delete a Mac. Its unsettled jobs end `refused/unknown_mac` (it can no
        longer report them). Returns those jobs."""
        at = self.now(now)
        changed = []
        with self._locked():
            state = self._load_state()
            row = state["nodes"].pop(str(node_id or ""), None)
            if row is None:
                raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
            if row.get("primary") and state["nodes"]:
                oldest = min(state["nodes"].values(),
                             key=lambda n: (n.get("created_at") or 0.0,
                                            n.get("order") or 0))
                oldest["primary"] = True
            self._save_state(state)
            for job in self._all_jobs():
                if job.get("node") == row["node_id"] and \
                        job.get("state") not in SETTLED:
                    self._settle(job, "refused", at, reason="unknown_mac",
                                 detail="that Mac was removed from the deck")
                    self._save_job(job)
                    changed.append(job)
        return changed

    def resolve_node(self, mac: str | None, *, now: float | None = None) -> dict:
        """The node a desk means. By id or name (case-insensitive); omitted =
        the single online Mac, else the primary among several online, else the
        primary (or only) Mac -- so an offline answer can still name it."""
        at = self.now(now)
        nodes = self.nodes()
        if not nodes:
            raise MacError("no_mac", "No Mac is connected to this deck. The owner "
                           "turns it on in Shaliach -> Settings -> Mac.", 404)
        wanted = str(mac or "").strip()
        if wanted:
            by_id = [n for n in nodes if n["node_id"] == wanted]
            if by_id:
                return by_id[0]
            named = [n for n in nodes
                     if str(n.get("name", "")).casefold() == wanted.casefold()]
            if len(named) == 1:
                return named[0]
            names = ", ".join(n.get("name", "") for n in nodes)
            if named:
                raise MacError("ambiguous_mac", f"Several Macs are called "
                               f"{wanted!r}; use a node id. Macs: "
                               + ", ".join(n["node_id"] for n in named), 409)
            raise MacError("unknown_mac", f"No Mac called {wanted!r}. Macs: {names}",
                           404)
        online = [n for n in nodes if presence(n, at) == "online"]
        if len(online) == 1:
            return online[0]
        pool = online or nodes
        primary = [n for n in pool if n.get("primary")]
        if primary:
            return primary[0]
        if len(pool) == 1:
            return pool[0]
        raise MacError("ambiguous_mac", "Several Macs are online and none is "
                       "primary; name one with mac=. Macs: "
                       + ", ".join(n.get("name", "") for n in pool), 409)

    # -- offline cards (the desk tool files them; the router resolves them) --

    def offline_card(self, node_id: str, desk: str) -> str | None:
        row = self._load_state()["nodes"].get(node_id) or {}
        return (row.get("offline_cards") or {}).get(desk)

    def set_offline_card(self, node_id: str, desk: str, card_id: str) -> None:
        with self._locked():
            state = self._load_state()
            row = state["nodes"].get(node_id)
            if row is None:
                raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
            row.setdefault("offline_cards", {})[desk] = card_id
            self._save_state(state)

    def take_back_online(self, node_id: str) -> dict[str, str]:
        """Pop every stored offline card for a Mac that is not paused.

        Called by the router after each poll: a poll IS the Mac being back.
        Popping under the lock means two overlapping polls cannot both tell a
        desk -- exactly one message per card.
        """
        with self._locked():
            state = self._load_state()
            row = state["nodes"].get(node_id)
            if row is None or row.get("mode") == "paused":
                return {}
            cards = dict(row.get("offline_cards") or {})
            if cards:
                row["offline_cards"] = {}
                self._save_state(state)
            return cards

    def no_mac_card(self, desk: str, *, now: float | None = None) -> str | None:
        """The `no_mac` card filed for this desk in the last 24 h, if any."""
        at = self.now(now)
        entry = self._load_state()["no_mac_cards"].get(desk) or {}
        if at - float(entry.get("ts") or 0.0) < NO_MAC_CARD_TTL:
            return entry.get("id")
        return None

    def set_no_mac_card(self, desk: str, card_id: str, *,
                        now: float | None = None) -> None:
        at = self.now(now)
        with self._locked():
            state = self._load_state()
            state["no_mac_cards"][desk] = {"id": card_id, "ts": at}
            self._save_state(state)

    # -- jobs --

    def enqueue(self, desk, kind, args, *, mac: str | None,
                timeout_s=TIMEOUT_DEFAULT, background=False,
                now: float | None = None) -> dict:
        """Queue a job for a Mac. The only way a job is ever created.

        Refuses (never queues) for an offline or paused Mac, so nothing waits
        on a machine that is not there. `MacError.node` names that Mac.
        """
        at = self.now(now)
        desk = _text(desk, "desk", NAME_MAX)
        args = validate_args(kind, args)
        timeout_s = _int(timeout_s, "timeout_s", 1, TIMEOUT_MAX)
        background = _bool(background, "background")
        with self._locked():
            self._sweep_locked(at)
            node = self.resolve_node(mac, now=at)
            seen = presence(node, at)
            if seen == "paused":
                raise MacError("mac_paused", f"{node.get('name')} has Mac access "
                               "paused.", 409, node=node)
            if seen == "offline":
                raise MacError("mac_offline", f"{node.get('name')} is offline or "
                               f"asleep (last seen {_clock_str(node.get('last_seen'))}).",
                               409, node=node)
            caps = node.get("capabilities") or []
            if caps and kind not in caps:
                raise MacError("capability_off", f"{node.get('name')} has "
                               f"{kind!r} turned off.", 409, node=node)
            jobs = self._all_jobs()
            recent = [j for j in jobs if j.get("desk") == desk
                      and at - float(j.get("created_at") or 0.0) < 60.0]
            limit = OWNER_PER_MIN if desk == OWNER_DESK else PER_DESK_PER_MIN
            if len(recent) >= limit:
                raise MacError("rate_limited", f"at most {PER_DESK_PER_MIN} Mac "
                               "jobs a minute per desk; wait and batch the work",
                               429, node=node)
            queued = [j for j in jobs if j.get("node") == node["node_id"]
                      and j.get("state") == "queued"]
            if len(queued) >= QUEUE_MAX_PER_NODE:
                raise MacError("node_busy", f"{node.get('name')} already has "
                               f"{QUEUE_MAX_PER_NODE} jobs waiting", 429, node=node)
            job = {"id": f"mj_{secrets.token_hex(6)}", "node": node["node_id"],
                   "desk": desk, "kind": kind, "args": args,
                   "timeout_s": timeout_s, "background": background,
                   "created_at": at, "order": time.time_ns(),
                   "summary": summary_of(kind, args),
                   "state": "queued", "claimed_at": None, "started_at": None,
                   "settled_at": None, "last_event_at": None, "last_seq": 0,
                   "cancel_requested": False, "exit": None, "signal": None,
                   "reason": None, "detail": "", "duration_ms": None,
                   "pid": None, "cwd": None, "sandboxed": None,
                   "out": {s: {"total": 0, "dropped": 0} for s in STREAMS}}
            while self._job_path(job["id"]).exists():
                job["id"] = f"mj_{secrets.token_hex(6)}"
            self._save_job(job)
        return job

    def poll(self, node_id, free_slots, running, mode, grants,
             now: float | None = None, *, control=None, screen=None,
             perms=None, displays=None) -> tuple[list[dict], list[str]]:
        """A Mac's heartbeat and claim. Returns (jobs to run, ids to kill).

        `control`, `screen` and `perms` are what the Mac says of itself: its
        live control grant (null when none), the pixel size of its live view,
        and its Accessibility / Screen Recording permissions. Mirrored as
        sent; an app that predates them sends nothing and has no control."""
        at = self.now(now)
        control = _control_report(control)
        screen = _screen_report(screen)
        perms = _perms_report(perms)
        displays = _displays_report(displays)
        free = _int(free_slots, "free_slots", 0, 1 << 16)
        if running is None:
            running = []
        if not isinstance(running, list) or not all(isinstance(r, str)
                                                    for r in running):
            raise _bad("running is a list of job ids")
        if mode not in MODES:
            raise _bad(f"mode is one of {', '.join(MODES)}")
        grants = grants or {}
        if not isinstance(grants, dict):
            raise _bad("grants is an object keyed by desk")
        with self._locked():
            state = self._load_state()
            row = state["nodes"].get(str(node_id or ""))
            if row is None:
                raise MacError("unknown_node", f"no Mac {node_id!r}", 404)
            row.update(last_seen=at, mode=mode, running=list(running)[:64],
                       control=control, screen=screen, perms=perms,
                       displays=displays,
                       grants={str(d): g for d, g in list(grants.items())[:256]
                               if isinstance(g, dict)
                               and g.get("state") in GRANT_STATES})
            self._save_state(state)
            self._sweep_locked(at)
            jobs = [j for j in self._all_jobs() if j.get("node") == row["node_id"]]
            by_id = {j["id"]: j for j in jobs}
            cancel = []
            for job in jobs:
                if job.get("state") in ACTIVE and job.get("cancel_requested"):
                    cancel.append(job["id"])
            for job_id in running:
                job = by_id.get(job_id)
                if job is None or job.get("state") in SETTLED:
                    if job_id not in cancel:   # the deck gave up on it: kill it
                        cancel.append(job_id)
                elif job.get("state") in ACTIVE:
                    job["last_event_at"] = at
                    self._save_job(job)
            handed: list[dict] = []
            if mode == "paused":
                return handed, cancel
            active = [j for j in jobs if j.get("state") in ACTIVE]
            per_desk: dict[str, int] = {}
            for job in active:
                per_desk[job["desk"]] = per_desk.get(job["desk"], 0) + 1
            room = min(free, PER_NODE_CONCURRENT - len(active))
            for job in jobs:
                if room <= 0:
                    break
                if job.get("state") != "queued":
                    continue
                if per_desk.get(job["desk"], 0) >= PER_DESK_CONCURRENT:
                    continue   # waits: this desk is at its limit on this Mac
                job.update(state="claimed", claimed_at=at, last_event_at=at)
                self._save_job(job)
                per_desk[job["desk"]] = per_desk.get(job["desk"], 0) + 1
                room -= 1
                handed.append(wire(job))
        return handed, cancel

    def record_events(self, node_id, job_id, events, *,
                      now: float | None = None) -> bool:
        """Apply what a Mac reported. Returns the cancel flag (kill it now)."""
        at = self.now(now)
        if not isinstance(events, list):
            raise _bad("events is a list")
        with self._locked():
            job = self._load_job(job_id)
            if job is None:
                raise MacError("unknown_job", f"no job {job_id!r}", 404)
            if job.get("node") != node_id:
                raise MacError("not_your_job", "that job is another Mac's", 403)
            fresh = []
            last = int(job.get("last_seq") or 0)
            for event in events:
                if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
                    raise _bad(f"an event is {{seq, type, data}}; type is one of "
                               f"{', '.join(EVENT_TYPES)}")
                seq = event.get("seq")
                if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                    raise _bad("seq is a positive integer")
                if seq > last:
                    fresh.append(event)
                    last = seq
            fresh.sort(key=lambda e: e["seq"])
            if job.get("state") in SETTLED:
                if fresh or not events:
                    raise MacError("job_settled", f"job is already "
                                   f"{job.get('state')}", 409)
                return bool(job.get("cancel_requested"))   # a retried result
            if job.get("state") == "queued":
                raise MacError("not_your_job", "that job was never claimed", 403)
            for event in fresh:
                self._apply(job, event, at)
                job["last_seq"] = event["seq"]
                if job.get("state") in SETTLED:
                    break
            job["last_event_at"] = at
            self._save_job(job)
            return bool(job.get("cancel_requested"))

    def _apply(self, job: dict, event: dict, at: float) -> None:
        kind, data = event["type"], event.get("data")
        if kind == "awaiting_grant":
            job["state"] = "awaiting_grant"
        elif kind == "started":
            data = data if isinstance(data, dict) else {}
            job.update(state="running", started_at=at,
                       pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
                       cwd=str(data.get("cwd"))[:4096] if data.get("cwd") else None,
                       sandboxed=bool(data.get("sandboxed")))
        elif kind in STREAMS:
            if not isinstance(data, str):
                raise _bad(f"{kind} data is a string")
            if len(data) > CHUNK_MAX:
                raise _bad(f"an output chunk is at most {CHUNK_MAX} characters")
            self._append(job, kind, data)
        else:  # result
            if not isinstance(data, dict) or data.get("state") not in RESULT_STATES:
                raise _bad(f"result.state is one of {', '.join(RESULT_STATES)}")
            payload = data.get("payload") or {}
            if not isinstance(payload, dict):
                raise _bad("result.payload is an object")
            blob = json.dumps(payload)
            if len(blob) > PAYLOAD_MAX:
                raise MacError("too_large", "result payload is too large", 413)
            exit_code = data.get("exit")
            job.update(
                exit=exit_code if isinstance(exit_code, int)
                and not isinstance(exit_code, bool) else None,
                signal=data.get("signal") if data.get("signal") in ("TERM", "KILL")
                else None,
                duration_ms=data.get("duration_ms")
                if isinstance(data.get("duration_ms"), int) else None)
            reason = data.get("reason")
            self._settle(job, data["state"], at,
                         reason=str(reason)[:64] if reason else None,
                         detail=str(data.get("detail") or "")[:2000])
            if payload:
                atomic.write_text(self._job_path(job["id"], ".payload.json"), blob,
                                  mode=0o600)

    def _append(self, job: dict, stream: str, data: str) -> None:
        """Append to a stream, keeping head + newest within OUT_KEEP bytes."""
        raw = data.encode("utf-8")
        path = self._job_path(job["id"], f".{stream}")
        meta = job.setdefault("out", {}).setdefault(stream,
                                                     {"total": 0, "dropped": 0})
        with open(path, "ab") as fh:
            fh.write(raw)
        meta["total"] = int(meta.get("total", 0)) + len(raw)
        size = path.stat().st_size
        if size > OUT_KEEP:
            kept = path.read_bytes()
            trimmed = kept[:OUT_HEAD] + kept[len(kept) - (OUT_KEEP - OUT_HEAD):]
            meta["dropped"] = int(meta.get("dropped", 0)) + len(kept) - len(trimmed)
            atomic.write_bytes(path, trimmed, mode=0o600)

    def job(self, job_id: str) -> dict:
        """One job's row, plus its result `payload` once settled."""
        row = self._load_job(job_id)
        if row is None:
            raise MacError("unknown_job", f"no job {job_id!r}", 404)
        try:
            row["payload"] = json.loads(
                self._job_path(job_id, ".payload.json").read_text())
        except (OSError, json.JSONDecodeError):
            row["payload"] = {}
        return row

    def output(self, job_id: str, stream: str, since: int) -> tuple[str, int, bool]:
        """(text since byte offset `since`, next offset, truncated).

        Offsets are positions in the whole stream as the Mac sent it. When the
        middle was dropped to stay under OUT_KEEP, a read that spans the gap
        comes back `truncated` and simply skips it."""
        if stream not in STREAMS:
            raise _bad("stream is stdout or stderr")
        row = self._load_job(job_id)
        if row is None:
            raise MacError("unknown_job", f"no job {job_id!r}", 404)
        meta = (row.get("out") or {}).get(stream) or {}
        total, dropped = int(meta.get("total", 0)), int(meta.get("dropped", 0))
        try:
            stored = self._job_path(job_id, f".{stream}").read_bytes()
        except OSError:
            stored = b""
        since = max(0, min(int(since or 0), total))
        truncated = False
        if dropped == 0:
            part = stored[since:]
        elif since < OUT_HEAD:
            part, truncated = stored[since:], True
        elif since < OUT_HEAD + dropped:
            part, truncated = stored[OUT_HEAD:], True
        else:
            part = stored[since - dropped:]
        return part.decode("utf-8", errors="ignore"), total, truncated

    def request_cancel(self, job_id: str, desk: str, *,
                       now: float | None = None) -> dict:
        at = self.now(now)
        with self._locked():
            job = self._load_job(job_id)
            if job is None:
                raise MacError("unknown_job", f"no job {job_id!r}", 404)
            if job.get("desk") != desk:
                raise MacError("not_your_job", "that job is another desk's", 403)
            if job.get("state") == "queued":
                self._settle(job, "cancelled", at, reason="cancelled",
                             detail="cancelled before the Mac picked it up")
            elif job.get("state") in ACTIVE:
                job["cancel_requested"] = True
            else:
                return job
            self._save_job(job)
            return job

    def node_jobs(self, node_id: str, limit: int = 50) -> list[dict]:
        self.node(node_id)
        rows = [j for j in self._all_jobs() if j.get("node") == node_id]
        rows.reverse()
        return [audit_row(j) for j in rows[:max(1, min(int(limit), 500))]]

    # -- time passing --

    def sweep(self, now: float | None = None) -> list[dict]:
        """Expire, lose and purge. Returns the jobs whose state changed."""
        at = self.now(now)
        with self._locked():
            return self._sweep_locked(at)

    def _sweep_locked(self, at: float) -> list[dict]:
        nodes = self._load_state()["nodes"]
        changed = []
        for job in self._all_jobs():
            state = job.get("state")
            if state == "queued":
                node = nodes.get(job.get("node"))
                created = float(job.get("created_at") or 0.0)
                seen = (node or {}).get("last_seen")
                polled_since = seen is not None and float(seen) >= created
                if node is None:
                    self._settle(job, "refused", at, reason="unknown_mac",
                                 detail="that Mac was removed from the deck")
                elif at - created >= CLAIM_WAIT and not polled_since:
                    self._settle(job, "expired", at, reason="mac_not_answering",
                                 detail=f"{node.get('name')} did not pick the job "
                                 f"up within {int(CLAIM_WAIT)} s")
                elif at - created >= CLAIM_WAIT + float(job.get("timeout_s") or 0):
                    self._settle(job, "expired", at, reason="node_busy",
                                 detail=f"{node.get('name')} was busy with other "
                                 "jobs for the whole timeout")
                else:
                    continue
            elif state in ACTIVE:
                last = float(job.get("last_event_at") or job.get("claimed_at") or 0.0)
                if at - last < LOST_AFTER:
                    continue
                self._settle(job, "lost", at, reason="lost",
                             detail=f"the Mac stopped reporting on this job for "
                             f"{int(LOST_AFTER)} s")
            elif state in SETTLED:
                keep = INPUT_KEEP if job.get("kind") == "input" \
                    else KEEP_DAYS * 86400
                if at - float(job.get("settled_at") or at) > keep:
                    for suffix in (".json", ".stdout", ".stderr", ".payload.json"):
                        try:
                            os.unlink(self._job_path(job["id"], suffix))
                        except OSError:
                            pass
                continue
            else:
                continue
            self._save_job(job)
            changed.append(job)
        return changed
