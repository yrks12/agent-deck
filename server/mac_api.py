"""The Mac bridge's wire: the routes a Mac dials out to, and the owner's view (MB1).

The Mac never listens. It registers (`POST /v1/nodes`), then long-polls
`/v1/nodes/{id}/poll` forever: the poll is both its heartbeat and how it
claims jobs. It reports each job's progress to `.../jobs/{job_id}/events` and
tells the deck what the owner tapped on a grant card via `.../grants`.

**No route here creates a job a desk asked for.** Only a desk's `mac` MCP
process on the box enqueues those (`mac_nodes.Store.enqueue`). The one route
that queues anything is the owner's own `screen/input` (his taps on the live
view). Owner, 2026-10-01 21:00: watching and driving his OWN Mac need no
agent control grant -- the grant is what lets AGENTS in. His input is still
refused before anything is queued when the Mac is asleep, paused, or lacks
Accessibility, and the Mac refuses it itself when Mac access is off/paused.
The bearer token is therefore as strong as his hands on that Mac.
`tests/test_mac_api.py` pins the exact (method, path) set.

**The live view** (Mac control, 2026-10-01) is the desk screen viewer's shape
under `/v1/nodes/{id}`: `screen` (status), `screen.jpg` (the newest frame
with its age) and `screen/input`. The Mac never listens, so it PUSHES frames
(`screen/frame`) -- only while someone is watching: a frame request raises a
short watch flag that wakes the long-poll, and every upload is answered with
whether anyone still is. Frames are kept in memory only, newest per Mac.

Auth: every route needs the `/v1` bearer (`api._authorise`); the Mac's own
routes also need `X-Deck-Node: <node_id>.<node_secret>`. Refusals are the
deck's usual `{"ok": false, "reason", "detail"}`, built here rather than
raised, so the router behaves the same on any app it is mounted on.

Everything that reaches a desk or the owner goes through injected callables:
`tell(desk, text)` (office line + wake, reason `mac`), `owner_line(text)` (the
owner's thread), and optionally `queue(desk, text)` (office line, no wake).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request, Response

from . import handoff, mac_nodes, owner
from .api import Refused, StrictJSON, _authorise

MAX_WAIT = 25.0
WAKE_CHECK = 0.25   # s between cheap "did a job file change?" checks in a long-poll
NODE_HEADER = "x-deck-node"

GRANT_DECISIONS = ("hour", "always", "deny", "revoke")

FRAME_MAX = 4 << 20       # bytes per live-view JPEG
FRAME_STALE = 10.0        # s: an older frame is not served as live
FULL_ACCESS_COPY = ("Turn on Full access on your Mac (Agent Deck → Settings → "
                    "Mac) to control it from your phone.")
INPUT_WAIT = 10.0         # s his tap waits for the Mac to finish it

#: The exact route set. The route-sweep test compares against its own copy.
ROUTES = (
    ("POST", "/v1/nodes"),
    ("GET", "/v1/nodes"),
    ("PATCH", "/v1/nodes/{node_id}"),
    ("DELETE", "/v1/nodes/{node_id}"),
    ("GET", "/v1/nodes/{node_id}/jobs"),
    ("POST", "/v1/nodes/{node_id}/poll"),
    ("POST", "/v1/nodes/{node_id}/jobs/{job_id}/events"),
    ("POST", "/v1/nodes/{node_id}/grants"),
    ("GET", "/v1/nodes/{node_id}/screen"),
    ("GET", "/v1/nodes/{node_id}/screen.jpg"),
    ("POST", "/v1/nodes/{node_id}/screen/input"),
    ("POST", "/v1/nodes/{node_id}/screen/frame"),
)


def control_text(mac_name: str, until) -> str:
    """The one line a desk gets when the owner turns Mac control on for it."""
    end = time.strftime("%H:%M", time.localtime(float(until))) if until \
        else "he stops it"
    return (f"[Agent Deck] {owner.title()} turned on Mac control for you on "
            f"{mac_name} until {end}. Retry what you were doing: take "
            "mcp__mac__screenshot first, then click and type in its pixels.")


def reload_note(mac_name: str, until) -> str:
    """What a live desk reads as it reloads to pick up the control tools."""
    end = time.strftime("%H:%M", time.localtime(float(until))) if until \
        else "he stops it"
    return (f"[Agent Deck] {owner.title()} turned on Mac control on {mac_name} "
            f"(until {end}). Your mac tools were reloaded to include it: "
            "mcp__mac__screenshot first, then mcp__mac__click, move, drag, "
            "scroll, type_text and key at that screenshot's pixels.")


def desks_to_reload(scope: str, desks, *, stale: Callable[[str], bool],
                    live: Callable[[str], bool]) -> list[str]:
    """Live Claude desks the grant covers whose mac tools predate it.

    `desks` is (name, engine) pairs. An asleep desk is left alone: its next
    wake starts a fresh mac process with the current tools anyway."""
    return [name for name, engine in desks
            if engine == "claude" and (scope == "all" or name == scope)
            and stale(name) and live(name)]


def grant_text(decision: str, mac_name: str) -> str:
    """The one line a desk gets when the owner answers its grant card."""
    who = owner.title()
    if decision == "hour":
        return (f"[Agent Deck] {who} allowed you to use his Mac ({mac_name}) for "
                "1 hour. Retry what you were doing.")
    if decision == "always":
        return (f"[Agent Deck] {who} allowed you to use his Mac ({mac_name}) "
                "always. Retry what you were doing.")
    if decision == "deny":
        return (f"[Agent Deck] {who} said no to you using his Mac ({mac_name}). "
                "Do not ask again today; finish without it or tell whoever you "
                "report to what you could not do.")
    return (f"[Agent Deck] {who} took back your access to his Mac ({mac_name}). "
            "Do not use it until he allows you again.")


def _refuse(status: int, reason: str, detail: str = "",
            headers: dict | None = None) -> StrictJSON:
    return StrictJSON({"ok": False, "reason": reason, "detail": detail or reason},
                      status_code=status, headers=headers)


def _from_error(err: mac_nodes.MacError) -> StrictJSON:
    return _refuse(err.status, err.reason, err.detail)


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise mac_nodes.MacError("bad_input", "body must be a JSON object", 400)
    if not isinstance(payload, dict):
        raise mac_nodes.MacError("bad_input", "body must be a JSON object", 400)
    return payload


def build_router(store: mac_nodes.Store, tell: Callable[[str, str], object],
                 owner_line: Callable[[str], object], *,
                 queue: Callable[[str, str], object] | None = None,
                 handoffs_path: Path | None = None,
                 reload_stale: Callable[[str], object] | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)
    #: node id -> {"jpeg", "at", "width", "height"}: the newest live frame.
    frames: dict[str, dict] = {}

    def _handoffs() -> Path:
        return Path(handoffs_path or handoff.DEFAULT_PATH)

    def _say(fn, *args) -> bool:
        """A delivery never fails the request that caused it."""
        if fn is None:
            return False
        try:
            result = fn(*args)
        except Exception as exc:  # the Mac's answer must not hinge on the office
            print(f"[agent-deck] mac: delivery failed: {exc!r}")
            return False
        if isinstance(result, dict):
            return bool(result.get("ok", True))
        return result is not False

    def _resume(node: dict, desk: str, card_id: str) -> None:
        """The Mac is back: close the desk's offline card and tell it, once."""
        path = _handoffs()
        card = next((h for h in handoff.load(path)
                     if h.id.lower() == str(card_id).lower()), None)
        if card is not None and card.status == "waiting":
            card = handoff.resolve(path, card.id, "done")
            text = handoff.resume_message(card, "done")
        else:
            text = (f"[Agent Deck] {node.get('name', 'The Mac')} is back online. "
                    "Retry what you were doing.")
        _say(tell, desk, text)

    def _back_online(node_id: str) -> None:
        cards = store.take_back_online(node_id)
        if not cards:
            return
        node = store.node(node_id)
        for desk, card_id in cards.items():
            try:
                _resume(node, desk, card_id)
            except Exception as exc:
                print(f"[agent-deck] mac: resuming {desk} failed: {exc!r}")

    def _control_granted(node_id: str) -> None:
        """His grant now covers desks that were refused: close, tell, once."""
        taken = store.take_control_granted(node_id)
        if not taken:
            return
        node = store.node(node_id)
        until = (node.get("control") or {}).get("until")
        path = _handoffs()
        for desk, card_id in taken.items():
            card = next((h for h in handoff.load(path)
                         if h.id.lower() == str(card_id).lower()), None)
            if card is not None and card.status == "waiting":
                handoff.resolve(path, card.id, "done")
            _say(tell, desk, control_text(node.get("name", "his Mac"), until))

    def _new_grant(node_id: str) -> None:
        """He just turned control on: desks running older mac tools reload."""
        scope = store.take_new_grant(node_id)
        if scope and reload_stale is not None:
            _say(reload_stale, scope)

    def _extras(node_id: str) -> dict:
        node = store.node(node_id)
        asks = node.get("control_asks") or {}
        if asks:   # only asks whose card still waits: a closed card leaves his Mac
            waiting = {h.id.lower() for h in handoff.load(_handoffs())
                       if h.status == "waiting"}
            asks = {d: a for d, a in asks.items()
                    if str((a or {}).get("card") or "").lower() in waiting}
        return {"watch": mac_nodes.watching(node, store.now()),
                "control_asks": sorted(asks)}

    def _auth(request: Request) -> StrictJSON | None:
        try:
            _authorise(request)
        except Refused as exc:
            return StrictJSON(exc.detail, status_code=exc.status_code,
                              headers=getattr(exc, "headers", None))
        return None

    def _node_auth(request: Request, node_id: str) -> StrictJSON | None:
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            proven = store.check_node(request.headers.get(NODE_HEADER))
        except mac_nodes.MacError as err:
            return _from_error(err)
        if proven != node_id:
            return _refuse(401, "unknown_node", "X-Deck-Node is not this Mac's")
        return None

    # ── the Mac's routes ─────────────────────────────────────────────────────

    @router.post("/nodes")
    async def register(request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            out = await asyncio.to_thread(
                store.register, body.get("machine_id"), body.get("name"),
                body.get("os"), body.get("app_version"),
                body.get("capabilities"), body.get("mode"),
                request.headers.get(NODE_HEADER))
        except mac_nodes.MacError as err:
            return _from_error(err)
        status, event = out.pop("status"), out.pop("event")
        if event == "new":
            await asyncio.to_thread(_say, owner_line,
                                    f"A Mac connected as *{out['name']}*")
        elif event == "re-paired":
            await asyncio.to_thread(_say, owner_line, f"*{out['name']}* re-paired")
        return StrictJSON(out, status_code=status)

    @router.post("/nodes/{node_id}/poll")
    async def poll(node_id: str, request: Request):
        refused = await asyncio.to_thread(_node_auth, request, node_id)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            wait = body.get("wait", MAX_WAIT)
            if isinstance(wait, bool) or not isinstance(wait, (int, float)):
                raise mac_nodes.MacError("bad_input", "wait is a number of seconds")
            wait = max(0.0, min(float(wait), MAX_WAIT))
            args = (node_id, body.get("free_slots", 0), body.get("running"),
                    body.get("mode", "ask"), body.get("grants"))
            kwargs = {"control": body.get("control"),
                      "screen": body.get("screen"), "perms": body.get("perms")}
            deadline = time.monotonic() + wait
            jobs, cancel = await asyncio.to_thread(store.poll, *args, **kwargs)
            await asyncio.to_thread(_back_online, node_id)
            await asyncio.to_thread(_control_granted, node_id)
            await asyncio.to_thread(_new_grant, node_id)
            extras = await asyncio.to_thread(_extras, node_id)
            first = extras
            stamp = store.stamp()
            while not jobs and not cancel and extras == first:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(min(WAKE_CHECK, left))
                now_stamp = store.stamp()
                if now_stamp != stamp:
                    jobs, cancel = await asyncio.to_thread(store.poll, *args,
                                                           **kwargs)
                    extras = await asyncio.to_thread(_extras, node_id)
                    stamp = store.stamp()
        except mac_nodes.MacError as err:
            return _from_error(err)
        return {"jobs": jobs, "cancel": cancel, "server_ts": store.now(),
                **extras}

    @router.post("/nodes/{node_id}/jobs/{job_id}/events")
    async def events(node_id: str, job_id: str, request: Request):
        refused = await asyncio.to_thread(_node_auth, request, node_id)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            cancel = await asyncio.to_thread(store.record_events, node_id, job_id,
                                             body.get("events"))
        except mac_nodes.MacError as err:
            return _from_error(err)
        return {"ok": True, "cancel": cancel}

    @router.post("/nodes/{node_id}/grants")
    async def grants(node_id: str, request: Request):
        refused = await asyncio.to_thread(_node_auth, request, node_id)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            desk = body.get("desk")
            if not isinstance(desk, str) or not desk.strip() or len(desk) > 64:
                raise mac_nodes.MacError("bad_input", "desk is required")
            decision = body.get("decision")
            if decision not in GRANT_DECISIONS:
                raise mac_nodes.MacError(
                    "bad_input", f"decision is one of {', '.join(GRANT_DECISIONS)}")
            until = body.get("until")
            if until is not None and (isinstance(until, bool)
                                      or not isinstance(until, (int, float))):
                raise mac_nodes.MacError("bad_input", "until is a timestamp or null")
            node = await asyncio.to_thread(store.node, node_id)
        except mac_nodes.MacError as err:
            return _from_error(err)
        text = grant_text(decision, node.get("name", "his Mac"))
        if decision == "revoke":
            told = await asyncio.to_thread(_say, queue, desk.strip(), text)
        else:
            told = await asyncio.to_thread(_say, tell, desk.strip(), text)
        return {"ok": True, "told": told}

    @router.post("/nodes/{node_id}/screen/frame")
    async def screen_frame_upload(node_id: str, request: Request):
        """The Mac pushes its newest live-view frame; told if anyone watches."""
        refused = await asyncio.to_thread(_node_auth, request, node_id)
        if refused is not None:
            return refused
        data = await request.body()
        if len(data) > FRAME_MAX:
            return _refuse(413, "too_large", f"a frame is at most {FRAME_MAX} "
                           "bytes")
        if not data.startswith(b"\xff\xd8"):
            return _refuse(400, "bad_input", "a frame is one JPEG")
        try:
            width = int(request.headers.get("x-frame-width", ""))
            height = int(request.headers.get("x-frame-height", ""))
            mac_nodes.validate_args("input", {
                "action": "move", "x": 0, "y": 0,
                "space": {"width": width, "height": height}})
        except (ValueError, mac_nodes.MacError):
            return _refuse(400, "bad_input", "X-Frame-Width and X-Frame-Height "
                           "are the frame's pixel size")
        frames[node_id] = {"jpeg": data, "at": store.now(), "width": width,
                           "height": height}
        node = await asyncio.to_thread(store.node, node_id)
        return {"ok": True, "watch": mac_nodes.watching(node, store.now())}

    # ── the owner's routes (bearer only) ─────────────────────────────────────

    def _fresh_frame(node_id: str) -> dict | None:
        frame = frames.get(node_id)
        if frame and store.now() - frame["at"] <= FRAME_STALE:
            return frame
        return None

    def _screen_why(node: dict) -> tuple[str, str]:
        """(reason, sentence) for no live picture of this Mac, or ("", "").

        Watching his own Mac is HIS (owner, 2026-10-01 21:00): no agent's
        control grant is asked for. Only what the Mac itself is missing."""
        name = node.get("name") or "your Mac"
        presence = mac_nodes.presence(node, store.now())
        if presence == "paused":
            return ("mac_paused", f"Agent Deck is paused on {name}. Resume it "
                    "on the Mac to see it live.")
        if presence != "online":
            return ("mac_asleep", f"{name} is asleep or offline. Wake it and "
                    "make sure Agent Deck is open on it.")
        if (node.get("perms") or {}).get("screen_recording") is False:
            return ("screen_recording_off", "Screen Recording is off for Agent "
                    f"Deck on {name}. On the Mac open Agent Deck Settings -> "
                    "Mac, or System Settings -> Privacy & Security -> Screen "
                    "Recording, and turn it on.")
        return "", ""

    @router.get("/nodes/{node_id}/screen")
    async def screen_status(node_id: str, request: Request):
        """The desk screen viewer's status shape, for his Mac."""
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            node = await asyncio.to_thread(store.node, node_id)
        except mac_nodes.MacError as err:
            return _from_error(err)
        reason, why = _screen_why(node)
        frame = _fresh_frame(node_id)
        size = (frame or node.get("screen") or {"width": 1440, "height": 900})
        name = node.get("name", "")
        listed = store._control_view(node, store.now())
        return {"desk": name, "display": name,
                "computer": {"running": not why, "image": "macOS",
                             "container": node_id},
                "width": size["width"], "height": size["height"],
                "stale_after": FRAME_STALE,
                "frame_url": f"/v1/nodes/{node_id}/screen.jpg",
                "input_url": f"/v1/nodes/{node_id}/screen/input",
                "generated_at": time.time(), "detail": why, "reason": reason,
                "control": listed, "perms": node.get("perms")}

    @router.get("/nodes/{node_id}/screen.jpg")
    async def screen_jpg(node_id: str, request: Request):
        """The newest frame with its age; asking for it keeps the Mac capturing."""
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            await asyncio.to_thread(store.watch, node_id)
            node = await asyncio.to_thread(store.node, node_id)
        except mac_nodes.MacError as err:
            return _from_error(err)
        frame = _fresh_frame(node_id)
        if frame is None:
            reason, why = _screen_why(node)
            if reason:
                return _refuse(409, reason, why)
            return _refuse(409, "frame_pending", "Waiting for a picture from "
                           f"{node.get('name') or 'your Mac'}. It takes a "
                           "second or two.")
        return Response(content=frame["jpeg"], media_type="image/jpeg", headers={
            "Cache-Control": "no-store",
            "X-Frame-Age": f"{max(0.0, store.now() - frame['at']):.3f}",
            "X-Frame-Display": node.get("name", "")})

    def _his_input(node_id: str, body: dict) -> StrictJSON | dict:
        node = store.node(node_id)
        # His own hands need no agent grant; only what the Mac lacks stops them.
        reason, why = _screen_why(node)
        if reason and reason != "screen_recording_off":
            return _refuse(409, reason, why)
        # No grant is asked, but a leaked token alone must not drive his Mac:
        # input needs the Mac itself in Full access. Watching does not.
        if node.get("mode") != "full":
            return _refuse(409, "full_access_required", FULL_ACCESS_COPY)
        if (node.get("perms") or {}).get("accessibility") is False:
            return _refuse(409, "accessibility_off", "Accessibility is off for "
                           f"Agent Deck on {node.get('name') or 'your Mac'}, so "
                           "it cannot click or type. On the Mac open Agent Deck "
                           "Settings -> Mac, or System Settings -> Privacy & "
                           "Security -> Accessibility, and turn it on.")
        gesture = {k: v for k, v in body.items() if k in (
            "action", "x", "y", "to_x", "to_y", "button", "count", "dx", "dy",
            "text", "key")}
        frame = frames.get(node_id) or node.get("screen") or {}
        if gesture.get("action") not in ("type", "key"):
            gesture["space"] = {"width": frame.get("width"),
                                "height": frame.get("height")}
        job = store.enqueue(mac_nodes.OWNER_DESK, "input", gesture,
                            mac=node_id, timeout_s=int(INPUT_WAIT))
        deadline = time.monotonic() + INPUT_WAIT
        while time.monotonic() < deadline:
            row = store.job(job["id"])
            if row.get("state") in mac_nodes.SETTLED:
                if row["state"] == "done":
                    return {"ok": True, "action": gesture.get("action")}
                return _refuse(409, row.get("reason") or row["state"],
                               row.get("detail") or row["state"])
            time.sleep(0.03)
        try:
            store.request_cancel(job["id"], mac_nodes.OWNER_DESK)
        except mac_nodes.MacError:
            pass
        return _refuse(504, "timed_out", "The Mac did not finish that in time.")

    @router.post("/nodes/{node_id}/screen/input")
    async def screen_input(node_id: str, request: Request):
        """His own tap, keystroke or text, from the live view on his phone."""
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            return await asyncio.to_thread(_his_input, node_id, body)
        except mac_nodes.MacError as err:
            return _from_error(err)

    @router.get("/nodes")
    async def list_nodes(request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        await asyncio.to_thread(store.sweep)
        return {"nodes": await asyncio.to_thread(store.listing)}

    def _row(node_id: str) -> dict:
        return next(r for r in store.listing() if r["node_id"] == node_id)

    @router.patch("/nodes/{node_id}")
    async def patch_node(node_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            unknown = set(body) - {"name", "primary"}
            if unknown or not body:
                raise mac_nodes.MacError("bad_input", "patch takes name and/or primary")
            await asyncio.to_thread(store.update_node, node_id,
                                    name=body.get("name"),
                                    primary=body.get("primary"))
        except mac_nodes.MacError as err:
            return _from_error(err)
        return await asyncio.to_thread(_row, node_id)

    @router.delete("/nodes/{node_id}")
    async def delete_node(node_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            refused_jobs = await asyncio.to_thread(store.forget, node_id)
        except mac_nodes.MacError as err:
            return _from_error(err)
        return {"ok": True, "refused": [j["id"] for j in refused_jobs]}

    @router.get("/nodes/{node_id}/jobs")
    async def node_jobs(node_id: str, request: Request, limit: int = 50):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            rows = await asyncio.to_thread(store.node_jobs, node_id, limit)
        except mac_nodes.MacError as err:
            return _from_error(err)
        return {"jobs": rows}

    return router
