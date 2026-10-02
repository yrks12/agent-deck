"""GET /v1/money: the Money Board for the Mac, iPhone and web apps.

The browser board reads `/api/money`: same body, behind the sign-in cookie
the `/api/` middleware in server/app.py checks (it cannot send a bearer).

Serves the board `server/money/service.py` caches; `?refresh=1` rebuilds it
first. The refresh loop starts with the deck (not under pytest: it reads the
owner's real Stripe and Google accounts).
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request

from .api import StrictJSON, _authorise
from .money import service


async def _loop() -> None:
    await asyncio.sleep(30)                     # let the deck finish starting
    while True:
        try:
            await asyncio.to_thread(service.refresh)
        except Exception as exc:  # noqa: BLE001 - one bad round never kills it
            print(f"[agent-deck] money refresh failed: {exc!r}")
        await asyncio.sleep(service.INTERVAL)


def build_router() -> APIRouter:
    router = APIRouter(default_response_class=StrictJSON)

    @router.on_event("startup")
    async def _start() -> None:
        # Decided at startup, not import: pytest imports the app before any
        # test runs, and a test must never read the owner's real accounts.
        if "PYTEST_CURRENT_TEST" not in os.environ:
            asyncio.create_task(_loop())

    async def body(refresh: int) -> StrictJSON:
        board = None
        if refresh:
            board = await asyncio.to_thread(service.refresh)
        board = board or service.cached()
        if board is None:
            return StrictJSON({"state": "warming", "companies": [], "experiments": [],
                               "connect": []})
        return StrictJSON({"state": "ready", **board})

    @router.get("/v1/money")
    async def get_money(request: Request, refresh: int = 0) -> StrictJSON:
        _authorise(request)
        return await body(refresh)

    @router.get("/api/money")
    async def web_money(refresh: int = 0) -> StrictJSON:
        return await body(refresh)        # the /api/ middleware checks the cookie

    return router
