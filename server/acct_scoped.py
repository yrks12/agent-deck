"""What a desk leaves behind in its old Claude account when the mover moves it.

The conversation moves (mover.py); claude.ai things do not. An artifact, its
ArtifactData rows and its assets belong to the account that published it, and
so do the user MCP servers in that account's `.claude.json`.

MEASURED on the box 2026-10-01 (a social desk after the main -> work
failover): its dashboard write answered "no such artifact or no access"; the
artifact's perm record named main as owner; read as work it was a 404; and
main could not grant work access -- PATCH /api/frame/perm answered 403 "this
org type may only set read.mode=owner" (two personal plans, two orgs). So the
deck cannot share across accounts. What it can do is tell the desk, at the
move, which artifacts are the old account's and what to do about them.

`artifacts_touched` is PURE given the transcript: the claude.ai artifact links
the session passed to, or got back from, an Artifact tool. Text that merely
mentions a link does not count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import accounts
from .paths import slug_for

ARTIFACT_TOOLS = ("Artifact", "ArtifactData", "ArtifactComments")
_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9-]{16,40}")

HEAD = "[Agent Deck] You were moved from the {src} Claude account to {dst}."
NOTE = (" These claude.ai artifacts were made or written in {src} and stay there, so from "
        "{dst} a read or write answers \"no such artifact or no access\": {links}. "
        "{src} cannot share them with {dst}. For each one you still use: republish "
        "its page as a NEW artifact (Artifact publish with no url, same file and "
        "capabilities), load its data again from your own files or records, put the "
        "new link in your notes, memory and routines, and tell your boss the new "
        "link. Do not retry the old link.")
MCP_NOTE = (" Your user MCP servers {names} are not set up in {dst} -- tell your "
            "boss if you need them.")


def artifacts_touched(transcript: Path) -> list[str]:
    """Artifact links in this transcript's Artifact tool calls, first seen first."""
    try:
        lines = transcript.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    ours: set[str] = set()
    seen: dict[str, None] = {}
    for line in lines:
        if "artifact" not in line.lower():
            continue
        try:
            content = (json.loads(line).get("message") or {}).get("content")
        except (ValueError, AttributeError):
            continue
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ARTIFACT_TOOLS:
                ours.add(str(block.get("id")))
                found = _URL.findall(json.dumps(block.get("input")))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in ours:
                found = _URL.findall(json.dumps(block.get("content")))
            else:
                continue
            seen.update(dict.fromkeys(found))
    return list(seen)


def user_mcp(config: Path) -> set[str]:
    try:
        servers = json.loads(config.read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return set()
    return set(servers) if isinstance(servers, dict) else set()


def note_for(src: accounts.Account, dst: accounts.Account, cwd: str,
             session_id: str) -> str:
    """What the desk must hear about `src`-scoped things, or "" if nothing."""
    transcript = accounts.dirs(src).projects / slug_for(cwd) / f"{session_id}.jsonl"
    links = artifacts_touched(transcript)
    missing = sorted(user_mcp(accounts.dirs(src).global_config)
                     - user_mcp(accounts.dirs(dst).global_config))
    if not links and not missing:
        return ""
    text = HEAD.format(src=src.label, dst=dst.label)
    if links:
        text += NOTE.format(src=src.label, dst=dst.label, links=", ".join(links))
    if missing:
        text += MCP_NOTE.format(dst=dst.label, names=", ".join(missing))
    return text
