"""Builds one immutable snapshot of every live Claude Code session per tick."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import manager, models, office, onboard, roster
from .paths import subagents_dir, transcript_path
from .sources import comms
from .sources.bus import BusReader
from .sources.opencode import OpencodeScanner, OpencodeSession
from .sources.sessions import RawSession, SessionScanner
from .sources.opencode_usage import OpencodeGoUsageReader
from .sources.subagents import SubagentWatcher
from .sources.usage import UsageReader, meter_enabled
from .sources.transcript import TranscriptTail

# A session whose pid vanished lingers this long so the card can fade out
# instead of popping off the grid mid-glance.
DEAD_GRACE_SECONDS = 60.0

# Derived state, highest priority first. The order here is the sort order.
STATE_ORDER = ["NEEDS_YOU", "WORKING", "DONE", "SHELL", "IDLE", "DEAD"]


@dataclass
class _Tracked:
    tail: TranscriptTail
    agents: SubagentWatcher


class Collector:
    def __init__(self) -> None:
        self.scanner = SessionScanner()
        self.opencode = OpencodeScanner()
        self.bus = BusReader()
        # `claude.usage_meter = false` (deck.toml): the meter sends nothing.
        self.usage = UsageReader(enabled=meter_enabled())
        self.opencode_usage = OpencodeGoUsageReader()
        # Cross-session traffic rides the transcript pass we already run: each
        # tail hands its messages here as it reads them.
        self.comms = comms.CommsIndex()
        # Speaks the manager's reply to whatever you last sent it, once.
        self.speaker = manager.ReplySpeaker()
        # Desks outlive the process sitting at them. Read per tick rather than
        # cached, so a roster edit shows on the board within a second without
        # restarting the daemon.
        self.roster_path = roster.DEFAULT_PATH
        self._tracked: dict[str, _Tracked] = {}
        self._gone_since: dict[str, float] = {}
        self._last_snapshot: dict[str, dict] = {}

    def _track(self, session: RawSession) -> _Tracked:
        tracked = self._tracked.get(session.session_id)
        if tracked is None:
            tracked = _Tracked(
                tail=TranscriptTail(
                    transcript_path(session.cwd, session.session_id),
                    owner=session.session_id,
                    sink=self.comms.add,
                ),
                agents=SubagentWatcher(subagents_dir(session.cwd, session.session_id)),
            )
            self._tracked[session.session_id] = tracked
        return tracked

    def tick(self) -> dict:
        now = time.time()
        signals = self.bus.poll()
        sessions = self.scanner.scan()
        opencode_sessions = self.opencode.scan()
        live_ids = {s.session_id for s in sessions}
        live_ids.update(s.session_id for s in opencode_sessions)

        cards: list[dict] = []
        for session in sessions:
            self._gone_since.pop(session.session_id, None)
            cards.append(self._build(session, signals, now))

        for session in opencode_sessions:
            self._gone_since.pop(session.session_id, None)
            cards.append(self._build_opencode(session, now))

        # Keep recently-dead sessions on the grid briefly.
        for session_id, snapshot in self._last_snapshot.items():
            if session_id in live_ids:
                continue
            first_gone = self._gone_since.setdefault(session_id, now)
            if now - first_gone > DEAD_GRACE_SECONDS:
                continue
            dead = dict(snapshot)
            dead["state"] = "DEAD"
            dead["state_since"] = first_gone
            cards.append(dead)

        for card in cards:
            if card["state"] != "DEAD":
                self._last_snapshot[card["session_id"]] = card

        # Drop trackers for sessions that are fully gone, so offsets and seen-id
        # sets don't grow without bound across a long-running daemon.
        expired = [
            sid
            for sid, first_gone in self._gone_since.items()
            if now - first_gone > DEAD_GRACE_SECONDS
        ]
        for sid in expired:
            self._gone_since.pop(sid, None)
            self._last_snapshot.pop(sid, None)
            self._tracked.pop(sid, None)

        # A session is named once, on the command line that started it, and
        # `claude` cannot rename a live one -- so a desk that has renamed
        # itself is still sitting at a process called by its old name. Rewrite
        # to the name the desk goes by NOW, here, before the card reaches
        # anything: everything below and downstream keys on `card["name"]` --
        # the sort, the mail count, `office.publish` for the office hook, the
        # desk join, `app._try_inject`'s scan and the harvester's actor. One
        # call, or all of them go dark the moment a new hire names itself.
        onboard.reseat(cards, onboard.load_aliases(
            onboard.aliases_path(self.roster_path)))

        cards.sort(key=lambda c: (STATE_ORDER.index(c["state"]), c["name"]))

        # The hooks read this board; publish before returning so a session
        # submitting a prompt right now sees the current roster.
        office.publish(cards)
        mail = office.pending_counts()
        for card in cards:
            card["mail_pending"] = mail.get(card["name"], 0) + mail.get(card["session_id"], 0)
            card["toplevel"] = office.toplevel_for(card["cwd"])
            # Live branch beats the transcript's historical gitBranch.
            card["git_branch"] = office.branch_for(card["toplevel"]) or card["git_branch"]

        # The desk join rides the snapshot, not a second endpoint: the board is
        # SSE-driven, so a desk only appears there if it is in this payload. An
        # empty roster costs one missing-file read and changes nothing.
        desks = roster.occupancy(roster.load_roster(self.roster_path), cards)

        return {
            "generated_at": now,
            "sessions": cards,
            "desks": desks,
            "totals": _totals(cards),
            "plan": _merge_plan_usage(self.usage.poll(), self.opencode_usage.poll()),
        }

    def _build(self, session: RawSession, signals: dict, now: float) -> dict:
        tracked = self._track(session)
        state = tracked.tail.poll()

        sig = signals.get(session.session_id)
        forced = dict(sig.subagents) if sig else {}
        agents = tracked.agents.poll(forced_states=forced)

        # A busy session is provably unblocked; drop any stale attention flag.
        if session.status == "busy" and sig is not None and sig.attention is not None:
            self.bus.clear_attention(session.session_id)
            sig = signals.get(session.session_id)

        usage = dict(state.usage.as_dict())
        for agent in agents:
            agent_usage = agent.usage.as_dict()
            for key in usage:
                usage[key] += agent_usage.get(key, 0)

        derived, state_since = self._derive(session, sig, now)

        # The manager's reply arrives on the tick that sees its turn end. The
        # speaker decides whether it is ours to speak; this only offers it.
        if sig is not None and sig.done_at:
            self.speaker.check(
                session.session_id,
                done_at=sig.done_at,
                text=state.last_assistant_full,
                message_id=state.last_assistant_id,
            )

        return {
            "session_id": session.session_id,
            "pid": session.pid,
            "name": session.name,
            "cwd": session.cwd,
            "project": os.path.basename(session.cwd.rstrip("/")) or session.cwd,
            "kind": session.kind,
            # Whose config dir the session runs in (server/accounts.py).
            "account": getattr(session, "account", "main"),
            "raw_status": session.status,
            "state": derived,
            "state_since": state_since,
            "started_at": session.started_at / 1000 if session.started_at else 0,
            "git_branch": state.git_branch,
            "model": models.normalize(state.model),
            "version": state.version or session.version,
            "activity": {
                "tool": state.activity.tool,
                "detail": state.activity.detail,
                "running": state.activity.running and session.status == "busy",
            },
            "attention": (
                {"kind": sig.attention.kind, "message": sig.attention.message,
                 "since": sig.attention.since}
                if sig and sig.attention
                else None
            ),
            "last_prompt": state.last_prompt,
            "last_assistant": state.last_assistant,
            "turns": state.turns,
            "usage": usage,
            "subagents": [a.as_dict() for a in agents],
            "agents_running": sum(1 for a in agents if a.running),
        }

    def _build_opencode(self, session: OpencodeSession, now: float) -> dict:
        state = "WORKING" if session.status == "busy" else "IDLE"
        state_since = session.last_log_at or (session.updated_at / 1000 if session.updated_at else now)
        usage = {
            "input": session.tokens_input + session.tokens_cache_read + session.tokens_cache_write,
            "output": session.tokens_output,
            "cache_read": session.tokens_cache_read,
            "cache_write": session.tokens_cache_write,
        }
        return {
            "session_id": session.session_id,
            "pid": session.pid,
            "name": session.name,
            "cwd": session.cwd,
            "project": os.path.basename(session.cwd.rstrip("/")) or session.cwd,
            "kind": session.kind,
            "source": "opencode",
            "raw_status": session.status,
            "state": state,
            "state_since": state_since,
            "started_at": session.started_at / 1000 if session.started_at else 0,
            "git_branch": office.branch_for(office.toplevel_for(session.cwd)),
            "model": models.normalize(session.model),
            "version": session.version,
            "activity": {
                "tool": session.agent or "opencode",
                "detail": "",
                "running": state == "WORKING",
            },
            "attention": None,
            "last_prompt": "",
            "last_assistant": "",
            "turns": 0,
            "usage": usage,
            "subagents": [],
            "agents_running": 0,
        }

    def _derive(self, session: RawSession, sig, now: float) -> tuple[str, float]:
        status_since = (
            session.status_updated_at / 1000
            if session.status_updated_at
            else session.updated_at / 1000 or now
        )

        if sig is not None and sig.attention is not None:
            return "NEEDS_YOU", sig.attention.since

        if session.status == "busy":
            return "WORKING", status_since

        if session.status == "shell":
            return "SHELL", status_since

        # DONE = finished a turn and now waiting on the owner. The hook clears
        # done_at on the next UserPromptSubmit, which is what settles the card
        # back to IDLE -- so it stays green until he actually replies.
        if sig is not None and sig.done_at:
            return "DONE", sig.done_at

        return "IDLE", status_since


def _merge_plan_usage(anthropic: dict, opencode: dict) -> dict:
    """Merge Anthropic and OpenCode Go plan usage into one meter payload.

    `available` stays True if either provider has data. `stale` is True only
    when every provider that has data is stale. Extra fields from Anthropic are
    preserved; OpenCode Go windows are appended.
    """
    plan = dict(anthropic)
    plan.pop("reason", None)
    plan["windows"] = list(anthropic.get("windows", [])) + list(opencode.get("windows", []))
    plan["available"] = bool(anthropic.get("available") or opencode.get("available"))

    reasons: dict[str, str] = {}
    if anthropic.get("reason"):
        reasons["anthropic"] = anthropic["reason"]
    if opencode.get("reason"):
        reasons["opencode"] = opencode["reason"]
    if reasons:
        plan["reasons"] = reasons

    if plan["available"]:
        stale = True
        if anthropic.get("available"):
            stale = stale and anthropic.get("stale", True)
        if opencode.get("available"):
            stale = stale and opencode.get("stale", True)
        plan["stale"] = stale
    else:
        plan["stale"] = True
        plan["reason"] = "unavailable"

    fetched_times = [
        source["fetched_at"]
        for source in (anthropic, opencode)
        if source.get("fetched_at")
    ]
    if fetched_times:
        plan["fetched_at"] = max(fetched_times)

    return plan


def _totals(cards: list[dict]) -> dict:
    live = [c for c in cards if c["state"] != "DEAD"]
    return {
        "sessions": len(live),
        "needs_you": sum(1 for c in live if c["state"] == "NEEDS_YOU"),
        "working": sum(1 for c in live if c["state"] == "WORKING"),
        "agents_running": sum(c["agents_running"] for c in live),
        "output_tokens": sum(c["usage"]["output"] for c in live),
    }
