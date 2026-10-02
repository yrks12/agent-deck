"""`/v1/store/*`: the Connectors & Skills store over HTTP (docs/connectors.md).

Every route needs the `/v1` bearer, checked before anything else. Refusals are
the deck's `{"ok": false, "reason", "detail"}`. A refusal's detail is built
from names only, so a key sent in an install body can never be echoed back.
Store calls touch the network and the disk, so they run off the event loop.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .api import Refused, StrictJSON, _authorise
from .connectors import Store, StoreError

__all__ = ["build_router"]

MAX_LIMIT = 200


def _error(exc: StoreError) -> StrictJSON:
    return StrictJSON(exc.body(), status_code=exc.status)


def _auth(request: Request) -> StrictJSON | None:
    try:
        _authorise(request)
    except Refused as exc:
        return StrictJSON(exc.detail, status_code=exc.status_code,
                          headers=getattr(exc, "headers", None))
    return None


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise StoreError("bad_input", "The body must be a JSON object.")
    if not isinstance(payload, dict):
        raise StoreError("bad_input", "The body must be a JSON object.")
    return payload


def _int(raw, default: int, low: int, high: int, name: str) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise StoreError("bad_input", f"{name} must be a whole number.")
    if not low <= value <= high:
        raise StoreError("bad_input", f"{name} must be between {low} and {high}.")
    return value


def _id(body: dict) -> str:
    value = body.get("id")
    if not isinstance(value, str) or not value:
        raise StoreError("bad_input", "id is the store item's id.")
    return value


def build_router(store: Store) -> APIRouter:
    router = APIRouter(prefix="/v1/store", default_response_class=StrictJSON)

    @router.get("/catalog")
    async def catalog(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        qp = request.query_params
        try:
            return await asyncio.to_thread(
                store.catalog, kind=qp.get("kind") or None, q=qp.get("q") or "",
                trust=qp.get("trust") or "trusted",
                limit=_int(qp.get("limit"), 50, 1, MAX_LIMIT, "limit"),
                offset=_int(qp.get("offset"), 0, 0, 1_000_000, "offset"))
        except StoreError as exc:
            return _error(exc)

    @router.get("/item")
    async def item(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            return await asyncio.to_thread(store.item, request.query_params.get("id") or "")
        except StoreError as exc:
            return _error(exc)

    @router.post("/install")
    async def install(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            body = await _body(request)
            accept = body.get("accept_unverified", False)
            if not isinstance(accept, bool):
                raise StoreError("bad_input", "accept_unverified is true or false.")
            return await asyncio.to_thread(
                store.install, _id(body), body.get("desks"), body.get("secrets"), accept)
        except StoreError as exc:
            return _error(exc)

    @router.post("/update")
    async def update(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            body = await _body(request)
            return await asyncio.to_thread(store.update, _id(body), body.get("desks"))
        except StoreError as exc:
            return _error(exc)

    @router.post("/uninstall")
    async def uninstall(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            body = await _body(request)
            return await asyncio.to_thread(store.uninstall, _id(body), body.get("desks"))
        except StoreError as exc:
            return _error(exc)

    @router.get("/installed")
    async def installed(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            return await asyncio.to_thread(store.installed,
                                           request.query_params.get("desk") or None)
        except StoreError as exc:
            return _error(exc)

    @router.post("/connect")
    async def connect(request: Request):
        """Start an OAuth sign-in; the app opens `authorize_url` in his browser."""
        if (refused := _auth(request)) is not None:
            return refused
        try:
            body = await _body(request)
            return await asyncio.to_thread(store.connect, _id(body), body.get("desks"))
        except StoreError as exc:
            return _error(exc)

    @router.post("/connect/complete")
    async def complete(request: Request):
        """The code his browser came back with: `{state, code}` or the whole
        `callback_url`. Behind the bearer like every /v1 route -- the app that
        caught the loopback redirect is the caller, never the browser."""
        if (refused := _auth(request)) is not None:
            return refused
        try:
            body = await _body(request)
            for key in ("state", "code", "callback_url"):
                if body.get(key) is not None and not isinstance(body.get(key), str):
                    raise StoreError("bad_input", f"{key} is text.")
            return await asyncio.to_thread(store.complete, body.get("state"),
                                           body.get("code"), body.get("callback_url"))
        except StoreError as exc:
            return _error(exc)

    @router.get("/connect/status")
    async def connect_status(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        try:
            return store.connect_status(request.query_params.get("state") or "")
        except StoreError as exc:
            return _error(exc)

    @router.post("/refresh")
    async def refresh(request: Request):
        if (refused := _auth(request)) is not None:
            return refused
        store.refresh_async()
        return {"ok": True, "refreshing": True}

    return router
