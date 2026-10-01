"""Incremental transcript reader.

These files reach 8+ MB and grow while we watch them, so the one rule here is:
never re-read bytes we have already parsed. Each tail keeps a byte offset and an
accumulated snapshot; a poll reads only the delta and stops at the last complete
newline so a half-written record is picked up on the next tick instead of being
dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import comms

# Transcripts log the same message 2-3x (streaming deltas, final, replays).
# Token totals are therefore deduped by message.id -- the same correction the
# existing ~/.claude/statusline-with-tokens.sh makes with `unique_by(.id)`.


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        return {
            "input": self.total_input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass
class Activity:
    tool: str = ""
    detail: str = ""
    ts: str = ""
    running: bool = False


@dataclass
class TranscriptState:
    git_branch: str = ""
    model: str = ""
    version: str = ""
    usage: Usage = field(default_factory=Usage)
    activity: Activity = field(default_factory=Activity)
    last_prompt: str = ""
    last_assistant: str = ""
    # The card shows the clipped copy; speech needs the whole reply and its id.
    last_assistant_full: str = ""
    last_assistant_id: str = ""
    last_ts: str = ""
    turns: int = 0
    exists: bool = False


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _epoch(ts) -> float:
    """Transcript timestamps are ISO-8601 Zulu; edges want seconds."""
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _tool_detail(name: str, params: dict) -> str:
    """One short human line describing what a tool call is actually doing."""
    if not isinstance(params, dict):
        return ""
    for key in ("description", "query", "prompt", "pattern", "command", "subject"):
        if params.get(key):
            return _clip(params[key], 90)
    for key in ("file_path", "path", "notebook_path", "url"):
        if params.get(key):
            value = str(params[key])
            return os.path.basename(value) or value
    return ""


class TranscriptTail:
    """Byte-offset tail over one .jsonl transcript."""

    def __init__(self, path: Path, *, sidechain: bool = False,
                 owner: str = "", sink=None) -> None:
        self.path = path
        # Subagent transcripts are entirely isSidechain=true; the parent's is not
        # and must drop sidechain records so subagent work is not counted twice.
        self.sidechain = sidechain
        # `owner` labels which session these records belong to and `sink` is
        # where cross-session messages go. Both optional: the tail stays usable
        # on its own.
        self.owner = owner
        self.sink = sink
        self._offset = 0
        self._inode: int | None = None
        self._seen_message_ids: set[str] = set()
        self._pending_tool_ids: set[str] = set()
        self.state = TranscriptState()

    def _reset(self) -> None:
        self._offset = 0
        self._seen_message_ids.clear()
        self._pending_tool_ids.clear()
        self.state = TranscriptState()

    def poll(self) -> TranscriptState:
        try:
            stat = self.path.stat()
        except OSError:
            self.state.exists = False
            return self.state

        self.state.exists = True

        # Rotation / truncation / a resumed session writing a fresh file.
        if self._inode is not None and (stat.st_ino != self._inode or stat.st_size < self._offset):
            self._reset()
        self._inode = stat.st_ino

        if stat.st_size <= self._offset:
            return self.state

        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(stat.st_size - self._offset)
        except OSError:
            return self.state

        cut = chunk.rfind(b"\n")
        if cut == -1:
            # No complete record yet; leave the offset so we retry these bytes.
            return self.state
        self._offset += cut + 1

        for line in chunk[:cut].split(b"\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                self._apply(record)

        return self.state

    # -- record handling ---------------------------------------------------

    def _apply(self, record: dict) -> None:
        kind = record.get("type")

        if kind == "last-prompt":
            prompt = record.get("lastPrompt")
            if isinstance(prompt, str) and prompt.strip():
                self.state.last_prompt = _clip(prompt, 220)
            return

        # Subagent turns live in their own transcripts; counting them here would
        # double-report the parent's activity.
        if bool(record.get("isSidechain")) is not self.sidechain:
            return

        for src, dst in (("gitBranch", "git_branch"), ("version", "version")):
            value = record.get(src)
            if isinstance(value, str) and value:
                setattr(self.state, dst, value)

        ts = record.get("timestamp")
        if isinstance(ts, str) and ts:
            self.state.last_ts = ts

        if kind == "user":
            self._apply_user(record, ts)
        elif kind == "assistant":
            self._apply_assistant(record, ts)

    def _emit(self, record: dict) -> None:
        """Hand a cross-session message to the sink. Never fail the poll."""
        if self.sink is None:
            return
        try:
            self.sink(record)
        except Exception:
            pass

    def _emit_inbound(self, text: str, ts) -> None:
        """A message from another session, if that is what this text is."""
        inbound = comms.parse_inbound(text)
        if inbound is None:
            return
        self._emit({
            "dir": "in",
            "owner": self.owner,
            "ts": _epoch(ts),
            "from_sock": inbound["from_sock"],
            "from_name": inbound["from_name"],
            "text": inbound["body"],
        })

    def _apply_user(self, record: dict, ts) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")

        if isinstance(content, str):
            if content.strip():
                # Received cross-session messages arrive as a plain string body,
                # not a block list -- missing this loses inbound traffic whole.
                self._emit_inbound(content, ts)
                self.state.last_prompt = _clip(content, 220)
                self.state.turns += 1
            return

        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id in self._pending_tool_ids:
                    self._pending_tool_ids.discard(tool_use_id)
                    if not self._pending_tool_ids:
                        self.state.activity.running = False
            elif btype == "text" and block.get("text", "").strip():
                self._emit_inbound(block["text"], ts)
                self.state.last_prompt = _clip(block["text"], 220)
                self.state.turns += 1

    def _apply_assistant(self, record: dict, ts) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return

        model = message.get("model")
        if isinstance(model, str) and model:
            self.state.model = model

        self._add_usage(message)

        content = message.get("content")
        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                self.state.activity = Activity(
                    tool=name,
                    detail=_tool_detail(name, block.get("input") or {}),
                    ts=ts if isinstance(ts, str) else "",
                    running=True,
                )
                tool_use_id = block.get("id")
                if tool_use_id:
                    self._pending_tool_ids.add(tool_use_id)
                if name == "SendMessage":
                    params = block.get("input") or {}
                    self._emit({
                        "dir": "out",
                        "owner": self.owner,
                        "ts": _epoch(ts),
                        "to_label": str(params.get("to") or ""),
                        "text": str(params.get("message") or ""),
                    })
            elif block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    self.state.last_assistant = _clip(text, 220)
                    self.state.last_assistant_full = _clip(text, 4000)
                    self.state.last_assistant_id = str(message.get("id") or "")

    def _add_usage(self, message: dict) -> None:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        message_id = message.get("id")
        if not message_id:
            return
        if message_id in self._seen_message_ids:
            return
        self._seen_message_ids.add(message_id)

        def num(key: str) -> int:
            value = usage.get(key)
            return value if isinstance(value, int) else 0

        self.state.usage.input += num("input_tokens")
        self.state.usage.output += num("output_tokens")
        self.state.usage.cache_read += num("cache_read_input_tokens")
        self.state.usage.cache_write += num("cache_creation_input_tokens")
