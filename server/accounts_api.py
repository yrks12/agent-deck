"""The Claude-accounts routes (docs/plans/2026-10-01-two-accounts.md, S8).

    GET  /v1/accounts                 {"accounts": [{id, label, kind, plan}]}
    PUT  /v1/accounts/policy          {"mode": "fixed|failover|failover_api"}
         200 the effective policy; 403 failover_not_allowed; 400 bad_mode
    POST /v1/accounts/login                  {id, label}  -> {login_id, url, status, ...}
    POST /v1/accounts/login/{login_id}/code  {code}       -> {status: done|failed, ...}
    GET  /v1/accounts/login/{login_id}                    -> the sign-in's status
         (server/account_login.py: the CLI's own login; the code is never kept)
    POST /v1/agents/{name}/account    {"account": "work"}
         200 {ok, moved, desk, from, to, session_id}
         409 not_idle | engine_not_supported
         404 no_account | unknown_agent
         502 move_failed

Refusals keep their own slug (`{ok: false, reason, detail}`), so a client
branches on `reason` rather than on the status code. `plan` comes from the
usage meters' last reading: no request is made to list accounts.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .api import Refused, StrictJSON, _authorise


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def build_router(*, meters, mover, logins=None) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    async def _login_call(fn, *args) -> StrictJSON:
        from .account_login import LoginError
        try:
            return StrictJSON(await asyncio.to_thread(fn, *args))
        except LoginError as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from exc

    @router.post("/accounts/login")
    async def start_login(request: Request) -> StrictJSON:
        """Sign a Claude account in from the app: the CLI's own login."""
        _authorise(request)
        body = await _body(request)
        ident = body.get("id")
        if not isinstance(ident, str) or not ident:
            raise Refused(400, "bad_request", 'send {"id": "<account>", "label": "..."}')
        return await _login_call(logins.start, ident, str(body.get("label") or ""))

    @router.post("/accounts/login/{login_id}/code")
    async def login_code(login_id: str, request: Request) -> StrictJSON:
        """The pasted code goes straight into the CLI; it is never echoed,
        logged or kept."""
        _authorise(request)
        code = (await _body(request)).get("code")
        if not isinstance(code, str) or not code:
            raise Refused(400, "bad_request", 'send {"code": "<what Claude showed>"}')
        return await _login_call(logins.submit, login_id, code)

    @router.get("/accounts/login/{login_id}")
    async def login_status(login_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return await _login_call(logins.get, login_id)

    @router.get("/accounts")
    async def list_accounts(request: Request) -> StrictJSON:
        _authorise(request)
        every = await asyncio.to_thread(meters.poll_all)
        return StrictJSON({"accounts": [
            {"id": a.id, "label": a.label, "kind": a.kind, "plan": p.get("subscription")}
            for a, p in every]})

    @router.put("/accounts/policy")
    async def set_policy(request: Request) -> StrictJSON:
        """The apps' auto-switch toggle. Overrides deck.toml's mode in a
        deck-owned file, never past `failover_allowed` (owner ruling)."""
        _authorise(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        mode = body.get("mode") if isinstance(body, dict) else None
        from . import deckconfig, policy
        try:
            return StrictJSON(policy.set_mode(deckconfig.load(), str(mode or "")))
        except policy.PolicyError as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from exc

    @router.post("/agents/{name}/account")
    async def move_desk(name: str, request: Request) -> StrictJSON:
        _authorise(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        to = body.get("account") if isinstance(body, dict) else None
        if not isinstance(to, str) or not to:
            raise Refused(400, "bad_request", 'send {"account": "<id>"}')
        from .mover import MoveError
        try:
            moved = await asyncio.to_thread(mover.move, name, to)
        except MoveError as exc:
            raise Refused(exc.status, exc.reason, exc.detail) from exc
        return StrictJSON(moved)

    return router
