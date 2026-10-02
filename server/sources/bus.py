"""Hook event bus.

`claude agents --json` only reports idle | busy | shell -- it cannot say
"blocked on a permission prompt". The Notification hook can, so hooks/cc-bus.js
appends one JSON line per event to ~/.claude/agent-bus/events.jsonl and this
module folds that stream into per-session attention state.

There is no "notification dismissed" event, so clearing is a state machine:
anything that proves the session moved on (a new prompt, a completed tool, a
flip to busy) clears the flag.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..paths import BUS_FILE

# Notification types that mean the session is genuinely BLOCKED on the owner.
#
# `idle_prompt` is deliberately excluded. Claude Code fires it after ~60s of
# sitting at the prompt, so every idle session eventually raises one -- treating
# it as attention turns the alert band into noise and makes "needs you" mean
# nothing. A plain idle session is IDLE; only a real block is NEEDS_YOU.
ATTENTION_TYPES = {"permission_prompt", "agent_needs_input"}

# Fires when a turn ends and Claude is waiting; drives DONE, not the alert band.
IDLE_TYPES = {"idle_prompt"}

# Events that prove the session is no longer blocked.
CLEARING_EVENTS = {"UserPromptSubmit", "PostToolUse", "PreToolUse"}


@dataclass
class Attention:
    kind: str
    message: str
    since: float


@dataclass
class SessionSignals:
    attention: Attention | None = None
    done_at: float = 0.0  # last Stop / agent_completed
    last_event_at: float = 0.0
    ended: bool = False
    subagents: dict[str, bool] = field(default_factory=dict)  # agent_id -> running


class BusReader:
    """Byte-offset tail over the hook event log."""

    def __init__(self) -> None:
        self._offset = 0
        self._inode: int | None = None
        self.sessions: dict[str, SessionSignals] = {}

    def _signals(self, session_id: str) -> SessionSignals:
        sig = self.sessions.get(session_id)
        if sig is None:
            sig = SessionSignals()
            self.sessions[session_id] = sig
        return sig

    def poll(self) -> dict[str, SessionSignals]:
        try:
            stat = BUS_FILE.stat()
        except OSError:
            return self.sessions

        # cc-bus.js truncates the log past 5 MB; treat that as a fresh file.
        if self._inode is not None and (stat.st_ino != self._inode or stat.st_size < self._offset):
            self._offset = 0
        self._inode = stat.st_ino

        if stat.st_size <= self._offset:
            return self.sessions

        try:
            with BUS_FILE.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(stat.st_size - self._offset)
        except OSError:
            return self.sessions

        cut = chunk.rfind(b"\n")
        if cut == -1:
            return self.sessions
        self._offset += cut + 1

        for line in chunk[:cut].split(b"\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                self._apply(event)

        return self.sessions

    def _apply(self, event: dict) -> None:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return

        sig = self._signals(session_id)
        kind = str(event.get("event") or "")
        ts = event.get("ts")
        sig.last_event_at = float(ts) if isinstance(ts, (int, float)) else time.time()

        if kind == "Notification":
            n_type = str(event.get("notification_type") or "")
            if n_type in ATTENTION_TYPES:
                sig.attention = Attention(
                    kind=n_type,
                    message=str(event.get("message") or ""),
                    since=sig.last_event_at,
                )
            elif n_type in IDLE_TYPES or n_type == "agent_completed":
                sig.done_at = sig.last_event_at
            return

        if kind in CLEARING_EVENTS:
            sig.attention = None
            if kind == "UserPromptSubmit":
                sig.done_at = 0.0
            return

        if kind == "Stop":
            sig.done_at = sig.last_event_at
            return

        if kind == "SubagentStart":
            agent_id = event.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                sig.subagents[agent_id] = True
            return

        if kind == "SubagentStop":
            agent_id = event.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                sig.subagents[agent_id] = False
            return

        if kind == "SessionEnd":
            sig.ended = True
            sig.attention = None
            return

    def clear_attention(self, session_id: str) -> None:
        """Called when polled status flips to busy -- the session moved on."""
        sig = self.sessions.get(session_id)
        if sig is not None:
            sig.attention = None
