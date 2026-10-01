"""Spoken replies in the call's voice, and voice messages that are not a call.

Owner, 2026-09-30: "when I chat with voice it answers in voice automatically,
but I want to switch it off, and use the normal voice like in the call" and
"I want to just record a message and not call it".

* `POST /v1/speech` -- words -> mp3 in the desk's CALL voice
  (`realtime.pick_voice`, the same OpenAI voice the live call speaks with),
  through OpenAI's speech endpoint. Cached on disk by (model, voice, words),
  so a replayed reply or the recurring "Asking Atlas..." costs nothing.
* `POST /v1/threads/{id}/voice-notes` -- a recording -> transcribed HERE ->
  an ordinary owner message (delivered, or the desk woken, exactly like typing)
  that carries the recording's id. `GET /v1/voice-notes/{id}` plays it back.

The OpenAI key lives in the deck's env and nowhere else: it is never returned,
logged, cached or queued. Every failure reason is built from a status code or
an exception's type name, never its text (which may quote the request).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Callable

import httpx
from fastapi import APIRouter, Request, Response

from . import office, realtime, roster
from .api import OWNER, Refused, StrictJSON, _authorise, _parse_thread_id

API = realtime.API
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
#: The accurate one, not `-mini`: Hebrew and English mixed in one sentence is
#: his normal speech.
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TTS_STYLE = ("Speak like a warm, brief colleague on a phone call: natural "
             "pace, relaxed, never announcer-like.")
MAX_TEXT = 1500
MAX_AUDIO = 20 * 1024 * 1024
CACHE_KEEP = 400
_NOTE_ID = re.compile(r"^vn_[0-9a-f]{16}$")


def _key(reason: str) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise Refused(503, reason, "OPENAI_API_KEY is not set on the deck")
    return key


def _speech(key: str, body: dict) -> bytes:
    r = httpx.post(f"{API}/audio/speech", json=body, timeout=30.0,
                   headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.content


def _transcribe(key: str, data: bytes, filename: str, mime: str) -> str:
    r = httpx.post(f"{API}/audio/transcriptions", timeout=60.0,
                   headers={"Authorization": f"Bearer {key}"},
                   files={"file": (filename, data, mime)},
                   data={"model": TRANSCRIBE_MODEL, "response_format": "json"})
    r.raise_for_status()
    return str(r.json().get("text") or "")


def _why(exc: Exception) -> str:
    """A failure reason that cannot carry the key."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"OpenAI answered HTTP {exc.response.status_code}"
    return f"could not reach OpenAI ({type(exc).__name__})"


def cache_dir() -> Path:
    return office.BUS_DIR / "tts"


def notes_dir() -> Path:
    return office.BUS_DIR / "voice_notes"


def _voice_for(agent: str, roster_path: Path) -> str:
    desk = next((d for d in roster.load_roster(roster_path) if d.name == agent), None)
    return realtime.pick_voice(agent or "agent deck", desk.voice if desk else None)


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    for old in files[:-CACHE_KEEP]:
        old.unlink(missing_ok=True)


def speak(text: str, agent: str, roster_path: Path) -> tuple[bytes, str, bool]:
    """(mp3, voice, was_cached). Raises `Refused` with a key-free reason."""
    words = (text or "").strip()[:MAX_TEXT]
    if not words:
        raise Refused(400, "empty_text", "nothing to say")
    voice = _voice_for(agent, roster_path)
    digest = hashlib.sha256(f"{TTS_MODEL}\0{voice}\0{words}".encode()).hexdigest()[:32]
    path = cache_dir() / f"{digest}.mp3"
    if path.is_file():
        os.utime(path)
        return path.read_bytes(), voice, True
    key = _key("speech_unavailable")
    body = {"model": TTS_MODEL, "voice": voice, "input": words,
            "instructions": TTS_STYLE, "response_format": "mp3"}
    try:
        audio = _speech(key, body)
    except Exception as exc:
        raise Refused(502, "speech_failed", _why(exc)) from None
    if not audio:
        raise Refused(502, "speech_failed", "OpenAI returned no audio")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(path)
        _prune(path.parent)
    except OSError:
        pass  # a cache that cannot be written is only slower
    return audio, voice, False


def transcribe(data: bytes, mime: str) -> str:
    key = _key("transcription_unavailable")
    try:
        return _transcribe(key, data, "voice-note.m4a", mime).strip()
    except Exception as exc:
        raise Refused(502, "transcription_failed", _why(exc)) from None


def build_router(surface, *, roster_path: Callable[[], Path]) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    @router.post("/speech")
    async def speech(payload: dict, request: Request) -> Response:
        _authorise(request)
        audio, voice, hit = await asyncio.to_thread(
            speak, str(payload.get("text") or ""),
            str(payload.get("agent") or "").strip().lower(), roster_path())
        return Response(audio, media_type="audio/mpeg",
                        headers={"X-Deck-Voice": voice,
                                 "X-Deck-Cache": "hit" if hit else "miss",
                                 "Cache-Control": "private, max-age=86400"})

    @router.post("/threads/{thread_id}/voice-notes", status_code=201)
    async def voice_note(thread_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        declared = int(request.headers.get("content-length") or 0)
        if declared > MAX_AUDIO:
            raise Refused(413, "too_long", "a voice message is at most 20 MB")
        data = await request.body()
        if not data:
            raise Refused(400, "empty_audio", "the recording was empty")
        if len(data) > MAX_AUDIO:
            raise Refused(413, "too_long", "a voice message is at most 20 MB")
        mime = (request.headers.get("content-type") or "audio/mp4").split(";")[0].strip()
        # The thread is checked BEFORE OpenAI is paid to listen.
        await asyncio.to_thread(surface.refresh)
        thread_id = surface._canonical_thread(thread_id)
        kind, who = _parse_thread_id(thread_id)
        if kind != "direct":
            raise Refused(400, "direct_only",
                          "a voice message goes to one agent's own chat")
        surface.agent(who[0])
        words = await asyncio.to_thread(transcribe, data, mime)
        if not words:
            raise Refused(422, "nothing_heard",
                          "nothing could be heard in the recording")
        note_id = "vn_" + uuid.uuid4().hex[:16]
        folder = notes_dir()
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((folder / f"{note_id}.m4a").write_bytes, data)
        result = await asyncio.to_thread(
            surface.send, thread_id, words, sender=OWNER, voice_note=note_id)
        publish = getattr(surface, "_publish", None)
        if publish is not None:
            await publish(await asyncio.to_thread(surface.refresh))
        return StrictJSON({**result, "transcript": words}, status_code=201)

    @router.get("/voice-notes/{note_id}")
    async def voice_note_audio(note_id: str, request: Request) -> Response:
        _authorise(request)
        path = notes_dir() / f"{note_id}.m4a"
        if not _NOTE_ID.match(note_id) or not path.is_file():
            raise Refused(404, "unknown_voice_note", "no such voice message")
        return Response(await asyncio.to_thread(path.read_bytes),
                        media_type="audio/mp4",
                        headers={"Cache-Control": "private, max-age=86400"})

    return router
