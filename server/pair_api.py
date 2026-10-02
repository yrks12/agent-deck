"""K3: `/healthz`, `POST /v1/pair`, `GET /v1/version`, and the bearer gate.

Built here, **not mounted** -- slice B2 calls `mount(app, deckconfig.load())`
in `server/app.py` and points `api._authorise` at `BearerGate.check`.

The gate accepts **either** the master `AGENT_DECK_TOKEN` (so the owner's Mac
and phone keep working unchanged, with byte-identical 401/503 answers) **or** a
device token whose SHA-256 is in `<state_dir>/devices.json`. Before either, a
banned IP is answered 429 `rate_limited`.

Choices worth knowing (each pinned by `tests/test_pair_api.py`):

* A request with **no** bearer is logged (`reason=missing_token`) but does not
  count towards a ban. It is not a guess, and counting it would lock out an app
  that simply has not been given its token yet. A wrong token counts.
* `/v1/pair` counts every attempt against the 5-per-10-minutes budget *before*
  looking at the code, and a refused attempt never burns the code.
* A 200 from `/v1/pair` is the one response that carries a token, so it is
  `Cache-Control: no-store`. No log line anywhere carries a token or a code.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request

from server import deckconfig, ratelimit
from server.api import TOKEN_ENV, Refused, StrictJSON, _refused_handler
from server.pairing import DeviceStore, PairingError, PairingStore, clean_device_name

__all__ = ["BearerGate", "build_router", "mount", "read_version"]

REPO = Path(__file__).resolve().parent.parent
API_LEVEL = 1
_SECRET = re.compile(r"^[A-Za-z0-9_-]{22}$")
_PAIR_STATUS = {"pair_unknown": 401, "pair_used": 409, "pair_expired": 410}


def read_version(root: Path = REPO) -> str:
    """The repo-root `VERSION` file (slice R0 adds it); `0.0.0` until then."""
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _rate_limited(retry: int) -> Refused:
    return Refused(429, "rate_limited",
                   f"Too many attempts from this address; try again in {retry} seconds.",
                   headers={"Retry-After": str(retry)})


class BearerGate:
    """Who may use `/v1`. `check` returns `"master"` or the device id."""

    def __init__(self, *, devices: DeviceStore, limiter: ratelimit.RateLimiter,
                 mode: str, master: Callable[[], str] | None = None) -> None:
        self.devices = devices
        self.limiter = limiter
        self.mode = mode
        self._master = master or (lambda: (os.environ.get(TOKEN_ENV) or "").strip())

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else None
        return ratelimit.client_ip(peer, request.headers.get("x-forwarded-for"), self.mode)

    def refuse_if_banned(self, ip: str) -> None:
        retry = self.limiter.banned(ip)
        if retry:
            raise _rate_limited(retry)

    def check(self, request: Request) -> str:
        ip = self.client_ip(request)
        self.refuse_if_banned(ip)
        expected = self._master()
        if not expected:
            raise Refused(503, "auth_not_configured",
                          f"{TOKEN_ENV} is unset; /v1 is closed until it is set")
        scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() == "bearer" and presented:
            # bytes, not str: compare_digest raises on non-ASCII str, which
            # would turn a garbage header into a 500 instead of a 401.
            if hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
                return "master"
            device = self.devices.verify(presented)
            if device is not None:
                return device.id
            ratelimit.log_failure(ip, request.url.path, "bad_token")
            self.limiter.auth_failed(ip)
        else:
            ratelimit.log_failure(ip, request.url.path, "missing_token")
        raise Refused(401, "unauthorized", "bearer token missing or wrong",
                      headers={"WWW-Authenticate": "Bearer"})


def build_router(config: deckconfig.DeckConfig, *, pairing: PairingStore | None = None,
                 devices: DeviceStore | None = None,
                 limiter: ratelimit.RateLimiter | None = None,
                 gate: BearerGate | None = None, version: str | None = None,
                 min_app: str = "0.0.0") -> APIRouter:
    state_dir = config.deck.state_dir
    pairing = pairing or PairingStore(state_dir)
    devices = devices or DeviceStore(state_dir)
    limiter = limiter or ratelimit.RateLimiter()
    gate = gate or BearerGate(devices=devices, limiter=limiter, mode=config.network.mode)
    version = version or read_version()
    router = APIRouter(default_response_class=StrictJSON)

    @router.get("/healthz")
    async def healthz() -> StrictJSON:
        return StrictJSON({"ok": True})

    @router.get("/v1/version")
    async def get_version(request: Request) -> StrictJSON:
        await asyncio.to_thread(gate.check, request)
        return StrictJSON({"version": version, "min_app": min_app, "api": API_LEVEL})

    @router.post("/v1/pair")
    async def pair(request: Request) -> StrictJSON:
        ip = gate.client_ip(request)
        route = request.url.path
        gate.refuse_if_banned(ip)
        retry = limiter.pair_attempt(ip)
        if retry:
            ratelimit.log_failure(ip, route, "rate_limited")
            raise _rate_limited(retry)
        try:
            body = await request.json()
            code, device = body["code"], clean_device_name(body["device"])
            if not isinstance(code, str) or not _SECRET.match(code):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            ratelimit.log_failure(ip, route, "pair_malformed")
            raise Refused(400, "pair_malformed",
                          "Send {\"code\": <the 22-character code>, \"device\": "
                          "<a name up to 60 characters>}.") from None
        try:
            await asyncio.to_thread(pairing.redeem, code)
        except PairingError as exc:
            ratelimit.log_failure(ip, route, exc.reason)
            raise Refused(_PAIR_STATUS[exc.reason], exc.reason, exc.detail) from None
        try:
            token, dev = await asyncio.to_thread(devices.add, device)
        except (OSError, ValueError):
            raise Refused(500, "pair_failed", "The server could not save this device; "
                          "run deckctl doctor, then deckctl pair again.") from None
        return StrictJSON({"token": token, "device_id": dev.id,
                           "deck": {"name": config.deck.name, "version": version}},
                          headers={"Cache-Control": "no-store"})

    return router


def mount(app, config: deckconfig.DeckConfig, **kwargs) -> None:
    """What B2 adds to `server/app.py`. Idempotent about the refusal handler."""
    app.add_exception_handler(Refused, _refused_handler)
    app.include_router(build_router(config, **kwargs))
