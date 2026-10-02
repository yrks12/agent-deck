"""The web board's Events view data (Feature C): `/api/events*`.

Same store as `/v1/events`, under `/api`, which the board's middleware
(`app.authorise_api`) gates for every route -- so these handlers carry no
bearer check of their own. Kept out of `events_api.py` so the /v1 gate sweep
(tests/test_device_tokens_reach_every_v1_route.py) reads only /v1 handlers there.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import APIRouter, Request

from . import events
from .api import Refused, StrictJSON
from .events_api import read_capped


def build_board_router(*, hub: Callable[[], events.Hub]) -> APIRouter:
    """The web board's view of the same data, under `/api` -- which the
    board's middleware (`app.authorise_api`) gates for every route."""
    router = APIRouter(prefix="/api")

    @router.get("/events")
    async def board_events() -> StrictJSON:
        store = hub().store
        rows = await asyncio.to_thread(store.recent, 200)
        return StrictJSON({"events": rows,
                           "routes": [r.to_dict() for r in store.routes()]})

    @router.put("/events/routes")
    async def board_routes(request: Request) -> StrictJSON:
        raw = await read_capped(request, 64 * 1024)
        try:
            doc = json.loads(raw or b"null")
            routes = hub().store.save_routes(
                doc.get("routes") if isinstance(doc, dict) else None)
        except (ValueError, events.EventError) as err:
            raise Refused(400, "bad_routes", str(err))
        return StrictJSON({"ok": True, "routes": [r.to_dict() for r in routes]})

    return router
