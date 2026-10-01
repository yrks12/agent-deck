"""Secure handoff: the step only a human can take, and the way back.

**This is not the approval layer.** `server/autoreview.py` answers *permission*
questions -- the agent could do the thing, it needs a yes, and the yes becomes a
durable rule. This module is for the case where no rule helps because the agent
**cannot act at all**: a 2FA code arriving on a phone, a CAPTCHA, an SMS
confirmation, a card payment, an `ssh` passphrase, a `gh auth login` device
code. There is no permission to grant. A human has to physically do it.

**And -- this is the part that is genuinely different from the permission case
-- the loop closes.** Spec M1 is the load-bearing negative for approvals: the
CLI's socket protocol has no frame that answers a permission prompt that is
already drawn, so a prompt on screen can never be dismissed from outside. That
does not apply here. Resuming an agent after a handoff is just a *new user
message* into its session, which is exactly what the `user` frame already does
(`manager.inject`). So: agent blocks -> phone buzzes -> human acts -> the
outcome is injected back as an ordinary instruction -> the agent carries on.
End to end, with no missing primitive. Do not assume this is blocked the way
answering a prompt is; it is not.

Three rules hold this module together:

1. **A handoff that does not state the current state of the work is a bug.**
   `state` is validated at `raise_handoff` and always printed by `compose`.
   Without it the phone message says "I need you" and nothing about whether
   money is already being spent, which is how you end up opening a laptop in a
   panic over a draft campaign that never went live.
2. **`done` and `skipped` are different instructions, not different words.**
   `done` means a human says they did it -- the agent must *re-check* that it
   actually worked. `skipped` means it will never happen -- the agent must
   *abandon* that path and report what it cannot finish. Collapse the two and
   a skipped handoff becomes an infinite retry loop.
3. **Nothing secret leaves this module.** Every string that can reach a phone
   goes through `redact()`. A one-time code in a message that syncs to another
   device is the precise thing a secure handoff exists to prevent.

Everything here is stdlib, and `compose`, `resume_message`,
`evidence_from_transcript` and `redact` are pure. Wiring -- the HTTP endpoint,
the WhatsApp send, the injection back into the session -- lives elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .expiry import DEFAULT_TTL, sweep
from .paths import BUS_DIR
from . import atomic

KINDS = ("2fa", "captcha", "login", "payment", "device_code", "other")

OUTCOMES = ("taken_over", "done", "skipped")

# "expired" is deliberately NOT an outcome. A payment nobody confirmed did not
# happen; a 2FA code nobody typed was not typed. Expiry says the question left
# the board, and says nothing at all about the work -- collapsing it into
# `done` would report a charge that was never made as made.
STATUSES = ("waiting", *OUTCOMES, "stale", "expired")

# `DEFAULT_TTL` is imported, not redefined: handoffs and asks must age out on
# the same clock. See server/expiry.py.

DEFAULT_PATH: Path = BUS_DIR / "handoffs.json"
MAX_HANDOFFS = 50

# The message has to be readable on a lock screen without scrolling.
PHONE_LIMIT = 600

# Fields are clipped before they reach the phone, so one runaway line cannot
# push the state sentence or the reply options off the bottom.
FIELD_LIMIT = 220
MAX_EVIDENCE = 2000

REDACTED = "[redacted]"

# No 0/O, no 1/l/I, no case to get wrong: an id is something the owner types with a
# thumb, on a train, to answer a message.
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ID_LEN = 4

# A desk takes a moment to show up in the live set (it is derived from hook
# events and polled session files). Sweeping inside that window would mark a
# handoff stale before the phone had even buzzed, so `now` buys a grace period.
STALE_GRACE = 30.0

# Screenshot evidence is opt-in. macOS `screencapture` needs Screen Recording
# permission, which may simply not be granted, and the feature must work
# without it -- for a terminal agent the tail of its own output is better
# evidence anyway. Never exercised by the tests.
SCREENSHOT_ENV = "DECK_HANDOFF_SCREENSHOT"


@dataclass(frozen=True)
class Handoff:
    id: str                  # short, unambiguous, safe to type on a phone
    ts: float
    agent: str               # the desk/session name that is blocked
    kind: str                # one of KINDS
    needs: str               # what the human must physically do, one sentence
    state: str               # the state of the work RIGHT NOW - load-bearing
    where: str               # url, host, app, or path - where the human goes
    evidence: str = ""       # a captured screen path, or a text excerpt
    status: str = "waiting"  # waiting | taken_over | done | skipped | stale


# ── redaction ──────────────────────────────────────────────────────────────
#
# Order matters: the labelled form ("password: hunter2") is caught first, so a
# short secret that no shape rule would notice is still removed.

_REDACTORS: tuple[tuple[re.Pattern[str], str], ...] = (
    # user:pass@host in any URL -- keep the host, it is where he has to go.
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]*@", re.I),
     r"\1" + REDACTED + "@"),
    # ?token=... / &api_key=... query parameters.
    (re.compile(r"([?&](?:access_?token|api[_-]?key|apikey|auth|code|key|otp|"
                r"passwd|password|pwd|secret|sig|signature|token)=)[^&\s#]+", re.I),
     r"\1" + REDACTED),
    # "password is hunter2", "code: 417293", "token = abc".
    (re.compile(r"\b(passphrase|passwords?|passwd|pwd|secrets?|tokens?|"
                r"api[_-]?keys?|apikey|otp|pin|codes?)\b(\s*(?:is|are|:|=)\s*)"
                r"(?!\[redacted\])\S+", re.I),
     r"\1\2" + REDACTED),
    # Authorization headers.
    (re.compile(r"\b(Bearer|Basic)\s+\S+", re.I), r"\1 " + REDACTED),
    # Vendor-prefixed keys that announce themselves.
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}|\bgh[pousr]_[A-Za-z0-9]{16,}"
                r"|\bxox[baprs]-[A-Za-z0-9\-]{8,}|\bAKIA[0-9A-Z]{12,}"),
     REDACTED),
    # A peerToken is /^[0-9a-f]{32}$/ (spec, "Injection auth"); so is most of
    # what a session key file holds.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), REDACTED),
    # Any other long opaque blob. Real prose does not run 40 characters
    # without a space or punctuation; URLs break on / and . so they survive.
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), REDACTED),
    # A bare one-time code. 6-8 digits, not part of a longer number, a version
    # or a dotted address -- so "£20/day" and "192.168.1.20" are untouched.
    (re.compile(r"(?<![\w.\-])\d{6,8}(?![\w.\-])"), REDACTED),
)


def redact(text: str) -> str:
    """Scrub anything that looks like a secret. PURE.

    Deliberately over-eager: a redacted campaign budget is an annoyance, a
    leaked one-time code sitting in a chat that syncs to a second device is the
    failure this whole feature exists to avoid. The one thing it must not do is
    eat ordinary prose, which is why the shape rules all require either a
    label, a scheme, a vendor prefix or an implausibly long run of characters.
    """
    out = str(text or "")
    for pattern, repl in _REDACTORS:
        out = pattern.sub(repl, out)
    return out


def _clean(text: str, limit: int = FIELD_LIMIT) -> str:
    """One redacted, whitespace-collapsed line, clipped to `limit`."""
    out = " ".join(redact(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


# ── the store ──────────────────────────────────────────────────────────────


def _handoff_from(row: dict) -> Handoff | None:
    if not isinstance(row, dict):
        return None
    try:
        kind = str(row["kind"])
        status = str(row.get("status") or "waiting")
        return Handoff(
            id=str(row["id"]),
            ts=float(row.get("ts") or 0.0),
            agent=str(row["agent"]),
            kind=kind if kind in KINDS else "other",
            needs=str(row["needs"]),
            state=str(row["state"]),
            where=str(row["where"]),
            evidence=str(row.get("evidence") or ""),
            status=status if status in STATUSES else "waiting",
        )
    except (KeyError, TypeError, ValueError):
        return None


def load(path: Path) -> list[Handoff]:
    """Every handoff on file, oldest first. Missing or unreadable -> [].

    A queue that cannot be parsed is an empty queue, never an exception: a
    corrupt file must not take the daemon down on a poll.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("handoffs") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    parsed = [_handoff_from(row) for row in rows]
    return [h for h in parsed if h is not None]


def save(path: Path, handoffs: list[Handoff]) -> None:
    """Atomic write, capped at MAX_HANDOFFS newest -- as roster.py does.

    Unbounded history on a path the board polls is how `edits.jsonl` reached
    3.2 MB (spec §3), so the cap is not decoration.
    """
    path = Path(path)
    kept = handoffs[-MAX_HANDOFFS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps({"version": 1,
                                        "handoffs": [asdict(h) for h in kept]}))


def new_id(taken: set[str]) -> str:
    """A short id no other live handoff is using."""
    for _ in range(200):
        candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if candidate not in taken:
            return candidate
    # Astronomically unlikely with a 50-deep queue; grow rather than loop.
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN + 2))


def raise_handoff(path: Path, *, agent: str, kind: str, needs: str, state: str,
                  where: str, evidence: str = "") -> Handoff:
    """Queue a block only a human can clear, and hand back the record.

    `state` is required and non-empty on purpose. An agent that raises a
    handoff without saying what it has already done is asking the owner to guess
    whether anything went live, and the guess is always the frightening one.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    for label, value in (("agent", agent), ("needs", needs),
                         ("state", state), ("where", where)):
        if not str(value or "").strip():
            raise ValueError(f"{label} is required and must not be empty")

    existing = load(path)
    handoff = Handoff(
        id=new_id({h.id for h in existing}),
        ts=time.time(),
        agent=str(agent).strip(),
        kind=kind,
        needs=str(needs).strip(),
        state=str(state).strip(),
        where=str(where).strip(),
        evidence=str(evidence or "")[:MAX_EVIDENCE],
    )
    save(path, existing + [handoff])
    return handoff


def waiting(path: Path) -> list[Handoff]:
    """Still blocking somebody, oldest first -- the oldest block hurts most.

    `taken_over` is deliberately not here and deliberately not `waiting`
    either: the human has the keyboard, so it needs no second nudge, but the
    work is not finished and must not vanish from the record.
    """
    return [h for h in load(path) if h.status == "waiting"]


def expire(path: Path, *, now: float | None = None,
           ttl: float = DEFAULT_TTL) -> list[Handoff]:
    """Time out handoffs nobody came back to. Returns the ones that changed.

    Different from `mark_stale`, and both are needed. `mark_stale` retires a
    handoff whose *agent* is gone -- the block can never be cleared because
    there is nothing left to resume. This retires a handoff whose *human* never
    came, which is the ordinary case: the phone buzzed during dinner and the
    moment passed.

    **This is not an outcome.** The status becomes `expired`, which is not in
    `OUTCOMES`, so nothing downstream can read it as the step having been done,
    skipped or taken over. A resolved handoff is never re-marked -- `taken_over`
    in particular, because a human at a keyboard on a long job looks exactly
    like an abandoned one from here, and sweeping it would tell the agent to
    stop waiting for someone who is still working.
    """
    at = time.time() if now is None else float(now)
    rows = load(path)
    after, changed = sweep(
        rows,
        now=at,
        ttl=ttl,
        ts_of=lambda h: h.ts,
        is_open=lambda h: h.status == "waiting",
        mark=lambda h: replace(h, status="expired"),
    )
    if changed:
        save(path, after)
    return changed


def resolve(path: Path, handoff_id: str, outcome: str) -> Handoff:
    """Record what the human did. Returns the updated handoff.

    Raises ValueError on an outcome that is not one of OUTCOMES, and KeyError
    on an id that is not on file -- a mistyped id must not silently resolve
    the wrong block.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    wanted = str(handoff_id or "").strip().lower()
    rows = load(path)
    for index, h in enumerate(rows):
        if h.id.lower() == wanted:
            updated = replace(h, status=outcome)
            rows[index] = updated
            save(path, rows)
            return updated
    raise KeyError(f"no handoff {handoff_id!r}")


def mark_stale(path: Path, *, alive: set[str], now: float) -> list[Handoff]:
    """Retire handoffs whose agent is gone. Returns the ones that changed.

    A desk that closed its terminal cannot be resumed, so its handoff can never
    be cleared by anything the owner does -- left `waiting` it sits at the top of the
    queue forever and teaches him to ignore the queue.

    `now` exists for the grace window: a handoff raised in the last
    STALE_GRACE seconds is never swept, because "not in the live set yet" and
    "dead" look identical from here.
    """
    rows = load(path)
    changed: list[Handoff] = []
    for index, h in enumerate(rows):
        if h.status != "waiting":
            continue
        if h.agent in alive:
            continue
        if now - h.ts < STALE_GRACE:
            continue
        rows[index] = replace(h, status="stale")
        changed.append(rows[index])
    if changed:
        save(path, rows)
    return changed


# ── the phone message ──────────────────────────────────────────────────────

_KIND_LABEL = {
    "2fa": "two-factor code",
    "captcha": "CAPTCHA",
    "login": "sign-in",
    "payment": "payment confirmation",
    "device_code": "device code",
    "other": "step",
}


def compose(h: Handoff) -> str:
    """The message that lands on the phone. PURE.

    Five things, in this order, because that is the order a person reads them
    in when their phone buzzes:

      1. who is blocked,
      2. what he has to physically do,
      3. **the state of the work right now** -- the load-bearing line, bold,
         because it is what decides whether this is urgent or can wait,
      4. where to go,
      5. the three replies.

    Evidence is deliberately not inlined: the screenshot or the log tail rides
    on the card in the deck, and pasting a log tail into a phone message pushes
    the state line off the screen, which defeats the point.
    """
    return "\n".join([
        f"*{_clean(h.agent, 60)}* is blocked and needs you.",
        "",
        _clean(h.needs),
        "",
        f"*State: {_clean(h.state)}*",
        "",
        f"Go to: {_clean(h.where)}",
        "",
        f"Reply *take over {h.id}* / *done {h.id}* / *skip {h.id}*.",
    ])[:PHONE_LIMIT]


# ── the way back in ────────────────────────────────────────────────────────

# Injected as an ordinary user message. Nothing exotic is needed: unlike
# answering a live permission prompt (spec M1, impossible over the socket),
# resuming after a handoff is just the next instruction in the session.

_RESUME_BODY = {
    "done": (
        "A human says they completed it. Do NOT assume it worked: go back and "
        "verify the step actually succeeded -- re-run the exact check that "
        "failed before, and read the result. Only carry on once that check "
        "passes. If it still blocks, raise a NEW handoff describing what you "
        "saw; do not loop on it."
    ),
    "skipped": (
        "This step will NOT happen. Abandon that path entirely and do not "
        "retry it, now or later -- retrying is the failure mode here. Report "
        "plainly what you can no longer finish because of it, then continue "
        "with whatever remains that does not depend on it."
    ),
    "taken_over": (
        "A human has the keyboard and is doing it right now. Hands off: do "
        "not touch that screen, session or resource, and do not retry it. "
        "Wait for the next message telling you it is done or skipped, and "
        "meanwhile only do work that is entirely independent of it."
    ),
}


def resume_message(h: Handoff, outcome: str) -> str:
    """What gets injected back into the blocked agent's session. PURE.

    `done` and `skipped` must never collapse into one another. `done` orders a
    re-check, because "I did it" from a human is a claim, not a fact -- the
    2FA may have timed out, the payment may have been declined. `skipped`
    orders abandonment, because an agent that treats a skip as a retry-with-a-
    pause is an agent that hammers a login screen until something locks it.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    label = _KIND_LABEL.get(h.kind, "step")
    return (
        f"Handoff {h.id} ({label}) resolved: {outcome}. "
        f"You were blocked on: {_clean(h.needs)} "
        f"{_RESUME_BODY[outcome]}"
    )


# ── evidence ───────────────────────────────────────────────────────────────


def evidence_from_transcript(lines: list[str], limit: int = 20) -> str:
    """The tail of the agent's own output, redacted. PURE.

    For a terminal agent this is the honest equivalent of the reference app's
    screenshot: the last thing it printed before it stopped, which is almost
    always the prompt it could not answer. Redacted with the same rule as the
    phone message -- a 2FA prompt that echoes the code is exactly the sort of
    line that ends up here.
    """
    kept = [str(line).rstrip() for line in (lines or []) if str(line).strip()]
    if not kept:
        return ""
    tail = kept[-max(1, int(limit)):]
    out = "\n".join(redact(line) for line in tail)
    return out[-MAX_EVIDENCE:]


def capture_screen(dest: Path) -> str:
    """Optional macOS screenshot. Returns the path, or "" if it did not happen.

    Off unless DECK_HANDOFF_SCREENSHOT=1, because `screencapture` needs Screen
    Recording permission that may never have been granted -- and when it has
    not been, macOS returns a blank or desktop-only image rather than an error,
    which is worse than no evidence at all. Nothing in this module depends on
    it, and no test calls it.
    """
    if os.environ.get(SCREENSHOT_ENV) != "1":
        return ""
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["/usr/sbin/screencapture", "-x", str(dest)],
                       check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return str(dest) if dest.exists() else ""
