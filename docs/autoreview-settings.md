# Registering the Auto Review hook

**The deck installs this for the sessions it hires. You do not need to do
anything.** `server/approval.py` generates
`~/.claude/agent-bus/approve-settings.json` and `server/spawn.py` hands it to
each new desk as `--settings`, so every session started by
`POST /v1/agents/interview` (or the `+` button, or `spawn_background`) runs
under the approver from its first tool call. Proven end to end in
`docs/prove-the-hook.md`.

Nothing in this repo edits `~/.claude/settings.json`, and nothing should. That
distinction is the design: **the deck governs the agents it hired; your own
terminals stay yours.** Node is resolved (`DECK_NODE`, then `PATH`, then nvm
newest-first, then the system paths) rather than pinned, so the next
`nvm install` does not silently unhook every hire.

The rest of this page is the *machine-wide* install — the thing the deck
deliberately does not do. Read it as the road not taken.

## Read this before you paste it

`PreToolUse` hooks are global to the machine. Installing this makes Agent Deck
the approver for **every** Claude Code session you run from now on — every tab,
every subagent, every background agent, including the session you paste it from.
Every tool call in all of them will consult `POST /api/approve` on the daemon
before it runs. A rule file that is wrong, or a rule that is broader than you
meant, applies everywhere at once and takes effect on the very next tool call —
there is no per-project opt-in and no staged rollout.

The failure direction is safe by construction: if the daemon is down, hung,
unreachable or answering nonsense, the hook prints `ask` and you get the ordinary
permission prompt you would have got anyway (`tests/test_approve_hook.py` pins
this). The hook can therefore only ever *reduce* prompts you have written a rule
for; it can never grant something no rule covered. What it cannot protect you
from is a rule you wrote too broadly, so review
`~/.claude/agent-bus/autoreview.json` before installing, not after.

Note also what this does **not** do: it only affects sessions started *under* the
hook. It cannot answer a prompt on a session that is already open — the session
socket has no frame for that (spec M1). It prevents prompts; it does not dismiss
them.

## The block

Merge into the `hooks` object of `~/.claude/settings.json`. If a `PreToolUse`
array already exists, append this entry to it rather than replacing the array.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/Users/samcarter/.nvm/versions/node/v22.17.0/bin/node\" \"/Users/samcarter/Projects/claude-dashbaord/hooks/cc-approve.js\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The absolute node path matches how `cc-bus.js` is already registered: a hook runs
with a minimal environment and cannot rely on `nvm`'s shims being on `PATH`.
It is also why the four hooks currently in `~/.claude/settings.json` all break
on the next node upgrade — the deck's generated file resolves instead of pinning,
and is regenerated on every spawn.

No `matcher` key means every tool. To start narrow — the recommended way in — set
a matcher and widen it later once you trust the rules:

```json
"matcher": "Bash|Read"
```

`timeout` is 5s against the hook's own 2s budget, so the hook always answers
first and Claude Code's timeout never fires. `DECK_URL` overrides the daemon
origin (default `http://127.0.0.1:7788`); the tests use it to point at a stub.

## Checking it without installing it

The hook is a plain process with a stdin/stdout contract, so you can exercise it
directly:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"git status"},"session_id":"s","cwd":"/x"}' \
  | node hooks/cc-approve.js
```

With the daemon running and a matching `always_allow` rule you get
`"permissionDecision":"allow"`. With it stopped you get `"ask"`.

## Backing it out

Delete the entry from `PreToolUse` and save. Hook config is read per session, so
open sessions keep the old behaviour until they restart. Emptying
`~/.claude/agent-bus/autoreview.json` is the faster kill switch: no rules means
every call falls through to `ask`, which is stock Claude Code behaviour.
