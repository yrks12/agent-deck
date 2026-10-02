"""C6: what a desk's voice remembers when a call starts.

Every OpenAI Realtime session begins blank, so `realtime.mint` folds a compact
"What you remember" block into the instructions: the desk's recent direct
thread with the owner, and short records of its last few calls. Pure helpers;
`server/calls.py` owns the state. See `docs/calls.md`.

The thread is read from the same file and with the same owner/desk filters as
`office.conversation`; the deck's own lines (sender `deck`, "[Agent Deck]"
framing) are not his words and are dropped. Everything is redacted on the way
in and again on the way out, and the whole block has a hard ceiling that cuts
the oldest first.
"""

from __future__ import annotations

import json
import re
import time

from . import handoff, office

THREAD_MESSAGES = 20
PAST_CALLS = 3
MESSAGE_CLIP = 600
SUMMARY_CLIP = 700
BLOCK_MAX = 12_000  # about 3k tokens

TRANSCRIPT_LINES = 60
TRANSCRIPT_CHARS = 8_000
LINE_CLIP = 1_000

_SECRET_NAME = re.compile(
    r"(?i)\b\w*(?:token|secret|password|passwd|api[_-]?key)\w*\s*[:=]\s*\S+")


def clean(text: str, limit: int) -> str:
    """One redacted, single-line, clipped string."""
    out = " ".join(handoff.redact(_SECRET_NAME.sub("[redacted]", str(text or ""))).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def recent_thread(name: str, n: int = THREAD_MESSAGES) -> list[str]:
    """The last `n` things said between the owner and desk `name`, oldest first."""
    try:
        rows = office.MESSAGES_FILE.read_text().splitlines()
    except OSError:
        return []
    said: list[str] = []
    for row in rows:
        try:
            rec = json.loads(row)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("ack"):
            continue
        to, sender = str(rec.get("to") or ""), str(rec.get("from") or "")
        if to == name and sender.strip().lower() in office.TYPED_BY_HIM:
            voice = office.OWNER_VOICE
        elif sender == name and office.is_owner(to):
            voice = name.capitalize()
        else:
            continue  # includes sender `deck`: a system voice, not a memory
        body = str(rec.get("text") or "").strip()
        if not body or body.startswith(office.MARK):
            continue
        said.append(f"{voice}: {clean(body, MESSAGE_CLIP)}")
    return said[-n:]


def summarise(transcript: list[dict], name: str) -> str:
    """Deterministic, no LLM: the first request plus how the call ended."""
    lines = [l for l in transcript if isinstance(l, dict) and l.get("text")]
    if not lines:
        return ""
    who = {"caller": office.OWNER_VOICE, "agent": name.capitalize()}
    first = next((l for l in lines if l.get("role") == "caller"), lines[0])
    parts = [f"{office.OWNER_VOICE} first asked: {clean(first['text'], 200)}"]
    tail = lines[-4:]
    if len(lines) > 1:
        ending = " | ".join(f"{who.get(l.get('role'), '?')}: {clean(l['text'], 140)}"
                            for l in tail)
        parts.append(f"Ended with: {ending}")
    out = " ".join(parts)
    return out if len(out) <= SUMMARY_CLIP else out[: SUMMARY_CLIP - 1] + "…"


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    return f"{int(round(seconds / 60))} min"


def _call_entries(calls: dict, agent: str) -> list[str]:
    done = [c for c in calls.values()
            if isinstance(c, dict) and c.get("agent") == agent
            and c.get("ended_at") is not None]
    done.sort(key=lambda c: c["ended_at"])
    out = []
    for c in done[-PAST_CALLS:]:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c["started_at"]))
        length = _ago((c.get("duration_ms") or 0) / 1000)
        what = clean(c.get("summary") or "no transcript was recorded", SUMMARY_CLIP)
        out.append(f"- {when}, {length} long: {what}")
    return out


def block(agent: str, calls: dict) -> str:
    """The memory section, or "" when there is nothing to remember."""
    thread = recent_thread(agent)
    past = _call_entries(calls, agent)

    def render() -> str:
        parts = ["What you remember (from earlier; do not recite it, use it "
                 "when it helps):"]
        if past:
            parts.append(f"Previous calls with {office.OWNER_VOICE}, oldest first:\n"
                         + "\n".join(past))
        if thread:
            parts.append(f"Recent chat between you and {office.OWNER_VOICE}, oldest first:\n"
                         + "\n".join(thread))
        return "\n\n".join(parts)

    while (thread or past) and len(render()) > BLOCK_MAX:
        (thread or past).pop(0)
    return render() if (thread or past) else ""


def append_lines(existing: list[dict], new: list[dict]) -> list[dict]:
    """Add `new` lines, then keep the newest within the line and char caps."""
    kept = [*existing, *new][-TRANSCRIPT_LINES:]
    while len(kept) > 1 and sum(len(l["text"]) for l in kept) > TRANSCRIPT_CHARS:
        kept.pop(0)
    return kept
