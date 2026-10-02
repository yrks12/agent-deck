"""`/v1/events` (Feature C): the inbound door and the routing table.

    POST /v1/events          an event (or {"events": [...]}) from the owner's own
                             sites and deploy scripts. Auth: the per-deck EVENTS
                             token, or normal deck auth. Capped, rate-limited.
    GET  /v1/events          recent events, the desk each went to, its state
    GET  /v1/events/routes   the routing table
    PUT  /v1/events/routes   replace it ({"routes": [...]})

The events token is a SEPARATE secret from the owner's: a website that holds
it can post events and read nothing -- every GET here, and every other /v1
route, still wants the deck's own auth. `DECK_EVENTS_TOKEN` in the root-only
env file wins; otherwise one is generated once under BUS_DIR/events, mode 0600.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request

from . import atomic, events
from .api import Refused, StrictJSON, _authorise
from .paths import BUS_DIR

TOKEN_ENV = "DECK_EVENTS_TOKEN"
TOKEN_PATH = BUS_DIR / "events" / "events-token.txt"
#: One event is a few hundred bytes; a batch of BATCH_MAX fits easily.
BODY_MAX = 16 * 1024
BATCH_MAX = 20


def events_token(path: Path | None = None, *, create: bool = False) -> str:
    """The events token: env first, else the file, else (with `create`, which
    only deck startup passes) made once, 0600. A request never writes it."""
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env
    path = Path(path or TOKEN_PATH)
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    if not create:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    made = secrets.token_urlsafe(32)
    atomic.write_text(path, made + "\n", mode=0o600)
    return made


class Throttle:
    """At most `limit` calls per `window` seconds per key. In memory."""

    def __init__(self, limit: int = 60, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            if len(self._hits) > 10_000:
                self._hits.clear()
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


THROTTLE = Throttle()


def _bearer(request: Request) -> str:
    scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
    return presented.strip() if scheme.lower() == "bearer" else ""


def _is_events_token(request: Request) -> bool:
    presented = _bearer(request)
    expected = events_token()
    return bool(presented and expected and hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")))


async def read_capped(request: Request, cap: int) -> bytes:
    """The body, refused with 413 past `cap` -- before reading it all."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise Refused(413, "too_large", f"body over {cap} bytes")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise Refused(413, "too_large", f"body over {cap} bytes")
    return body


def build_router(*, hub: Callable[[], events.Hub],
                 record: Callable[[dict], None] = lambda r: None) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    @router.post("/events")
    async def post_events(request: Request) -> StrictJSON:
        # The events token, else the deck's own gate (which counts a wrong
        # token towards the ban like every other /v1 route).
        who = "events" if _is_events_token(request) else "deck"
        if who == "deck":
            _authorise(request)
        peer = request.client.host if request.client else "?"
        if not THROTTLE.allow(f"{who}:{peer}"):
            raise Refused(429, "rate_limited", "too many events; slow down")
        raw = await read_capped(request, BODY_MAX)
        try:
            doc = json.loads(raw or b"null")
        except ValueError:
            raise Refused(400, "bad_event", "body is not JSON")
        items = doc.get("events") if isinstance(doc, dict) and "events" in doc else [doc]
        if not isinstance(items, list) or not items or len(items) > BATCH_MAX:
            raise Refused(400, "bad_event", f"send 1 to {BATCH_MAX} events")
        now = time.time()
        try:
            parsed = [events.Event.from_dict(item, now=now) for item in items]
        except events.EventError as err:
            raise Refused(400, "bad_event", str(err))
        results = []
        for ev in parsed:
            out = await asyncio.to_thread(hub().ingest, ev)
            record({"ts": now, "event": "event_wake", "source": ev.source,
                    "ref": ev.ref, "desk": out.get("desk"), "state": out["state"]})
            results.append(out)
        return StrictJSON({"ok": True, "results": results})

    @router.get("/events")
    async def get_events(request: Request) -> StrictJSON:
        _authorise(request)
        store = hub().store
        rows = await asyncio.to_thread(store.recent, 200)
        return StrictJSON({"events": rows,
                           "routes": [r.to_dict() for r in store.routes()]})

    @router.get("/events/routes")
    async def get_routes(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"routes": [r.to_dict() for r in hub().store.routes()]})

    @router.put("/events/routes")
    async def put_routes(request: Request) -> StrictJSON:
        _authorise(request)
        raw = await read_capped(request, 64 * 1024)
        try:
            doc = json.loads(raw or b"null")
            routes = hub().store.save_routes(
                doc.get("routes") if isinstance(doc, dict) else None)
        except (ValueError, events.EventError) as err:
            raise Refused(400, "bad_routes", str(err))
        return StrictJSON({"ok": True, "routes": [r.to_dict() for r in routes]})

    return router

