"""The web board's door to standing approvals: `/api/standing-approvals*`.

The board holds an HttpOnly cookie, which `/v1` does not accept. These are the
same handlers as `/v1`, mounted under `/api`, which the board's middleware
(`app.authorise_api`) gates for every request -- so they carry no bearer check
of their own. Kept out of `standing_api.py` so the /v1 gate sweep
(tests/test_device_tokens_reach_every_v1_route.py) reads only /v1 handlers there.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from fastapi import APIRouter, Request

from .api import StrictJSON
from .standing_api import Paths, build_router, default_paths


def _gated_by_middleware(request: Request) -> None:
    return None


def build_board_router(surface, *, paths: Callable[[], Paths] = default_paths
                       ) -> APIRouter:
    """The same routes for the web board, under `/api`: it holds an HttpOnly
    cookie, which `/v1` does not accept."""
    router = build_router(surface, paths=paths, prefix="/api",
                          gate=_gated_by_middleware)

    @router.get("/approvals")
    async def board_approvals() -> StrictJSON:
        await asyncio.to_thread(surface._refresh_holding)
        return StrictJSON({"approvals": await asyncio.to_thread(surface.approvals),
                           "generated_at": time.time()})

    return router


