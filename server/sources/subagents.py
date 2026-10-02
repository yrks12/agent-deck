"""Subagent tracking.

Claude Code writes each spawned agent to
    <projects>/<slug>/<sessionId>/subagents/agent-<id>.jsonl
alongside an agent-<id>.meta.json carrying agentType, description and worktree.
That pair is the whole "what are my agents doing" feed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .transcript import TranscriptTail, Usage

# How long an agent transcript can sit untouched before we stop calling it live.
# Phase 3 replaces this heuristic with exact SubagentStart/Stop bus events.
STALE_AFTER_SECONDS = 45.0


@dataclass
class Subagent:
    agent_id: str
    agent_type: str
    description: str
    worktree: str
    spawn_depth: int
    running: bool
    activity_tool: str
    activity_detail: str
    last_text: str
    usage: Usage
    updated_at: float

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "description": self.description,
            "worktree": self.worktree,
            "spawn_depth": self.spawn_depth,
            "running": self.running,
            "activity": {"tool": self.activity_tool, "detail": self.activity_detail},
            "last_text": self.last_text,
            "usage": self.usage.as_dict(),
            "updated_at": self.updated_at,
        }


class SubagentWatcher:
    """Tails every agent-*.jsonl under one session's subagents/ directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._tails: dict[str, TranscriptTail] = {}
        self._meta: dict[str, dict] = {}

    def _load_meta(self, agent_id: str) -> dict:
        cached = self._meta.get(agent_id)
        if cached is not None:
            return cached
        try:
            payload = json.loads((self.directory / f"agent-{agent_id}.meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        self._meta[agent_id] = payload
        return payload

    def poll(self, *, forced_states: dict[str, bool] | None = None) -> list[Subagent]:
        """forced_states maps agent_id -> running, from authoritative bus events."""
        try:
            paths = sorted(self.directory.glob("agent-*.jsonl"))
        except OSError:
            return []

        forced_states = forced_states or {}
        now = time.time()
        out: list[Subagent] = []

        for path in paths:
            agent_id = path.stem[len("agent-") :]
            tail = self._tails.get(agent_id)
            if tail is None:
                tail = TranscriptTail(path, sidechain=True)
                self._tails[agent_id] = tail
            state = tail.poll()

            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0

            meta = self._load_meta(agent_id)
            forced = forced_states.get(agent_id)
            if forced is not None:
                running = forced
            else:
                # An agent that has stopped mid-tool never gets its tool_result,
                # so staleness has to be able to overrule a "running" tool.
                running = state.activity.running and (now - mtime) < STALE_AFTER_SECONDS

            out.append(
                Subagent(
                    agent_id=agent_id,
                    agent_type=str(meta.get("agentType") or "agent"),
                    description=str(meta.get("description") or ""),
                    worktree=str(meta.get("worktreePath") or ""),
                    spawn_depth=int(meta.get("spawnDepth") or 1),
                    running=running,
                    activity_tool=state.activity.tool,
                    activity_detail=state.activity.detail,
                    last_text=state.last_assistant,
                    usage=state.usage,
                    updated_at=mtime,
                )
            )

        # Live agents first, then most recently touched.
        out.sort(key=lambda s: (not s.running, -s.updated_at))
        return out
