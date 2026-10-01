"""GET /v1/usage (contract C4): the Claude subscription meter for the Mac app.

Built from the collector's existing `UsageReader` and `OpencodeGoUsageReader`;
this module owns no poller and no network. `reader.poll()` is itself a 300 s
cache, so a request is a dict read (the first one may do the one fetch).
Mounted by `server/app.py` (tests/test_usage_is_mounted.py pins it).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .api import StrictJSON, _authorise


def _body(anthropic: dict, opencode: dict) -> dict:
    code = anthropic.get("reason_code")
    return {
        "available": bool(anthropic.get("available")),
        "stale": bool(anthropic.get("stale")),
        "reason": code,
        "plan": anthropic.get("subscription"),
        "windows": list(anthropic.get("limits") or []),
        "breakdown": anthropic.get("breakdown"),
        "extra_usage": anthropic.get("extra_usage")
        or {"enabled": False, "spend_limit_reached": False},
        "other": list(opencode.get("windows") or []),
        "fetched_at": anthropic.get("fetched_at"),
        "next_refresh_at": anthropic.get("next_refresh_at"),
    }


def build_router(*, reader, opencode) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    @router.get("/usage")
    async def get_usage(request: Request) -> StrictJSON:
        _authorise(request)
        anthropic = await asyncio.to_thread(reader.poll)
        other = await asyncio.to_thread(opencode.poll)
        return StrictJSON(_body(anthropic, other))

    return router
