# Desk identity and capabilities (B4)

**Code:** `server/capabilities.py` (detector + the two sections), wired in
`server/hire.py::brief`. **Tested by:** `tests/test_capabilities.py`,
`tests/test_brief_voice.py`.

Every desk's system prompt (`--append-system-prompt`, built by
`spawn.build_argv` -> `hire.brief`) now OPENS with two short sections:

1. **Who you are** (cap 1,200 chars) -- name and title from the roster, whose
   desk it is (`office.OWNER_VOICE`), who reports to it (the roster), and what
   "work like a person in an office would" means: email, calendar, documents,
   web and a real browser, code, schedules, hiring (chief only) -- with an
   office's limits: it drafts, the boss says send; it finds the purchase, the
   boss says buy. No company facts are written here; those come from the
   desk's own charter under "What you own".
2. **What you can do** (cap 1,500 chars) -- a DETECTED inventory. Only what
   was found is named:
   - claude.ai connectors connected vs. needing sign-in (`claude mcp list`,
     run in the desk's cwd);
   - the servers in the desk's own `--mcp-config` (`deck_mcp.config`):
     computer, deck, and the owner's Mac only when a `mac` server is wired
     (plus whether a Mac is paired, from `agent-bus/mac/nodes.json`);
   - WebSearch/WebFetch (Claude engine), the permission mode;
   - CLIs on PATH (`capabilities.CLIS`), `gh` marked signed-in when
     `~/.config/gh/hosts.yml` exists;
   - skills (`~/.claude/skills/**/SKILL.md`, the desk's
     `<cwd>/.claude/skills`, each enabled plugin's `skills/`) and plugins
     (`claude plugin list --json`);
   - how to grow: `claude plugin install`, `claude mcp add`, writing a
     `SKILL.md` into its skills dir, hiring a specialist (chief only) -- and
     that money, outward sends and plan changes still go up as an `ask`
     (chief) or a `say` to the boss (everyone else).

Subsets: the **chief** (no `reports_to`) gets chief-of-staff identity, its
team, hiring and `ask`. A **junior** is told its boss and routes everything
through a `say`. An **onboarding** desk (`new-hire-*`) is told it is not yet
named and gets the inventory so it can offer real options -- no chief claim,
no hiring.

## Refreshing

The inventory is rebuilt every time a brief is built, i.e. on every fresh
start (`POST /v1/agents/<name>/start`). The two subprocess answers (`claude
mcp list` ~2 s, `claude plugin list --json` ~0.3 s on the box) are cached per
cwd in `~/.claude/cache/agent-deck-capabilities.json` for
`DECK_CAPABILITIES_TTL` seconds (default 120), so one start pays once.

After signing in a connector, installing a plugin or adding an MCP server:
wait two minutes (or delete the cache file) and fresh-start the desk. A
running session keeps the brief it was spawned with -- the CLI respawns jobs
from frozen `respawnFlags` -- so a restart is the only way a desk learns.

A CLI config home with no sign-in state (no `.credentials.json`,
`mcp-needs-auth-cache.json`, `plugins/` or `history.jsonl`) is never shelled
out to: there is nothing to list, and it keeps the unit suite hermetic.
