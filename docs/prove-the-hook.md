# Prove the hook — does Claude Code actually obey `permissionDecision`?

**Status: YES. Measured 2026-09-01 against Claude Code 2.1.252.** The CLI reads
`hookSpecificOutput.permissionDecision` from a `PreToolUse` hook and acts on it,
including through a hook registered with `--settings`.

This replaces the earlier version of this document, which described an
experiment nobody had run. What follows is the run.

---

## The rule that still holds

**The hook is not in `~/.claude/settings.json`, and this change did not put it
there.** A machine-wide install would put every one of the owner's working
sessions behind the deck's rule engine and make every tool call in them wait on
the daemon — up to the hook's 2s budget — whenever the deck is down.

The install is per-hire instead: `server/approval.py` generates
`~/.claude/agent-bus/approve-settings.json` and `server/spawn.py` hands it to
the session it is starting via `--settings`. The deck governs the agents it
hired. The owner's own terminals are untouched.

## Which settings scope `--settings` occupies

`flagSettings`, and it is outside the workspace-trust gate. Determined two ways.

**Read out of the binary.** The CLI's own scope enum is
`["userSettings", "projectSettings", "localSettings", "flagSettings", ...]`, and
its origin classifier reads:

```js
case "projectSettings": case "localSettings": return "repo";
case "userSettings": case "flagSettings": case "policySettings":
case "built-in": return "operator";
```

The trust gate is applied to a *list*, and the list is exactly two entries long:

```js
var m = () => {
  let {gateProject: t} = ryt(), e = CC({onIndeterminate: "tracked"});
  return [...t ? [["projectSettings", ".claude/settings.json"]] : [],
          ...e ? [["localSettings", ".claude/settings.local.json"]] : []];
};
```

The predicate that list is fed to reads `.hooks` off each scope, so this is the
hook gate, not just the permissions one. `flagSettings` never appears in it. The
console warning the CLI prints when it withholds settings says the same thing in
prose: *"Ignoring N entries from `.claude/` settings: this workspace has not been
trusted."*

**Measured.** A `--settings` hook is honoured with `--setting-sources ''`, which
drops user, project and local — so it is not any of those three. And it MERGES
rather than replaces: session `c4a4c2e8` wrote an `approval` line to the deck's
ledger *and* the ordinary `cc-bus.js` `UserPromptSubmit` / `Stop` / `SessionEnd`
lines, and `cc-bus.js` is registered only in `~/.claude/settings.json`.

---

## The A/B, with a control

Two rules, each chosen so nothing else can explain the result:

```json
{"id": "proof-allow", "kind": "always_allow", "tool": "Bash",
 "pattern": "touch *deck-hook-proof*", "cwd": "**"}
{"id": "proof-deny",  "kind": "deny",         "tool": "Read",
 "pattern": "*deck-hook-readme*",     "cwd": "**"}
```

### DENY — the half that carries the claim

`Read` on an ordinary file in `/tmp` runs today with no prompt at all. Same
prompt, same directory, same everything; the only difference is `--settings`.

**CONTROL** (no `--settings`):

```
The line, verbatim:

    the canary line is BANANAPHONE

Read succeeded — one line, no refusal.
```

**TREATMENT** (`--settings <deck file>`):

```
REFUSED

The Read tool call on `/tmp/deck-hook-readme.txt` was blocked by a permission
rule. Exact text I was given:

    deny rule proof-deny
```

`deny rule proof-deny` is a string built in `server/autoreview.py::evaluate`.
Nothing else on the machine produces it. A *refusal* of something that would
otherwise run cannot come from anywhere but the hook.

### ALLOW — with a control that makes it mean something

"No prompt appeared" proves nothing if the command was permitted anyway — which
is exactly what a first attempt showed. So both arms run with
`--setting-sources ''`, so none of the owner's own permissions can explain
either. Same cwd, same prompt.

**CONTROL:**

```
REFUSED
    touch in '.../work/deck-hook-proof' was blocked. For security, Claude Code
    may only create or modify files in the allowed working directories ...
---- artefact after CONTROL ----
ls: .../work/deck-hook-proof: No such file or directory
```

**TREATMENT:**

```
DONE
---- artefact after TREATMENT ----
-rw-r--r--@ 1 samcarter wheel 0 .../work/deck-hook-proof
```

---

## The half that matters: through `POST /v1/agents/interview`

The above is a hand-built argv. This is the product.

A daemon serving this branch, `POST /v1/agents/interview` with
`{"engine":"claude","role_hint":"proof desk"}`. It hired `new-hire-93f2b4`,
pre-trusted the workspace, and opened a real Terminal window. The process it
started:

```
claude --settings .../agent-bus/approve-settings.json --append-system-prompt You are new-hire-93f2b4 ...
```

The desk was then sent the two probes over the deck's own inject channel
(`server/manager.py`). From its transcript:

```
[TOOL_USE Bash]   {"command": "touch /tmp/deck-hook-proof; echo \"exit=$?\""}
[TOOL_RESULT is_error=False] exit=0
[TOOL_USE Read]   {"file_path": "/tmp/deck-hook-readme.txt"}
[TOOL_RESULT is_error=True]  deny rule proof-deny
```

And in the live daemon's ledger, `~/.claude/agent-bus/events.jsonl`:

```
{"event":"approval","session_id":"b4a6d118-039f-441d-b0d7-b2977e1b3618","tool":"Bash","decision":"allow","rule_id":"proof-allow"}
{"event":"approval","session_id":"b4a6d118-039f-441d-b0d7-b2977e1b3618","tool":"Read","decision":"deny","rule_id":"proof-deny"}
```

A real session id, from a desk hired through the endpoint, reaching the deck on
port 7788 and being governed by it. That is the join the whole Auto Review
design was missing.

## The second half: compaction, and which deck a hire reports to

Two holes the first pass left open, closed and proven the same way.

### The event name, measured — because this is where a hook goes quiet

`hooks/cc-compact.js` calls itself a "PostCompact hook" in its own comment. That
comment is not evidence: a hook registered on an event that never fires passes
every assertion you can write about a settings file's contents. Both events were
registered for real and `/compact` run against 2.1.252.

**`PreCompact` fires even when no compaction happens.** On a session too short
to compact, the CLI printed `Not enough messages to compact.` — and the
PreCompact log had already been written:

```
{"session_id":"f38f2944-…","hook_event_name":"PreCompact",
 "trigger":"manual","custom_instructions":null}
```

while the PostCompact log did not exist at all. A detection layer on
`PreCompact` would therefore file compactions that never happened, which is a
worse failure than the silence it replaces.

**`PostCompact` fires only when one actually happened.** On a session with six
turns of history:

```
{"session_id":"58a3d0cb-…","hook_event_name":"PostCompact",
 "trigger":"manual","compact_summary":"<analysis>\nLet me work through this
 conversation chronologically…"}
```

So `PostCompact` is what the deck registers. Also measured: `PostCompact` is not
dispatched inside a subagent context, so a subagent's compaction is invisible to
this layer. `PreCompact` has no such guard.

### The endpoint that never existed

`cc-compact.js` has posted to `POST /api/compact` since the day it was written
and **that route did not exist**. Every compaction 404'd into a hook that is
contractually obliged to swallow failures. Registering the hook without adding
the route would have moved the silence rather than ended it. The route is now
there, and `server/compaction.py::record` — tested since it was written, called
by nothing — finally has a production caller.

### The live proof, and the `DECK_URL` fix in the same run

A daemon on **7791** (not the default), hiring through
`POST /v1/agents/interview`. The file it generated names its own address, read
off its own argv:

```json
{ "env": { "DECK_URL": "http://127.0.0.1:7791" },
  "hooks": { "PreToolUse": [...cc-approve.js...],
             "PostCompact": [...cc-compact.js...] } }
```

That desk's conversation was then compacted for real. In **7791's** ledger:

```
{"ts":1788257902.36,"event":"compact",
 "session_id":"8c855322-0648-4ec8-8bf3-dc7f7a5850b9",
 "agent":"new-hire-d48c65","trigger":"manual"}
```

The discriminating half is what is *absent*. That same session wrote twenty
lifecycle lines to the **7788** ledger — because `cc-bus.js` is registered
machine-wide in `~/.claude/settings.json`, which carries no `env` block and so
falls back to the default port. And 7788 has **zero** `compact` events. Same
session, two daemons, split exactly along which config named an address. Before
this change the compaction record would have gone to 7788 and been attributed to
the wrong deck, or dropped.

### What could not be done, stated plainly

**The compaction was not triggered from inside the Terminal window the interview
door opened.** The deck's inject channel cannot do it: `manager.inject` sends a
`{"type":"user"}` frame, the CLI treats it as a message and not as a command,
and the desk answered — correctly —

```
Can't do that — `/compact` is a terminal command the owner types in their own
session; a peer message can't trigger it…
```

So the compaction was forced by resuming **that same session's own transcript**
(`claude --resume 8c855322-… --settings <the file the deck generated for it>`).
Same session id, same desk, same settings file, real compaction. What remains
unexercised is only "a Terminal-hosted process compacts the same way a resumed
one does" — same binary, same settings scope, same event. Recorded as assumed
below rather than counted as proven.

## What is measured and what is assumed

| Claim | |
|---|---|
| The CLI obeys `allow` and `deny` from a `--settings` hook | **measured**, both directions, with controls |
| `--settings` is `flagSettings`, outside the trust gate | **measured** — read out of 2.1.252 *and* survives `--setting-sources ''` |
| It merges with the owner's hooks rather than replacing them | **measured** — both ledgers, one session |
| A desk hired through `/v1/agents/interview` runs under the hook | **measured** — real session id in the live ledger |
| `PostCompact` is the event that fires only on a real compaction; `PreCompact` fires even when the CLI refuses | **measured** — both registered, `/compact` run on a short session and a long one |
| A hired desk's compaction reaches the deck and lands in its ledger | **measured** — real session id and desk name, from a desk hired through `/v1/agents/interview` |
| A hire reports to the deck that hired it, not to port 7788 | **measured** — the compaction landed on 7791 only; 7788 has zero `compact` events for the same session |
| A Terminal-hosted hire compacts the same way the resumed session did | **assumed** — the deck's inject channel cannot send a slash command, so the compaction was forced by resuming that session's own transcript |
| `PostCompact` inside a subagent | **measured, and it does not fire** — the dispatch returns early on an agent context |
| The hook still answers `ask` on any failure, inside 2s | **measured** — `tests/test_approve_install.py`, on the command string read back out of the generated file |
| The same holds for `spawn_background` (`claude --bg`) | **assumed** — the argv is built by the same function and pinned by test, but no `--bg` desk was spawned |
| Node resolves when the daemon runs from its LaunchAgent | **measured** — `com.initech.agentdeck.plist` sets no `PATH`, so launchd gives it `/usr/bin:/bin:/usr/sbin:/sbin`; under that environment `shutil.which("node")` is `None` and `resolve_node()` falls through to `~/.nvm/versions/node/v22.17.0/bin/node`. The nvm fallback is not decoration — it is the only thing that finds node in production |

## Putting it back

Everything above ran against a scratch `CLAUDE_CONFIG_DIR` except the two proof
rules, which were written into `~/.claude/agent-bus/autoreview.json` so the
*live* daemon would serve them, and restored byte-for-byte to
`{"version": 1, "rules": []}` afterwards. The hired desk was killed and its
probe files removed. One residue is deliberate and left: `~/.claude.json` has a
`hasTrustDialogAccepted` entry for the scratch workspace `pretrust` vouched for.
