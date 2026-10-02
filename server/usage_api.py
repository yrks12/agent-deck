"""GET /v1/usage (contract C4): the Claude subscription meter for the Mac app.

Built from the collector's existing `UsageReader` and `OpencodeGoUsageReader`;
this module owns no poller and no network. `reader.poll()` is itself a 300 s
cache, so a request is a dict read (the first one may do the one fetch).
Mounted by `server/app.py` (tests/test_usage_is_mounted.py pins it).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from . import accounts as accounts_mod, deckconfig, roster
from .api import StrictJSON, _authorise
from .sources.usage import AccountMeters


def _account_row(acct, payload: dict, desks: list[str]) -> dict:
    """One account's meter (two-accounts plan): the C4 fields, per account."""
    return {
        "id": acct.id, "label": acct.label, "kind": acct.kind,
        "plan": payload.get("subscription"),
        "available": bool(payload.get("available")),
        "stale": bool(payload.get("stale")),
        "reason": payload.get("reason_code"),
        "windows": list(payload.get("limits") or []),
        "extra_usage": payload.get("extra_usage")
        or {"enabled": False, "spend_limit_reached": False},
        "fetched_at": payload.get("fetched_at"),
        "refresh_expires_at": payload.get("refresh_expires_at"),
        "desks": desks,
    }


def _policy() -> dict | None:
    """The EFFECTIVE policy: deck.toml, the app's toggle on top, capped by
    `failover_allowed`."""
    from . import policy
    try:
        return policy.effective(deckconfig.load())
    except Exception:  # noqa: BLE001 - a bad deck.toml is the doctor's to report
        return None


def _desks_by_account() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for desk in roster.load_roster(roster.DEFAULT_PATH):
        out.setdefault(accounts_mod.for_desk(desk).id, []).append(desk.name)
    return out


def _body(anthropic: dict, opencode: dict, meters: list | None = None) -> dict:
    code = anthropic.get("reason_code")
    by_account = _desks_by_account() if meters else {}
    return {
        # The numbers come from an undocumented Anthropic endpoint; the apps
        # say so (terms check, claude-dashbaord-oss-terms.md).
        "unofficial": True,
        "accounts": [_account_row(a, p, by_account.get(a.id, []))
                     for a, p in (meters or [])],
        "policy": _policy(),
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


def build_router(*, reader, opencode, meters: AccountMeters | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)
    meters = meters or AccountMeters(reader)

    @router.get("/usage")
    async def get_usage(request: Request) -> StrictJSON:
        _authorise(request)
        every = await asyncio.to_thread(meters.poll_all)
        anthropic = every[0][1]
        other = await asyncio.to_thread(opencode.poll)
        return StrictJSON(_body(anthropic, other, every))

    return router
