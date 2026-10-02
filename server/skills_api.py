"""`/v1/skills/*` (C5, slice K1): explore skills and install them on this server.

Built here, **not mounted** -- slice W1 calls
`app.include_router(skills_api.build_router(skills.Skills()))` in `server/app.py`.

Every route needs the `/v1` bearer (`api._authorise`), checked before any CLI
call. Installing needs the same bearer as hiring a desk; no new capability is
exposed. Refusals are the deck's `{"ok": false, "reason", "detail"}`; a
`needs_confirmation` refusal also carries `command` and `sha256`, and the app
re-POSTs with `accept_command: <sha256>` once a person has read the command.
The service calls block on the CLI, so they run off the event loop.

Routes (the exact set is pinned by tests/test_skills_api.py):

    GET    /v1/skills/catalog?q=&category=&installed=&limit=50&offset=0
    GET    /v1/skills/ops/{op_id}
    POST   /v1/skills/install          {"id","scope":"all"|"desk","desk"?,"accept_command"?}
    GET    /v1/skills/{skill_id}
    DELETE /v1/skills/{skill_id}?scope=all|desk&desk=
    PATCH  /v1/skills/{skill_id}       {"enabled":bool,"scope"?,"desk"?}
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .api import Refused, StrictJSON, _authorise
from .skills import Skills, SkillsError

__all__ = ["build_router", "ROUTES"]

MAX_LIMIT = 200

ROUTES = (
    ("GET", "/v1/skills/catalog"),
    ("GET", "/v1/skills/ops/{op_id}"),
    ("POST", "/v1/skills/install"),
    ("GET", "/v1/skills/{skill_id}"),
    ("DELETE", "/v1/skills/{skill_id}"),
    ("PATCH", "/v1/skills/{skill_id}"),
)


def _refuse(reason: str, detail: str, status: int = 400) -> StrictJSON:
    return StrictJSON({"ok": False, "reason": reason, "detail": detail}, status_code=status)


def _error(exc: SkillsError) -> StrictJSON:
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
        raise SkillsError("bad_input", "The body must be a JSON object.")
    if not isinstance(payload, dict):
        raise SkillsError("bad_input", "The body must be a JSON object.")
    return payload


def _opt_str(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise SkillsError("bad_input", f"{key} must be a string.")
    return value


def _int(raw: str | None, default: int, low: int, high: int, name: str) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SkillsError("bad_input", f"{name} must be a whole number.")
    if not low <= value <= high:
        raise SkillsError("bad_input", f"{name} must be between {low} and {high}.")
    return value


def build_router(service: Skills) -> APIRouter:
    router = APIRouter(prefix="/v1/skills", default_response_class=StrictJSON)

    @router.get("/catalog")
    async def catalog(request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        qp = request.query_params
        try:
            installed_raw = (qp.get("installed") or "").lower()
            if installed_raw not in ("", "true", "false"):
                raise SkillsError("bad_input", "installed is true or false.")
            installed = None if not installed_raw else installed_raw == "true"
            limit = _int(qp.get("limit"), 50, 1, MAX_LIMIT, "limit")
            offset = _int(qp.get("offset"), 0, 0, 1_000_000, "offset")
            out = await asyncio.to_thread(
                service.catalog, q=qp.get("q") or "", category=qp.get("category") or None,
                installed=installed, limit=limit, offset=offset)
        except SkillsError as exc:
            return _error(exc)
        return out

    @router.get("/ops/{op_id}")
    async def op(op_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            return service.op(op_id)
        except SkillsError as exc:
            return _error(exc)

    @router.post("/install")
    async def install(request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            skill_id = body.get("id")
            if not isinstance(skill_id, str) or not skill_id:
                raise SkillsError("bad_input", "id is the skill's name@marketplace.")
            out = await asyncio.to_thread(
                service.install, skill_id, _opt_str(body, "scope") or "all",
                desk=_opt_str(body, "desk"), accept_command=_opt_str(body, "accept_command"))
        except SkillsError as exc:
            return _error(exc)
        return StrictJSON(out, status_code=202)

    @router.get("/{skill_id}")
    async def detail(skill_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            return await asyncio.to_thread(service.detail, skill_id)
        except SkillsError as exc:
            return _error(exc)

    @router.delete("/{skill_id}")
    async def uninstall(skill_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        qp = request.query_params
        try:
            return await asyncio.to_thread(service.uninstall, skill_id,
                                           qp.get("scope") or "all", qp.get("desk") or None)
        except SkillsError as exc:
            return _error(exc)

    @router.patch("/{skill_id}")
    async def toggle(skill_id: str, request: Request):
        refused = _auth(request)
        if refused is not None:
            return refused
        try:
            body = await _body(request)
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise SkillsError("bad_input", "enabled is true or false.")
            return await asyncio.to_thread(service.set_enabled, skill_id, enabled,
                                           _opt_str(body, "scope") or "all",
                                           _opt_str(body, "desk"))
        except SkillsError as exc:
            return _error(exc)

    return router
