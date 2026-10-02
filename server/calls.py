"""Live voice calls (K6): start, end, and what the desk is told about them.

The call itself is the owner's Mac talking to a desk over the same thread it
already types into: each utterance is an ordinary `POST /v1/threads/.../messages`
with `channel: "voice"` and this call's `call_id` (K2). This module owns only
the two edges of that: a call opening and a call closing. See `docs/calls.md`.

The deck's two lines go through `office.send` as sender `deck` -- a system
voice -- never as the owner, so a desk cannot read the framing as him.
State is `calls.json` on the bus, so a deck restart mid-call loses nothing.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request

from . import atomic, callmemory, office, owner, realtime, ringing, roster
from .api import Refused, StrictJSON, _authorise

DECK = "deck"

START_LINE = (
    f"[Agent Deck] {owner.title()} started a live voice call. Work and answer "
    "exactly as you would in chat -- same tools, same checks, same care. Open "
    "every reply with the answer in one or two plain spoken sentences (no "
    "markdown, paths or code in those); detail for the chat can follow. Your "
    "voice is relayed live by a voice model that cannot do work itself and "
    "speaks only what you write; use `say` for progress and findings so the "
    "caller hears them, and for anything that will take more than a few "
    "seconds."
)
#: The desk called him (`server/ringing.py`) and he picked up. The apps speak
#: the reason as the call's first line (`opening`), so the desk must not.
ANSWERED_LINE = (
    f"[Agent Deck] {owner.title()} picked up the call you placed. Your reason "
    "was just spoken to him as your opening line: {reason} -- do not repeat "
    "it; go straight to what he needs to hear or decide. "
)
CALL_ME_NOW_LINE = (
    f"[Agent Deck] {owner.title()} turned on \"Call me now\": call him with "
    "mcp__deck__call_owner as soon as you have something worth his time. It "
    "lasts an hour or one call."
)
END_LINE = (
    "[Agent Deck] The call ended. This channel is closed from now on, so "
    "anything still owed goes in the chat."
)

DEFAULT_VOICE = {"id": "", "rate": 1.0}

_LOCK = threading.Lock()


def _file() -> Path:
    return office.BUS_DIR / "calls.json"


def _load() -> dict:
    try:
        data = json.loads(_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(calls: dict) -> None:
    atomic.write_text(_file(), json.dumps(calls))


def _voice_of(desk: roster.Desk) -> dict:
    v = desk.voice or {}
    rate = v.get("rate")
    return {"id": str(v.get("id") or ""),
            "rate": float(rate) if isinstance(rate, (int, float))
            and not isinstance(rate, bool) else 1.0}


def count_turns(call_id: str) -> int:
    """Voice-channel utterances that carried this call's id."""
    try:
        lines = office.MESSAGES_FILE.read_text().splitlines()
    except OSError:
        return 0
    turns = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(rec, dict) and rec.get("channel") == "voice"
                and rec.get("call_id") == call_id):
            turns += 1
    return turns


def build_router(*, roster_path: Callable[[], Path],
                 wake: Callable[[str], object],
                 inject: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> APIRouter:
    """`wake(name)` is the caller's `ensure_awake(name, reason="call")`;
    `inject(name, text)` is the live-socket fast path (True = delivered)."""
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    def tell(agent: str, text: str) -> None:
        """One deck line: queued, delivered now if a socket is live, else the
        desk is woken (the wake seed carries the queued mail)."""
        sent = office.send(agent, text, sender=DECK)
        if not sent.get("ok"):
            raise Refused(500, "queue_failed", str(sent.get("detail") or ""))
        if inject is not None:
            try:
                if inject(agent, office.attribute(text, DECK)):
                    office.ack(sent["id"])
                    return
            except Exception:
                pass

        def run() -> None:
            try:
                wake(agent)
            except Exception as exc:  # a wake must never take the deck down
                print(f"[agent-deck] call wake {agent!r} failed: {exc!r}")

        threading.Thread(target=run, name=f"call-wake-{agent}",
                         daemon=True).start()

    def begin(agent: str, ring: dict | None = None) -> dict:
        team = roster.load_roster(roster_path())
        desk = next((d for d in team if d.name == agent), None)
        if desk is None:
            raise Refused(404, "unknown_agent", f"no agent named {agent!r}")
        call_id = "call_" + uuid.uuid4().hex[:12]
        with _LOCK:
            calls = _load()
            memory = callmemory.block(agent, calls)
            calls[call_id] = {"agent": agent, "started_at": clock(),
                              "ended_at": None}
            if ring is not None:
                calls[call_id]["ring_id"] = ring["id"]
            _save(calls)
        out = {"call_id": call_id, "thread_id": f"direct:{agent}",
               "voice": _voice_of(desk)}
        if ring is None:
            tell(agent, START_LINE)
        else:
            reason = office.defang(ring["reason"])
            tell(agent, ANSWERED_LINE.format(reason=reason)
                 + START_LINE.split(". ", 1)[1])
            memory += (f"\n\nYOU placed this call. Your opening line, already "
                       f"spoken: {reason}")
            out["ring_id"], out["opening"] = ring["id"], ring["reason"]
        try:
            out["realtime"] = realtime.mint(desk, memory, team=team)
        except realtime.RealtimeError as exc:
            out["realtime_error"] = str(exc)
        except Exception as exc:  # a mint must never fail the call
            out["realtime_error"] = f"realtime unavailable ({type(exc).__name__})"
        return out

    def finish(call_id: str) -> dict:
        with _LOCK:
            calls = _load()
            call = calls.get(call_id)
            if not isinstance(call, dict):
                raise Refused(404, "unknown_call", f"no call {call_id!r}")
            if call.get("ended_at") is not None:
                raise Refused(409, "already_ended",
                              "this call has already ended")
            now = clock()
            duration_ms = int(round((now - call["started_at"]) * 1000))
            turns = count_turns(call_id)
            call.update(ended_at=now, duration_ms=duration_ms, turns=turns)
            summary = callmemory.summarise(call.get("transcript") or [],
                                           call["agent"])
            if summary:
                call["summary"] = summary
            _save(calls)
        # ONE line carrying the summary, so the desk wakes once; a call with no
        # transcript keeps the closing line exactly as it was.
        tell(call["agent"], f"{END_LINE} Call summary: {office.defang(summary)}"
             if summary else END_LINE)
        return {"ok": True, "call_id": call_id, "duration_ms": duration_ms,
                "turns": turns}

    def record(call_id: str, lines: list[dict]) -> dict:
        with _LOCK:
            calls = _load()
            call = calls.get(call_id)
            if not isinstance(call, dict):
                raise Refused(404, "unknown_call", f"no call {call_id!r}")
            if call.get("ended_at") is not None:
                raise Refused(409, "already_ended",
                              "this call has already ended")
            call["transcript"] = callmemory.append_lines(
                call.get("transcript") or [], lines)
            _save(calls)
            return {"ok": True, "call_id": call_id,
                    "lines": len(call["transcript"])}

    def pick_up(ring_id: str) -> dict:
        try:
            ring = ringing.answer(ring_id, clock())
        except ringing.RingError as exc:
            raise Refused(exc.status, exc.reason, exc.detail)
        return begin(ring["desk"], ring)

    def turn_away(ring_id: str) -> dict:
        try:
            ring = ringing.decline(ring_id, clock())
        except ringing.RingError as exc:
            raise Refused(exc.status, exc.reason, exc.detail)
        return {"ok": True, "ring_id": ring["id"], "state": ring["state"]}

    def change_settings(changes: dict) -> dict:
        try:
            conf, call_me_now = ringing.update(changes, clock())
        except ringing.RingError as exc:
            raise Refused(exc.status, exc.reason, exc.detail)
        chief = roster.chief(roster.load_roster(roster_path()))
        if call_me_now and chief:
            tell(chief, CALL_ME_NOW_LINE)
        return conf

    # A desk calling him (server/ringing.py). Before `/calls/{call_id}/...`
    # only by habit: the paths cannot collide.
    @router.get("/calls/incoming")
    async def incoming_calls(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON({"ringing": await asyncio.to_thread(ringing.live, clock())})

    @router.post("/calls/incoming/{ring_id}/answer", status_code=201)
    async def answer_incoming(ring_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(pick_up, ring_id),
                          status_code=201)

    @router.post("/calls/incoming/{ring_id}/decline")
    async def decline_incoming(ring_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(turn_away, ring_id))

    @router.get("/calls/settings")
    async def call_settings(request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(ringing.settings, clock()))

    @router.patch("/calls/settings")
    async def edit_call_settings(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(change_settings, payload))

    @router.post("/calls", status_code=201)
    async def start_call(payload: dict, request: Request) -> StrictJSON:
        _authorise(request)
        agent = str(payload.get("agent") or "").strip()
        if not agent:
            raise Refused(400, "missing_agent", "`agent` is required")
        return StrictJSON(await asyncio.to_thread(begin, agent),
                          status_code=201)

    @router.post("/calls/{call_id}/end")
    async def end_call(call_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        return StrictJSON(await asyncio.to_thread(finish, call_id))

    @router.post("/calls/{call_id}/transcript")
    async def call_transcript(call_id: str, payload: dict,
                              request: Request) -> StrictJSON:
        _authorise(request)
        raw = payload.get("lines")
        lines: list[dict] = []
        for item in raw if isinstance(raw, list) else [None]:
            role = item.get("role") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if role not in ("caller", "agent") or not isinstance(text, str):
                raise Refused(400, "bad_lines", "`lines` must be a list of "
                              '{"role":"caller"|"agent","text":"..."}')
            text = callmemory.clean(text, callmemory.LINE_CLIP)
            if text:
                lines.append({"role": role, "text": text})
        return StrictJSON(await asyncio.to_thread(record, call_id, lines))

    return router
