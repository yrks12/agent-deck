"""The Mac bridge's wire: the routes a Mac dials out to, and the owner's view (MB1).

The Mac never listens. It registers (`POST /v1/nodes`), then long-polls
`/v1/nodes/{id}/poll` forever: the poll is both its heartbeat and how it
claims jobs. It reports each job's progress to `.../jobs/{job_id}/events` and
tells the deck what the owner tapped on a grant card via `.../grants`.

**There is no route here that creates a job.** Only a desk's `mac` MCP process
on the box enqueues (`mac_nodes.Store.enqueue`). A leaked bearer token alone
can therefore list and rename Macs, but cannot run anything on one.
`tests/test_mac_api.py` pins the exact (method, path) set.

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

from fastapi import APIRouter, Request

from . import handoff, mac_nodes, owner
from .api import Refused, StrictJSON, _authorise

MAX_WAIT = 25.0
WAKE_CHECK = 0.25   # s between cheap "did a job file change?" checks in a long-poll
NODE_HEADER = "x-deck-node"

GRANT_DECISIONS = ("hour", "always", "deny", "revoke")

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
)


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
                 handoffs_path: Path | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

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
            deadline = time.monotonic() + wait
            jobs, cancel = await asyncio.to_thread(store.poll, *args)
            await asyncio.to_thread(_back_online, node_id)
            stamp = store.stamp()
            while not jobs and not cancel:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(min(WAKE_CHECK, left))
                now_stamp = store.stamp()
                if now_stamp != stamp:
                    jobs, cancel = await asyncio.to_thread(store.poll, *args)
                    stamp = store.stamp()
        except mac_nodes.MacError as err:
            return _from_error(err)
        return {"jobs": jobs, "cancel": cancel, "server_ts": store.now()}

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

    # ── the owner's routes (bearer only) ─────────────────────────────────────

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
