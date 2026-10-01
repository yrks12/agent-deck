"""C3: the desk's live VOICE on a call -- an OpenAI Realtime session.

The Mac connects straight to OpenAI over WebSocket with a short-lived client
secret minted here; the real API key stays on the box (`OPENAI_API_KEY` in the
deck's env) and is never logged, returned or queued. The model only talks:
anything that needs doing goes through its `send_to_desk` tool, which the app
posts to the desk's normal thread. See `docs/calls.md`.
"""

from __future__ import annotations

import hashlib
import os

import httpx

from . import owner, roster
from .hire import HOW_YOU_SOUND  # noqa: F401  (source of the sound rules below)

API = "https://api.openai.com/v1"
WS = "wss://api.openai.com/v1/realtime"
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
TRANSCRIBE = "gpt-4o-mini-transcribe"  # caller speech -> text, for call memory
TTL_SECONDS = 1800  # the secret must outlive the call's first connect only

#: Voices OpenAI lists for Realtime (developers.openai.com, 2026-09).
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
          "verse", "marin", "cedar")

TOOLS = [{
    "type": "function",
    "name": "send_to_desk",
    "description": (
        "Hand work to the desk: anything that needs doing, checking or "
        "looking up. Write a clear, complete instruction as if typing to a "
        "colleague. Call it right away, then tell the caller you're on it."),
    "parameters": {"type": "object",
                   "properties": {"text": {"type": "string"}},
                   "required": ["text"]},
}]


def pick_voice(name: str, voice: dict | None) -> str:
    """Roster voice if it names an OpenAI one, else a stable hash of the name."""
    chosen = (voice or {}).get("openai")
    if chosen in VOICES:
        return chosen
    return VOICES[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(VOICES)]


def instructions(desk: roster.Desk, memory: str = "") -> str:
    who = desk.name.capitalize()
    job = (desk.label or "").strip()
    charter = (desk.charter or desk.mission or "").strip().split("\n")[0][:240]
    role = f"{who}, {job}" if job else who
    persona = f" Your character: {desk.persona.strip()}." if desk.persona else ""
    text = (
        f"You are {role}, on a live phone call with {owner.name()}, who runs the company."
        f" {('You own: ' + charter) if charter else ''}{persona}\n"
        "You are the voice of the desk. You cannot do work or look anything up "
        "yourself: the real desk does that. For anything that needs doing or "
        "checking, call send_to_desk right away with a clear instruction, and "
        "tell him in a few words that you're on it. While you wait you can "
        "chat naturally, but never invent facts, numbers or status: if you "
        "don't know, say the desk is checking.\n"
        f"When a message starting \"[{who} update]\" arrives in the "
        "conversation, relay it in one or two spoken sentences.\n"
        "Sound like a colleague he likes working with: warm, brief, with a "
        "point of view. Short spoken sentences, contractions, plain words. "
        "Answer first. No markdown, no lists, no URLs, no code, no file paths. "
        "Have an opinion and say it. Own a slip in a few words and move on."
    ).replace("  ", " ")
    return f"{text}\n\n{memory}" if memory else text


def _post(url: str, key: str, body: dict) -> dict:
    r = httpx.post(url, json=body, timeout=15.0,
                   headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()


def mint(desk: roster.Desk, memory: str = "") -> dict:
    """The `realtime` block for a call. Raises `RealtimeError` with a reason
    that never contains the key."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RealtimeError("OPENAI_API_KEY is not set on the deck")
    voice = pick_voice(desk.name, desk.voice)
    text = instructions(desk, memory)
    body = {"expires_after": {"anchor": "created_at", "seconds": TTL_SECONDS},
            "session": {"type": "realtime", "model": MODEL,
                        "instructions": text, "tools": TOOLS,
                        "audio": {"input": {"transcription": {"model": TRANSCRIBE}},
                                  "output": {"voice": voice}}}}
    try:
        got = _post(f"{API}/realtime/client_secrets", key, body)
    except httpx.HTTPStatusError as exc:
        raise RealtimeError(f"OpenAI refused the client secret "
                            f"(HTTP {exc.response.status_code})") from None
    except Exception as exc:
        raise RealtimeError(f"could not reach OpenAI ({type(exc).__name__})") from None
    secret, exp = got.get("value"), got.get("expires_at")
    if not isinstance(secret, str) or not secret or not isinstance(exp, int):
        raise RealtimeError("OpenAI returned no client secret")
    return {"ws_url": f"{WS}?model={MODEL}", "client_secret": secret,
            "expires_at": exp, "model": MODEL, "voice": voice,
            "instructions": text, "tools": TOOLS}


class RealtimeError(Exception):
    pass
