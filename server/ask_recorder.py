"""The join that was missing: an `ask` verdict becomes a question on the board.

`autoreview.evaluate` could always answer `ask`, and `asking.record` could
always write a question down, but nothing connected them. So `POST /api/approve`
returned `{"decision": "ask"}`, the session drew its own permission prompt, and
`asks.json` stayed empty forever -- which means the approval card renders
nothing, the owner is never offered the one word that creates the rule, and the whole
"asks once, then never again" claim dies at its first step. This module is that
connection and nothing else.

It is a seam on purpose. `on_verdict` takes the verdict rather than computing
one, so the decision stays in `autoreview` and cannot drift into a second
implementation here; it takes the path rather than reading a global, so a test
never touches the real `~/.claude/agent-bus`; and it writes no rules and sends no
messages, because both of those are somebody else's job further down the line.

Three judgements live here, and only three:

1. **Record only `ask`.** An `allow` or a `deny` was already decided by a rule
   the owner wrote; putting it on the board gives him rows he cannot act on, and a
   board of those is one he stops reading -- at which point the question that
   did matter goes unread with everything else.
2. **Honour `asking.suppressed`.** An agent in a retry loop asks the same thing
   forty times in a minute. That is one interruption, not forty. The match stays
   exact on tool + subject + folder, so a *different* question is never
   swallowed -- a swallowed question is an agent stalled forever with nobody
   knowing why.
3. **Record the handoff floor too, and flag it.** A credential or an
   irreversible command must still reach him -- it just can never become
   standing policy, so the card shows "once"/"never" and refuses "always".

The subject written down is `autoreview.tool_subject(...)` -- the *same* string
`evaluate` matches its patterns against. That is not a detail: `asking.rule_from`
builds the standing rule out of the recorded subject, so if these two ever
differed, the rule an answer creates would never match the call that provoked it
and the agent would ask forever despite being answered.
"""

from __future__ import annotations

from pathlib import Path

from . import asking
from .autoreview import Verdict, tool_subject

# The flood-guard window, in seconds. Fifteen minutes is long enough to cover a
# retry storm and short enough that a question he never answered comes back
# rather than being permanently muted -- a mute is a deny nobody decided.
DEFAULT_WINDOW: float = 900.0

# How much of a session id is enough to recognise the sender on a phone.
ID_PREFIX = 8


def _decision_of(verdict: Verdict | dict | None) -> str:
    """The decision, whether it arrived as a Verdict or as endpoint JSON.

    The HTTP layer round-trips verdicts as dicts, so accepting both keeps the
    caller from having to rebuild one. Anything unrecognisable is NOT treated as
    an ask: a garbled verdict is a bug, and a bug must not fill the board.
    """
    if isinstance(verdict, dict):
        return str(verdict.get("decision") or "")
    return str(getattr(verdict, "decision", "") or "")


def _who(agent: str, session_id: str) -> str:
    """The name on the first line of the message.

    Nothing guarantees a desk name reaches the hook, and "**** wants to run rm
    -rf" is a question he cannot answer. A short session id is worse than a name
    and far better than nothing.
    """
    name = str(agent or "").strip()
    if name:
        return name
    ident = str(session_id or "").strip()
    if ident:
        return f"session {ident[:ID_PREFIX]}"
    return "an agent"


def is_handoff_ask(ask: asking.Ask) -> bool:
    """Must this row refuse the "always" option?

    Derived from the recorded subject rather than stored beside it, so it cannot
    fall out of step with `asking.answer`, which downgrades `always` to `once`
    using exactly the same test. One floor, asked twice, never two floors.
    """
    return asking.is_handoff_subject(ask.subject)


def on_verdict(
    asks_path: Path | str,
    *,
    verdict: Verdict | dict | None,
    tool_name: str,
    tool_input: object,
    cwd: str,
    session_id: str = "",
    agent: str = "",
    now: float | None = None,
    window: float = DEFAULT_WINDOW,
) -> asking.Ask | None:
    """Turn one `ask` verdict into a recorded question. Returns it, or None.

    None means "nothing to answer": the call was allowed, denied, or is the
    same question already put to him inside the window. `now` and `window` are
    parameters rather than clock reads so this is testable without sleeping.

    Writes to `asks_path` only. No rule, no message, no HTTP -- wiring lives
    with the caller.
    """
    if _decision_of(verdict) != "ask":
        return None

    # `tool_subject` is total: a non-dict input or a tool shipped in a future
    # release degrades to a string rather than raising on the hot path.
    subject = tool_subject(tool_name, tool_input)
    path = Path(asks_path)

    if asking.suppressed(path, tool=tool_name, subject=subject, cwd=cwd,
                         window=window, now=now):
        return None

    return asking.record(
        path,
        agent=_who(agent, session_id),
        tool=str(tool_name),
        subject=subject,
        cwd=str(cwd),
        ts=now,
    )


def handoff_verdict(tool_name: str, tool_input: object) -> bool:
    """Whether `evaluate` would have asked because of the secure-handoff floor.

    Exposed for a caller that wants the flag before a row exists (the endpoint
    knows the tool call, not the Ask). It goes through the same `tool_subject`
    the row is built from, so the answer cannot differ from `is_handoff_ask` on
    the row that call produces.
    """
    return asking.is_handoff_subject(tool_subject(tool_name, tool_input))
