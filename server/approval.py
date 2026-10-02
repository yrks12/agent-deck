"""Put every session the deck hires under the approval hook -- and only those.

**The fault.** `hooks/cc-approve.js` was registered in no settings scope on this
machine. `~/.claude/settings.json` carries `cc-bus.js` on seven events and
`cc-office.js` on three; the approver appears in none of them, and
`settings.local.json` has no hooks at all. Everything downstream of it --
`server/autoreview.py`, `POST /api/approve`, the ask ledger, the approval cards
-- was real code that no real session ever reached. The earlier proof run passed
an explicit `--settings` file on the command line, which showed the hook is
obeyed when installed, not that it was installed.

**Why not `~/.claude/settings.json`.** That file is read by every Claude Code
session on this Mac. Registering there would put the owner's own terminals
behind the deck's rule engine, which nobody asked for, and would make every tool
call in them wait on the daemon -- up to the hook's 2s budget, on every call,
whenever the deck is down. The deck governs the agents it hired. It does not
reconfigure the machine.

**The mechanism.** `--settings <path>` on the argv `server/spawn.py` already
builds for `--append-system-prompt`. Measured against Claude Code 2.1.252, not
reasoned about:

* `--settings` is its own settings scope, which the CLI calls `flagSettings`. It
  is classified `"operator"` origin alongside `userSettings` and `policySettings`
  (`projectSettings`/`localSettings` are `"repo"`), and it survives
  `--setting-sources ''`, which drops user, project and local.
* The workspace-trust gate never reaches it. The gated source list in the binary
  is literally `[["projectSettings", ".claude/settings.json"],
  ["localSettings", ".claude/settings.local.json"]]` and nothing else, evaluated
  against a predicate that reads `.hooks` off each scope. A hook registered here
  is honoured in a folder the CLI has never seen.
* It MERGES with the owner's hooks rather than replacing them. A session started
  with this file wrote both an `approval` line to the deck's ledger and the
  ordinary `cc-bus.js` `UserPromptSubmit`/`Stop`/`SessionEnd` lines.

The A/B/control transcript is in `docs/prove-the-hook.md`.

**Generated, not checked in.** Both paths in the hook command are specific to
the machine and to the checkout: this repo has twenty-odd worktrees and any of
them can be the daemon. Deriving the hook path from `__file__` means the hook
that runs always belongs to the daemon that vouched for it, and resolving node
at generation time means the next `nvm install` is picked up on the next hire
instead of breaking every hired session. The four hooks already in
`~/.claude/settings.json` all hard-code `.../node/v22.17.0/bin/node`; this does
not.

**Fail open, loudly.** No resolvable node means no `--settings` and no flag: the
desk starts and prompts normally, which is stock Claude Code behaviour, rather
than paying a failed process spawn on every tool call. That refusal is written
to the same ledger `pretrust` and `hire` write to, because a hire nobody governs
must not look identical to one that is governed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import atomic
from . import deckauth
from . import deskperms
from .paths import BUS_DIR

#: The hooks this file registers, in the checkout that is generating the file.
HOOKS_DIR: Path = Path(__file__).resolve().parent.parent / "hooks"
HOOK_PATH: Path = HOOKS_DIR / "cc-approve.js"
COMPACT_HOOK: Path = HOOKS_DIR / "cc-compact.js"
#: The prompt-time hook. `PreToolUse` alone cannot keep a hired desk off a
#: modal, because the word it returns to stay safe -- "ask" -- is the word that
#: draws one. See `hooks/cc-permission.js` for the measurement.
PERMISSION_HOOK: Path = HOOKS_DIR / "cc-permission.js"
#: The MCP-elicitation hook. Same class as the one above -- Claude Code's own
#: description of the event is "Fired when an MCP server requests user input.
#: Hooks can auto-respond (accept/decline) instead of showing the dialog" -- and
#: the deck registered nothing for it, so those dialogs were drawn on desks with
#: nobody at them. See `hooks/cc-elicit.js` for the measured contract.
ELICIT_HOOK: Path = HOOKS_DIR / "cc-elicit.js"
#: The mail hook -- the ONLY reader of the office queue. Every door that writes
#: to a desk (`Surface.send`, `app._deliver_routine`, `groups.broadcast`,
#: `harvest._tell_the_boss`, `api._resume_desk`) queues through `office.send`
#: first and only then tries the socket, so a desk with nobody at it keeps the
#: record. This hook is what picks that record up on the desk's next turn.
#:
#: MEASURED on the box on 2026-09-06, before this was registered:
#:
#:   ~/.claude/settings.json                      0 matches for cc-office
#:   ~/.claude/settings.local.json                does not exist
#:   .../agent-bus/approve-settings.json          PreToolUse, PermissionRequest,
#:                                                Elicitation, PostCompact
#:
#: So on that machine nothing read the queue, ever. Every message that missed a
#: socket was appended to a file with no reader, the desk sat IDLE, and the
#: owner was told `delivered: false` -- which `docs/client-api.md` defines as
#: "queued, not lost". It was lost. The Mac hid this because the owner's own
#: `~/.claude/settings.json` carries the hook machine-wide, which governs his
#: terminals, not the desks the deck hires.
OFFICE_HOOK: Path = HOOKS_DIR / "cc-office.js"
#: The mirror: each Bash command a desk runs, and its output, shown in the
#: read-only `agent` terminal of that desk's computer. Bash only, before and
#: after. See `hooks/cc-mirror.js` and server/terminal_stream.py.
MIRROR_HOOK: Path = HOOKS_DIR / "cc-mirror.js"
MIRROR_MATCHER = "Bash"

#: The events `hooks/cc-office.js` delivers mail on, and the only two it is
#: registered here for. Deliberately NOT `PreToolUse`: the hook's other half is
#: a collision guard that can BLOCK a write, and that belongs to the machine's
#: own settings where the owner opted into it -- a hired desk should not start
#: refusing its own edits because the deck installed a mail reader.
MAIL_EVENTS: tuple[str, ...] = ("UserPromptSubmit", "SessionStart")

#: Where the generated file lives: the deck's own directory, next to
#: `autoreview.json` and `events.jsonl`, and honouring `CLAUDE_CONFIG_DIR` so a
#: test never lands on the real one.
SETTINGS_PATH: Path = BUS_DIR / "approve-settings.json"
EVENTS_PATH: Path = BUS_DIR / "events.jsonl"

#: Claude Code's own timeout for the hook. The hook's internal guard is 2s, so
#: it always answers first and this never fires -- it is the backstop for a node
#: that cannot even start.
HOOK_TIMEOUT = 5

#: The compaction event, MEASURED against 2.1.252 rather than read off the
#: hook's own comment -- both events were registered and `/compact` run for real:
#:
#:   PreCompact  fires with {trigger, custom_instructions}, and fires even when
#:               the CLI goes on to REFUSE the compaction ("Not enough messages
#:               to compact"). A detection layer there invents compactions.
#:   PostCompact fires only when one actually happened, with {trigger,
#:               compact_summary}. Not dispatched inside a subagent context.
#:
#: So `PostCompact` is the event that means what the board needs it to mean.
COMPACT_EVENT = "PostCompact"

#: The deck's own address, when nothing else says otherwise. `bin/cdash` reads
#: AGENT_DECK_PORT; the LaunchAgent sets no environment at all and puts
#: `--port 7788` in ProgramArguments, which is why argv is consulted too.
DEFAULT_PORT = 7788

#: `PreToolUse` only. The tools whose subject a rule can meaningfully match
#: (`server/autoreview.py::_SUBJECT_FIELDS`). Deliberately not every tool: this
#: hook runs inside EVERY tool call in the session, so a matcher this narrow
#: means a bug in the approver can never stop a session thinking, searching or
#: answering, and never costs a Glob 2s while the deck is down.
MATCHER = "Bash|Read|Write|Edit|NotebookEdit|WebFetch"

#: `PermissionRequest`. Every tool, and the asymmetry with `MATCHER` above is
#: the point.
#:
#: MEASURED on 2.1.252 in an interactive pty, with one probe hook registered at
#: `matcher: "*"` and the evidence read from the hook's own JSONL log rather
#: than from the screen:
#:
#:   Bash                                             -> fires
#:   mcp__claude_ai_Google_Calendar__list_calendars   -> fires
#:   AskUserQuestion                                  -> fires
#:
#: (Also measured: it does not fire in `-p` headless mode at all. A headless run
#: produced a real `permission_denials` entry with the hook log empty, so the
#: pty is the only rig this can be established in.)
#:
#: So the narrow matcher was leaving exactly the hole the door was built to
#: close: a hired desk prompting on an MCP tool still sat on a modal that
#: nothing outside the process can answer.
#:
#: Why widening is safe here and not above. `PermissionRequest` fires ONLY
#: where a modal was already about to be drawn -- the session had already
#: stopped. The worst a bug in a wide matcher can do is turn a stall into a
#: denial, and a denial is a sentence the agent reads and routes around.
#:
#: The real cost of widening is the one the previous measurement flagged: a
#: denial the owner cannot interpret. That is paid for in
#: `autoreview.tool_subject` and `asking.describe_tool`, not by narrowing this
#: back -- the ask he receives names the server and the operation in words.
PERMISSION_MATCHER = "*"

#: Searched in order after `DECK_NODE` and `PATH`. The daemon runs from a
#: LaunchAgent with a minimal environment, so `PATH` alone is not enough.
NVM_ROOT: Path = Path.home() / ".nvm" / "versions" / "node"
FALLBACK_NODES: tuple[str, ...] = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


@dataclass(frozen=True)
class Install:
    """The outcome, in the shape `pretrust.Trust`, `hire.HireError` and
    `spawn.SpawnError` already use: a stable slug every caller reads the same
    way. `path` is empty exactly when `ok` is False."""

    ok: bool
    reason: str
    path: str = ""
    node: str = ""
    detail: str = ""


# ── finding node ───────────────────────────────────────────────────────────


def _runnable(candidate: str) -> bool:
    try:
        return os.path.isfile(candidate) and os.access(candidate, os.X_OK)
    except OSError:
        return False


def _version_key(name: str) -> tuple[int, int, int]:
    """`v22.17.0` -> (22, 17, 0). An unparseable directory name sorts last
    rather than raising -- `~/.nvm/versions/node/` is not ours to validate."""
    parts = (name.lstrip("vV").split(".") + ["", "", ""])[:3]
    return tuple(int(p) if p.isdigit() else -1 for p in parts)  # type: ignore[return-value]


def _nvm_nodes() -> list[str]:
    """Every nvm-installed node, newest first. Newest rather than "the one that
    was there when this was written": that pin is the bug."""
    try:
        entries = [e for e in NVM_ROOT.iterdir() if e.is_dir()]
    except OSError:
        return []
    entries.sort(key=lambda e: _version_key(e.name), reverse=True)
    return [str(e / "bin" / "node") for e in entries]


def resolve_node() -> str:
    """An absolute path to a runnable node, or "" if this machine has none.

    Order: an explicit `DECK_NODE`, then `PATH`, then nvm newest-first, then the
    usual system locations. Never a literal version -- a hard-coded nvm path
    breaks silently on the next node upgrade, and a hook whose command cannot
    start is a hook that costs every tool call a failed spawn.
    """
    override = os.environ.get("DECK_NODE", "").strip()
    candidates = ([override] if override else []) + \
        ([shutil.which("node")] if shutil.which("node") else []) + \
        _nvm_nodes() + list(FALLBACK_NODES)
    for candidate in candidates:
        if candidate and _runnable(candidate):
            return candidate
    return ""


# ── the deck's own address ─────────────────────────────────────────────────


def deck_url() -> str:
    """Where a session hired by THIS daemon should report back to.

    The hole this closes: both hooks fell back to a hard-coded
    `http://127.0.0.1:7788`, read off whatever environment the Terminal window
    happened to inherit. A deck on any other port therefore hired desks whose
    approvals and compactions landed on somebody else's daemon, or on nothing.
    The address is now stated in the settings file rather than guessed by the
    hook.

    Order, and each entry earns its place on this machine:

    1. `DECK_URL` -- an operator who has already said where the deck is.
    2. `AGENT_DECK_PORT` -- what `bin/cdash` and `bin/deck` honour.
    3. `--host` / `--port` on this process's own argv -- the LaunchAgent sets
       no environment at all and puts `--host 127.0.0.1 --port 7788` in
       ProgramArguments, so this is the branch that covers production.
    4. `DEFAULT_HOST`/`DEFAULT_PORT`.

    **The host used to be hard-coded to loopback, and on the box that was
    wrong.** MEASURED on the box on 2026-09-02, with the deck already live:
    the unit runs `uvicorn --host <wireguard address> --port 7789`, so the deck binds the
    WireGuard address and *only* that -- `curl http://127.0.0.1:7789/api/state`
    on the box is `Failed to connect`. This function nevertheless returned
    `http://127.0.0.1:7789`, and `hooks/cc-approve.js` handed that URL answered
    `"permissionDecisionReason":"deck unreachable"` while the same payload at
    `http://<wireguard address>:7789` answered `"no rule"`. Every approval, permission
    request, elicitation and compaction from a desk that box hired went into a
    closed socket, and the desk sat on a modal nobody could clear.

    Reading `--host` is not the exfiltration risk the old comment guarded
    against: the only value that can appear here is an address this very
    process bound, which is by definition local. A wildcard bind is the one
    exception -- `0.0.0.0` and `::` name every interface rather than a place to
    post to -- so those fall back to loopback instead.
    """
    stated = (os.environ.get("DECK_URL") or "").strip()
    if stated:
        return stated
    host = _host()
    if ":" in host:                     # a bare IPv6 literal needs brackets
        host = f"[{host}]"
    return f"http://{host}:{_port()}"


#: Not addresses: "every interface" and "no interface". A hook cannot post to
#: either, so they resolve to the machine the hook is running on.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*", ""})
DEFAULT_HOST = "127.0.0.1"


def _host() -> str:
    argv = list(sys.argv)
    if "--host" in argv:
        index = argv.index("--host") + 1
        candidate = argv[index].strip() if len(argv) > index else ""
        if candidate not in WILDCARD_HOSTS:
            return candidate
    return DEFAULT_HOST


def _port() -> int:
    from_env = (os.environ.get("AGENT_DECK_PORT") or "").strip()
    if from_env.isdigit():
        return int(from_env)
    argv = list(sys.argv)
    if "--port" in argv:
        candidate = argv[argv.index("--port") + 1] if len(argv) > argv.index("--port") + 1 else ""
        if candidate.isdigit():
            return int(candidate)
    return DEFAULT_PORT


# ── the document ───────────────────────────────────────────────────────────


def settings_document(node: str, hook: Path | str = HOOK_PATH,
                      compact_hook: Path | str = COMPACT_HOOK,
                      permission_hook: Path | str = PERMISSION_HOOK,
                      elicit_hook: Path | str = ELICIT_HOOK,
                      office_hook: Path | str = OFFICE_HOOK,
                      mirror_hook: Path | str = MIRROR_HOOK) -> dict:
    """The `--settings` file's contents. PURE -- writes nothing.

    Five hooks, because a hired desk needs five things said about it: what it
    is allowed to do (`PreToolUse`), what happens when the answer to that was
    "ask the human" (`PermissionRequest`), what happens when an MCP server puts
    a question to it directly (`Elicitation`), when it forgot who it was
    (`COMPACT_EVENT`), and -- the one added last and the one the owner felt --
    that somebody spoke to it while it was not listening (`MAIL_EVENTS`).

    The mail hook is not decoration on top of the socket; it is the half that
    makes `delivered: false` mean what the API says it means. See `OFFICE_HOOK`
    for the measurement: on the Linux box it was registered in no scope at all,
    so the office queue every door writes to had no reader and a desk nobody
    was sitting at swallowed the owner's messages in silence.

    `PermissionRequest` is not a nicety on top of `PreToolUse`; it is the half
    that makes the pair honest. MEASURED on 2.1.252 in an interactive pty: a
    `PreToolUse` "ask" draws a modal that only a human at that window can
    clear, and neither the deck's authed socket nor `--permission-mode dontAsk`
    could clear it. `PermissionRequest` takes only allow or deny, so a hired
    desk under both hooks has no path to a stall.

    A WIDER matcher than `PreToolUse`, and see `PERMISSION_MATCHER` for the
    measurement that settled it. The two hooks answer different questions: the
    first runs in every tool call and must stay cheap and narrow, the second
    runs only where the session was already about to stop.

    Every path is quoted inside its command string because a hook runs through
    a shell: an unquoted `/Users/x/My Projects/...` would split into two words.
    """
    def command(script: Path | str) -> dict:
        return {"type": "command", "command": f'"{node}" "{script}"',
                "timeout": HOOK_TIMEOUT}

    return {
        # OWNER RULING 2026-09-30: every desk runs with all permission checks
        # bypassed -- `permissions.defaultMode: "bypassPermissions"`, MEASURED
        # honoured from flag settings on claude 2.1.285. No classifier, no ask
        # floor. The hooks below still raise the owner's card for a hand-off
        # (hook asks outrank bypass). See `server/deskperms.py` for the
        # measurement and what it means. Per-hire and operator-scoped: nothing
        # here changes any session the deck did not hire.
        **deskperms.document(),
        # Claude Code applies this to every hook process it starts for the
        # session, so the hooks stop guessing. Stated rather than inherited.
        #
        # The PATH to the token, never the token. `/api` requires a credential
        # now, so the hooks need one, and there were two ways to give it to
        # them. This file is the wrong one to put a secret in for two separate
        # reasons: it lives in the bus beside siblings that are world-readable
        # (`asks.json`, `events.jsonl`, `messages.jsonl` are all 0644 on this
        # machine), and it is written once per hire -- so rotating the token
        # would strand every desk hired before the rotation, silently, as "the
        # approver stopped deciding".
        #
        # `deckauth.TOKEN_PATH` is 0600 and read fresh on each hook call, which
        # costs 0.0075 ms and survives a rotation. What the desk is told is
        # where to look.
        "env": {"DECK_URL": deck_url(),
                "DECK_TOKEN_FILE": str(deckauth.TOKEN_PATH)},
        "hooks": {
            "PreToolUse": [
                {"matcher": MATCHER, "hooks": [command(hook)]},
                {"matcher": MIRROR_MATCHER, "hooks": [command(mirror_hook)]},
            ],
            "PostToolUse": [
                {"matcher": MIRROR_MATCHER, "hooks": [command(mirror_hook)]},
            ],
            "PermissionRequest": [
                {"matcher": PERMISSION_MATCHER,
                 "hooks": [command(permission_hook)]},
            ],
            # No narrow matcher is possible here: MEASURED on 2.1.252, the
            # Elicitation matcher is applied to `matchQuery: serverName` -- the
            # MCP server's name, not a tool name -- so a tool list would match
            # nothing at all.
            "Elicitation": [
                {"matcher": "*", "hooks": [command(elicit_hook)]},
            ],
            # No matcher: the matcher for a compaction event is its `trigger`,
            # and both "manual" and "auto" are compactions worth recording.
            COMPACT_EVENT: [
                {"hooks": [command(compact_hook)]},
            ],
            # No matcher on either: neither event has a subject to match on --
            # `UserPromptSubmit` carries the prompt, `SessionStart` carries the
            # source -- and mail is due on every one of them regardless.
            #
            # Both, not one. `SessionStart` is what reaches a desk that was
            # spoken to while it was down and has only just come up;
            # `UserPromptSubmit` is what reaches one that has been sitting
            # there. A desk that got only the first would go deaf for the rest
            # of its life, which is the shape of the bug being fixed.
            **{event: [{"hooks": [command(office_hook)]}]
               for event in MAIL_EVENTS},
        },
    }


def install(*, hook: Path | str = HOOK_PATH,
            compact_hook: Path | str = COMPACT_HOOK,
            permission_hook: Path | str = PERMISSION_HOOK,
            elicit_hook: Path | str = ELICIT_HOOK,
            office_hook: Path | str = OFFICE_HOOK) -> Install:
    """Write the deck's settings file and report where it landed.

    Never raises. A hire must not fail because the approver could not be
    installed -- the caller's job on refusal is to spawn anyway and say so.

    Both hook scripts must exist before anything is written. A settings file
    naming a script that is not there registers a hook whose every invocation
    is a failed spawn, which is strictly worse than registering nothing.
    """
    node = resolve_node()
    if not node:
        return _log(Install(False, "no_node", detail=(
            "no runnable node found via DECK_NODE, PATH, ~/.nvm or the system "
            "paths; the hired session will prompt normally")))
    missing = [str(s) for s in (hook, compact_hook, permission_hook,
                                elicit_hook, office_hook)
               if not Path(s).is_file()]
    if missing:
        return _log(Install(False, "no_hook", node=node,
                            detail=f"not files in this checkout: {missing}"))
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(
            SETTINGS_PATH,
            json.dumps(settings_document(node, hook, compact_hook,
                                         permission_hook, elicit_hook,
                                         office_hook),
                       indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        return _log(Install(False, "write_failed", node=node,
                            detail=f"{type(exc).__name__}: {exc}"))
    return _log(Install(True, "installed", path=str(SETTINGS_PATH), node=node))


def _log(result: Install) -> Install:
    """One line in the ledger `hooks/cc-bus.js`, `hire()` and `pretrust()`
    already write. A silent refusal is what let the unregistered hook survive a
    whole build cycle, so the refusal is recorded as loudly as the success.
    Never raises: an unwritable ledger costs an audit line, not a hire."""
    line = {"ts": time.time(), "event": "approve_install", "ok": result.ok,
            "reason": result.reason, "path": result.path, "node": result.node,
            "detail": result.detail}
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
    return result
