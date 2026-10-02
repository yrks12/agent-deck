"""Stripe payments as events (Feature C): a verified webhook, and a poller.

`build_app` is a SEPARATE ASGI app that serves exactly one route,
`POST /hooks/stripe`. It is what a public listener may expose: nothing on it
reaches the board, /api or /v1. Every body is verified against the
`Stripe-Signature` header before it is parsed -- HMAC-SHA256 with the
endpoint's signing secret over "<t>.<raw body>", compared in constant time,
refused when `t` is more than TOLERANCE seconds from now.

`poll_events` is the fallback for a deck with no public HTTPS address: the
deck reads Stripe's own `/v1/events` with an API key every few minutes. Both
paths produce the same `events.Event` (ref = the Stripe event id), so a
payment seen by both is delivered once.

Secrets come from the root-only env file: DECK_STRIPE_WEBHOOK_SECRET for the
webhook, DECK_STRIPE_API_KEY (a restricted key with Events: read) for the poll.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import events
from .api import Refused

SECRET_ENV = "DECK_STRIPE_WEBHOOK_SECRET"
API_KEY_ENV = "DECK_STRIPE_API_KEY"
TOLERANCE = 300
#: Stripe's event payloads are a few KB; 64 KB leaves room for a large one.
BODY_MAX = 64 * 1024

#: The Stripe event types that are news to a desk, and the kind each becomes.
#: `charge.*` only: a Checkout payment also fires checkout.session.completed
#: and payment_intent.succeeded, and taking those too would tell the desk the
#: same payment three times under three refs.
KINDS = {"charge.succeeded": "payment", "charge.refunded": "refund",
         "charge.dispute.created": "dispute"}


class SignatureError(ValueError):
    """A webhook body that is not provably from Stripe. `str()` says why."""


def verify(payload: bytes, header: str, secret: str, *, now: float,
           tolerance: int = TOLERANCE) -> None:
    """Raise SignatureError unless `header` signs `payload` with `secret`."""
    stamp, sigs = None, []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            stamp = value
        elif key == "v1" and value:
            sigs.append(value)
    if not stamp or not stamp.isdigit() or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    expected = hmac.new(secret.encode("utf-8"), stamp.encode() + b"." + payload,
                        hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature does not match")
    if abs(now - int(stamp)) > tolerance:
        raise SignatureError("timestamp outside the tolerance")


def event_from_stripe(doc: dict, *, now: float) -> events.Event | None:
    """A Stripe event as a deck event, or None if it is not news. PURE."""
    kind = KINDS.get(str(doc.get("type") or ""))
    obj = ((doc.get("data") or {}).get("object") or {}) if isinstance(doc, dict) else {}
    if not kind or not isinstance(obj, dict) or not doc.get("id"):
        return None
    amount = obj.get("amount_refunded") if kind == "refund" else obj.get("amount")
    money = (f"{int(amount) / 100:.2f} {str(obj.get('currency') or '').upper()}"
             if isinstance(amount, int) else "an amount")
    who = ((obj.get("billing_details") or {}).get("email")
           or obj.get("receipt_email") or "an unknown customer")
    what = obj.get("description") or ""
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    summary = f"Stripe {kind}: {money} from {who}" + (f" -- {what}" if what else "")
    return events.Event(
        source="stripe", account=str(meta.get("site") or doc.get("account") or ""),
        kind=kind, summary=summary[:events.SUMMARY_MAX], ref=str(doc["id"]),
        ts=float(doc.get("created") or now))


def build_app(*, hub: Callable[[], events.Hub],
              secret: Callable[[], str] = lambda: os.environ.get(SECRET_ENV, ""),
              clock: Callable[[], float] | None = None) -> FastAPI:
    """The webhook-only app. No docs, no other route, nothing to discover."""
    import time

    from .events_api import THROTTLE, read_capped
    now = clock or time.time
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Refused)
    async def _refused(request: Request, exc: Refused) -> JSONResponse:
        return JSONResponse({"ok": False, "reason": exc.reason},
                            status_code=exc.status_code)

    @app.post("/hooks/stripe")
    async def stripe(request: Request) -> JSONResponse:
        key = secret().strip()
        if not key:
            raise Refused(503, "not_configured", f"{SECRET_ENV} is unset")
        peer = request.client.host if request.client else "?"
        if not THROTTLE.allow(f"stripe:{peer}"):
            raise Refused(429, "rate_limited", "slow down")
        body = await read_capped(request, BODY_MAX)
        try:
            verify(body, request.headers.get("stripe-signature", ""), key, now=now())
            doc = json.loads(body)
        except (SignatureError, ValueError) as err:
            print(f"[agent-deck] stripe webhook refused: {err}", flush=True)
            raise Refused(400, "bad_signature", "not a verified Stripe event")
        ev = event_from_stripe(doc, now=now()) if isinstance(doc, dict) else None
        if ev is None:
            return JSONResponse({"ok": True, "state": "ignored"})
        out = await asyncio.to_thread(hub().ingest, ev)
        return JSONResponse({"ok": True, "state": out["state"]})

    return app


def _fetch(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed host
        return json.load(resp)


def poll_events(api_key: str, *, since: int, now: float,
                fetch: Callable[[str, dict], dict] = _fetch
                ) -> tuple[list[events.Event], int]:
    """Stripe events created after `since` (epoch s) that are news, oldest
    first, and the newest `created` seen (the next `since`)."""
    query = [("limit", "100"), ("created[gt]", str(since))]
    query += [("types[]", t) for t in KINDS]
    doc = fetch("https://api.stripe.com/v1/events?" + urllib.parse.urlencode(query),
                {"Authorization": f"Bearer {api_key}"})
    rows = doc.get("data") if isinstance(doc, dict) else None
    out, newest = [], since
    for row in reversed(rows or []):
        if not isinstance(row, dict):
            continue
        created = row.get("created")
        if isinstance(created, int):
            newest = max(newest, created)
        ev = event_from_stripe(row, now=now)
        if ev is not None:
            out.append(ev)
    return out, newest
