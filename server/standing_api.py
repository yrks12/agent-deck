"""`/v1/standing-approvals`: the owner's side of standing approvals.

Every route is behind `_authorise`, which is the owner's credential (the
master token or a device he paired) -- the same gate as answering a card.
Desks never reach these routes: they PROPOSE through the deck MCP tool
(`server/deck_mcp.py`), which can only ever write `status: proposed`.

Wire format: `docs/client-api.md`, "Standing approvals".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Query, Request

from . import asking, standing, standing_floor, standing_usage
from .api import Refused, StrictJSON, _authorise


@dataclass(frozen=True)
class Paths:
    policies: Path
    usage: Path
    audit: Path


def default_paths() -> Paths:
    return Paths(standing.DEFAULT_PATH, standing_usage.USAGE_PATH,
                 standing_usage.AUDIT_PATH)


def _refused(exc: standing.StandingError) -> Refused:
    return Refused(exc.status, exc.reason, exc.detail)


def row(policy: standing.Policy, used: dict, now: float) -> dict:
    """One policy as a client draws it: the policy plus today's usage."""
    mine = used.get(policy.id) or {}
    expired = policy.expires_at is not None and policy.expires_at <= now
    return {**policy.wire(), "live": policy.live(now), "expired": expired,
            "usage": {"day": standing_usage.day_of(now),
                      "count": int(mine.get("count", 0)),
                      "usd": float(mine.get("usd", 0))}}


def _kind_for(tool: str) -> str:
    kinds = standing_usage.kinds_of(tool, None)
    for kind in ("run_command", "send_email", "post_comment", "call_api"):
        if kind in kinds:
            return kind
    return "call_api"


def standing_option(ask: asking.Ask, desk: str) -> dict:
    """What the card's "always" path offers: make it a limited standing one."""
    floor = standing_floor.never_coverable(
        ask.tool, {"command": ask.subject} if ask.tool == "Bash" else {})
    if floor is None and asking.is_handoff_subject(ask.subject):
        floor = "credential_entry"
    ok = bool(desk) and ask.tool != asking.ELICITATION_TOOL and floor is None
    return {"available": ok, "kind": _kind_for(ask.tool), "desk": desk,
            "route": f"/v1/approvals/{ask.id}/standing",
            "summary": (f"Let {desk} do this up to a daily limit"
                        if ok else "not available for this action")}


def from_ask(surface, ask_id: str, body: dict, paths: Paths) -> dict:
    """Make the card's action a standing approval with a limit, and let this
    one through. One policy per command the card names, as "always" writes."""
    asking.expire(surface.asks_path)
    ask = asking.find(surface.asks_path, ask_id)
    if ask is None:
        raise Refused(404, "unknown_ask", f"no ask {ask_id!r}")
    desk = surface._desk_for_ask(ask)
    option = standing_option(ask, desk)
    if not option["available"]:
        raise Refused(409, "never_coverable" if desk else "no_desk",
                      option["summary"])
    kind = body.get("kind") or option["kind"]
    if ask.tool == "Bash":
        patterns = [r.pattern for r in asking.desk_rules(ask, desk)]
    else:
        patterns = ["*"]
    made = []
    for pattern in patterns:
        try:
            made.append(standing.create(
                {"desk": desk, "kind": kind, "tool": ask.tool,
                 "pattern": pattern, "limits": body.get("limits") or {},
                 "expires_at": body.get("expires_at"),
                 "note": body.get("note") or f"from ask {ask.id}"},
                by="owner", path=paths.policies))
        except standing.StandingError as exc:
            for policy in made:  # all or nothing
                standing.revoke(policy.id, path=paths.policies)
            raise _refused(exc) from exc
    answered = (surface.answer_ask(ask.id, "once")
                if ask.status == "pending" else None)
    now = time.time()
    used = standing_usage.usage(paths.usage, now)
    return {"ok": True, "policies": [row(p, used, now) for p in made],
            "answered": answered}


def _owner_gate(request: Request) -> None:
    _authorise(request)


def build_router(surface, *, paths: Callable[[], Paths] = default_paths,
                 prefix: str = "/v1", gate: Callable = _owner_gate
                 ) -> APIRouter:
    router = APIRouter(prefix=prefix, default_response_class=StrictJSON)
    _authorise = gate  # noqa: F841 -- shadows the /v1 gate for the handlers below

    def listing(status: str | None, desk: str | None) -> dict:
        p, now = paths(), time.time()
        used = standing_usage.usage(p.usage, now)
        rows = [row(x, used, now) for x in standing.load(p.policies)
                if (not status or x.status == status)
                and (not desk or x.desk in (desk, "*"))]
        return {"policies": rows, "generated_at": now}

    def call(fn, *args, **kwargs) -> dict:
        try:
            policy = fn(*args, path=paths().policies, **kwargs)
        except standing.StandingError as exc:
            raise _refused(exc) from exc
        now = time.time()
        return {"ok": True, "policy": row(
            policy, standing_usage.usage(paths().usage, now), now)}

    @router.get("/standing-approvals")
    async def list_policies(request: Request, status: str | None = Query(None),
                            desk: str | None = Query(None)) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(listing, status, desk))

    @router.post("/standing-approvals")
    async def create_policy(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        proposed = payload.get("status") == "proposed"
        by = str(payload.get("proposed_by") or "owner") if proposed else "owner"
        return StrictJSON(await asyncio.to_thread(
            call, standing.create, payload, by=by,
            status="proposed" if proposed else "active"))

    @router.get("/standing-approvals/audit")
    async def list_audit(request: Request, desk: str = Query(""),
                         policy_id: str = Query(""),
                         limit: int = Query(200, ge=1, le=1000)) -> StrictJSON:
        _authorise(request)
        rows = await asyncio.to_thread(
            standing_usage.audit, paths().audit, limit=limit, desk=desk,
            policy_id=policy_id)
        return StrictJSON({"audit": rows, "generated_at": time.time()})

    @router.patch("/standing-approvals/{policy_id}")
    async def edit_policy(policy_id: str, payload: dict,
                          request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(
            call, standing.edit, policy_id, payload))

    @router.post("/standing-approvals/{policy_id}/approve")
    async def approve_policy(policy_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(
            call, standing.approve, policy_id))

    @router.delete("/standing-approvals/{policy_id}")
    async def revoke_policy(policy_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(
            call, standing.revoke, policy_id))

    @router.post("/approvals/{ask_id}/standing")
    async def standing_from_ask(ask_id: str, payload: dict,
                                request: Request) -> StrictJSON:
        _authorise(request)
        await asyncio.to_thread(surface._refresh_holding)
        return StrictJSON(await asyncio.to_thread(
            from_ask, surface, ask_id, payload, paths()))

    return router
