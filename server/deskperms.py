"""Desks run with EVERY permission check bypassed.

**OWNER RULING, 2026-09-30, verbatim** -- after Claude Code's auto-mode
classifier blocked atlas from changing acme.example's DNS in GoDaddy
("auto mode classifier. Reason: [Unauthorized Persistence]"): *"this has to be
fixed! we should allow it anything"*. And, the same day: *"any type of
permission it should has or ask for approval, this is not helping if its says
im block"*.

**What it means.** A hired desk can do anything the box user can do, including
irreversible things: delete files, push, change DNS, write crontabs, read
credentials. There is no classifier and no `permissions.ask` floor. It is
deliberate, it is the owner's call, and it is what this module states instead
of the old auto-mode design (classifier + `ALWAYS_ASK` + `autoMode`
environment), which is gone.

**Mechanism, MEASURED on the box (claude 2.1.285), 2026-09-30.** Under
`claude -p` the settings `defaultMode` alone was honoured, but a desk is a
`--bg` session and there it is NOT: a throwaway desk with only the settings
stayed `permissionMode: "default"` and still raised approval cards. Three
things together make a `--bg` desk run in bypass, each verified by removing it:

1. argv `--permission-mode bypassPermissions` (`spawn.build_argv`);
2. `skipDangerousModePermissionPrompt: true` in the per-hire `--settings`
   (`document()` below) -- without it the flag is ignored;
3. `bypassPermissionsModeAccepted: true` in `~/.claude.json`
   (`pretrust.accept_bypass`, called from `spawn._vouch`) -- without it `--bg`
   refuses: "requires accepting the disclaimer first".

`--dangerously-skip-permissions` stays in `spawn.BANNED_FLAGS` (same mode, but
the sweep is the guard against a process-wide switch). The per-hire settings
still never touch the owner's own terminals.

**Also MEASURED, and why the floor had to be removed rather than outranked:**
in bypass mode a `permissions.ask` rule STILL stops the call, and so does a
hook's `permissionDecision: "ask"`. That is what keeps the hand-off working:
`autoreview.HANDOFF_PATTERNS` (a PreToolUse hook ask) and the PermissionRequest
hook still raise the owner's card. A login, 2FA, captcha or payment page is
needing his hands, not a permission, and is untouched by this ruling.

**Fourth requirement, found by the live probe:** in bypass mode Claude Code
HOLDS inbound peer messages unless `crossSessionInbound: "accept"` is set -- the
deck's own messages to the desk included -- so `document()` states it.

**No dead ends.** Nothing may end in prose saying "I am blocked". Whatever can
still stop a desk must be cleared by the desk, or be a card the owner can tap:
an approval card, a "Take over the screen" handoff, or an `ask` decision. The
brief says so (`hire.NEVER_BLOCKED`).

**A resumed desk keeps its old flags** (docs/wake.md): a session started
before this change stays in auto mode until it is FRESH-started.
"""

from __future__ import annotations

from dataclasses import dataclass

#: MEASURED, see the module docstring: flag settings honour this.
PERMISSION_MODE = "bypassPermissions"


@dataclass(frozen=True)
class Checkpoint:
    """One thing he is always asked about. `rule` goes to Claude Code verbatim."""

    rule: str
    why: str


#: EMPTY ON PURPOSE (owner ruling 2026-09-30). MEASURED: a `permissions.ask`
#: rule still stops a session in bypass mode, so any row here would be a wall.
ALWAYS_ASK: tuple[Checkpoint, ...] = ()

#: ── THE ONE TOOL A DESK MAY NOT USE ────────────────────────────────────────
#:
#: `CronCreate` schedules inside one Claude session: "Session-only (not written
#: to disk, dies when Claude exits). Auto-expires after 7 days." MEASURED on
#: the box, 2026-09-28: atlas scheduled the owner's daily report with it, the
#: deck's Routines panel stayed empty, and a restart would have dropped it.
#: This is not risky work being refused -- it is the wrong door, and the brief
#: (`hire.SCHEDULES`) names the right one: a `YOS_ROUTINE` line. `CronDelete`
#: and `CronList` stay allowed so a desk can clear out a job it already made.
DENY: tuple[str, ...] = ("CronCreate",)


#: ── THE ONE ALLOW RULE ─────────────────────────────────────────────────────
#:
#: The deck's own MCP server (`server/deck_mcp.py`: say, ask, message_desk).
#: MEASURED on the box, 2026-09-30, a fresh desk under auto mode: its first
#: `mcp__deck__say` and `mcp__deck__ask` each raised an approval card for the
#: owner -- auto mode does not clear a desk's own MCP tools. A desk asking him
#: whether it may say "on it" is the stall those tools exist to remove, and
#: none of them can do more than post into the deck's own queue. By server
#: name, and only this server: the desk's computer tools stay with the
#: classifier and the approver.
ALLOW: tuple[str, ...] = ("mcp__deck",)


def ask_rules() -> list[str]:
    """The `permissions.ask` array for a hired desk: always empty."""
    return [checkpoint.rule for checkpoint in ALWAYS_ASK]


def document() -> dict:
    """The `permissions` block for the generated `--settings`.

    PURE -- writes nothing, reads nothing. `server/approval.py` merges this into
    the file it already generates, so there is exactly one config writer.

    `defaultMode` is `bypassPermissions`; there is no `autoMode` block because
    there is no classifier. `permissions.deny` holds only `DENY`: `CronCreate`
    is the wrong door (session-only), not risky work, and the brief names the
    right one.
    """
    return {
        "skipDangerousModePermissionPrompt": True,
        # MEASURED on the box, 2026-09-30: a session in bypass mode HOLDS every
        # inbound peer message ("The sender did not attest its permission mode
        # and this session bypasses prompts") -- the owner's messages, injected
        # by the deck, arrived parked and were never read. "An explicit value
        # always wins", so it is stated.
        "crossSessionInbound": "accept",
        "permissions": {
            "defaultMode": PERMISSION_MODE,
            "allow": list(ALLOW),
            "ask": ask_rules(),
            "deny": list(DENY),
        },
    }
