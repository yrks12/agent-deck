"""C3: the desk's live VOICE on a call -- an OpenAI Realtime session.

The Mac connects straight to OpenAI over WebSocket with a short-lived client
secret minted here; the real API key stays on the box (`OPENAI_API_KEY` in the
deck's env) and is never logged, returned or queued. See `docs/calls.md`.

The model is a THIN voice layer over the desk (owner, 2026-10-01: "On the chat,
Atlas performs way better than in a call"). Measured that day: two thirds of
what he said on a call was answered by this model alone, from a one-paragraph
persona, and it denied abilities the desk has. Now every substantive turn goes
to the desk in his own words (`send_to_desk`, posted by the app to the desk's
normal thread), the model speaks the desk's answer, and a decision card is
answered by voice (`answer_card`). It is briefed with what chat Atlas is
briefed with first -- identity, detected abilities, the team memory index --
so even its two-word acks are on-brand.
"""

from __future__ import annotations

import hashlib
import os

import httpx

from . import capabilities, learning, owner, roster
from .hire import HOW_YOU_SOUND  # noqa: F401  (source of the sound rules below)

API = "https://api.openai.com/v1"
WS = "wss://api.openai.com/v1/realtime"
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
TRANSCRIBE = "gpt-4o-mini-transcribe"  # caller speech -> text, for call memory
TTL_SECONDS = 1800  # the secret must outlive the call's first connect only

#: Voices OpenAI lists for Realtime (developers.openai.com, 2026-09).
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
          "verse", "marin", "cedar")

#: The whole instructions string. gpt-realtime reads it on every turn, so it
#: is held to a budget: the parts below are capped, memory goes first.
INSTRUCTIONS_MAX = 21_000
#: Team-memory lines shown (titles + one-line hooks), and their budget.
TEAM_SHOWN = 20
TEAM_MAX = 2_400
CHARTER_MAX = 800

TOOLS = [{
    "type": "function",
    "name": "send_to_desk",
    "description": (
        "Give the desk what he just asked or told you: every question about "
        "work, facts, status, numbers, the team or what you can do, and every "
        "request to do something. The desk answers; you speak its answer."),
    "parameters": {"type": "object",
                   "properties": {"text": {
                       "type": "string",
                       "description": (
                           "His exact words, as he said them, in his language "
                           "and in the first person. Do not rephrase, "
                           "summarise or add to them; if he said several "
                           "things in a row, give them all.")}},
                   "required": ["text"]},
}, {
    "type": "function",
    "name": "answer_card",
    "description": (
        "Answer a decision card the desk put to him, after he has picked by "
        "voice. Use the card id from the \"asks you to pick\" message."),
    "parameters": {"type": "object",
                   "properties": {
                       "card_id": {"type": "string",
                                   "description": "The card's id, dec_..."},
                       "choice": {"type": "string",
                                  "description": (
                                      "The option he picked, by its label, "
                                      "or his own words if none fits.")}},
                   "required": ["card_id", "choice"]},
}]


def pick_voice(name: str, voice: dict | None) -> str:
    """Roster voice if it names an OpenAI one, else a stable hash of the name."""
    chosen = (voice or {}).get("openai")
    if chosen in VOICES:
        return chosen
    return VOICES[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(VOICES)]


def _team_index(limit: int = TEAM_SHOWN) -> str:
    """The team memory INDEX the desks are briefed on: titles and hooks only."""
    try:
        shown = learning.lessons()[:limit]
    except Exception:
        return ""
    if not shown:
        return ""
    head = ("Team memory (what the team has learned; the desk reads the full "
            "lesson, you only know these lines):")
    rows = [f"- {l.title} -- {l.hook}" for l in shown]
    while rows and len("\n".join([head, *rows])) > TEAM_MAX:
        rows.pop()
    return "\n".join([head, *rows]) if rows else ""


def brief(desk: roster.Desk, team: list[roster.Desk] | None = None) -> str:
    """Who the desk is and what it can do: the first two sections of the
    chat desk's own brief (`server/capabilities.py`), so the voice is never
    the one that says "I can't". A probe that fails costs only that part."""
    parts = []
    try:
        parts.append(capabilities.identity(desk, team or []))
    except Exception:
        pass
    charter = " ".join((desk.charter or desk.mission or "").split())
    if charter:
        parts.append("What you own: " + (charter if len(charter) <= CHARTER_MAX
                                         else charter[:CHARTER_MAX - 1] + "…"))
    try:
        parts.append(capabilities.render(capabilities.current(desk), desk))
    except Exception:
        pass
    index = _team_index()
    if index:
        parts.append(index)
    return "\n\n".join(p for p in parts if p)


def instructions(desk: roster.Desk, memory: str = "", *,
                 team: list[roster.Desk] | None = None,
                 known: str | None = None) -> str:
    """The voice layer's rules, then the desk's identity, abilities and team
    memory (`known`, built by `brief` when not given), then the memory block.
    Over budget, the memory block is cut first, then `known`."""
    who = desk.name.capitalize()
    boss = owner.name()
    persona = f" Your character: {desk.persona.strip()}." if desk.persona else ""
    rules = (
        f"You are {who}, on a live phone call with {boss}, who runs the "
        f"company.{persona} This call is the same {who} he chats with: the "
        f"desk -- {who} at work, with every account, tool, file and the team "
        "below -- does the thinking and the work, and you are its voice.\n"
        "How every turn goes:\n"
        "1. Every question about work, facts, status, numbers, money, the "
        "team, his companies, what was done, or what you can or can't do, "
        "and every request to do something: call send_to_desk at once with "
        "his exact words, and say only a two-to-four word acknowledgement in "
        "his language, like \"On it.\" Do not answer it yourself, do not "
        "guess, and do not say \"let me think\".\n"
        "2. Only greetings, thanks, small talk, and asking him to repeat or "
        "clarify are yours to answer directly.\n"
        f"3. A message starting \"[{who} update]\" is the desk's real "
        "answer or progress. Speak it to him at once, in the language he is "
        "speaking, condensed to at most three short spoken sentences. Use "
        "only what it says: add no facts, numbers or promises, and never "
        "contradict it. A bare \"on it\" update is a few words.\n"
        f"4. A message starting \"[{who} asks you to pick]\" is a decision "
        "card: ask him the question and read the options. When he answers, "
        "call answer_card with that card's id and his choice, then say what "
        "you picked in a few words.\n"
        "5. Never say you can't do, see, check or reach something: the desk "
        "can, so send it. Never invent facts, numbers or status: until an "
        "update arrives you don't know, so say the desk is on it.\n"
        "Sound like a colleague he likes working with: warm, brief, direct. "
        "Short spoken sentences, plain words, answer first. No markdown, no "
        "lists, no URLs, no code, no file paths."
    )
    if known is None:
        known = brief(desk, team)
    known_part = (f"What {who} is and can do (all of it through the desk):\n"
                  f"{known}") if known else ""

    def join(*parts: str) -> str:
        return "\n\n".join(p for p in parts if p)

    text = join(rules, known_part, memory)
    if len(text) > INSTRUCTIONS_MAX:
        room = INSTRUCTIONS_MAX - len(join(rules, known_part)) - 2
        memory = memory[-room:] if room > 200 else ""
        text = join(rules, known_part, memory)
    return text[:INSTRUCTIONS_MAX]


def _post(url: str, key: str, body: dict) -> dict:
    r = httpx.post(url, json=body, timeout=15.0,
                   headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()


def mint(desk: roster.Desk, memory: str = "", *,
         team: list[roster.Desk] | None = None) -> dict:
    """The `realtime` block for a call. Raises `RealtimeError` with a reason
    that never contains the key."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RealtimeError("OPENAI_API_KEY is not set on the deck")
    voice = pick_voice(desk.name, desk.voice)
    text = instructions(desk, memory, team=team)
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
