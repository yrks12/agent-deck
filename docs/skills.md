# Skills

The Skills screen is where you browse skills and install them on your server. Claude Code calls
them *plugins*. A plugin bundles skills, slash commands, agents, hooks and MCP servers. The wire
calls them skills, and a skill's `id` is the CLI's `name@marketplace`.

> **This skill can run programs on your server as your agents do.**
>
> The app shows this line whenever a skill's `components` include `hooks` or `mcp_servers`.
> Installing a skill needs the same bearer token as hiring an agent. It adds no new capability:
> the deck runs `claude plugin …` as its own user, with its own environment, exactly as an agent
> on the box could.

Code: `server/skills.py` (the service) and `server/skills_api.py` (the router). The router is
built but **not mounted**. Slice W1 mounts it with
`app.include_router(skills_api.build_router(skills.Skills()))`.
Tests: `tests/test_skills.py` and `tests/test_skills_api.py`, which run against a fake CLI that
speaks the shapes measured below.

## API

Every route needs `Authorization: Bearer <token>`, and the token is checked before any CLI call.
A refusal looks like `{"ok": false, "reason": <slug>, "detail": <sentence>}`.

| Route | Answer |
|---|---|
| `GET /v1/skills/catalog?q=&category=&installed=true\|false&limit=50&offset=0` | `{"items":[Skill],"total":int,"refreshed_at":"<ISO-8601 Z>","stale":bool}` |
| `GET /v1/skills/{id}` | `Skill` plus `readme` (≤ 4000 chars, or null) and `components` (or null) |
| `POST /v1/skills/install` `{"id","scope":"all"\|"desk","desk"?,"accept_command"?}` | `202 {"op_id":"sk_<12hex>","state":"running"}` |
| `GET /v1/skills/ops/{op_id}` | `{"state":"running"\|"done"\|"failed","reason":null\|slug,"detail":str,"applies":"next_session"}` |
| `DELETE /v1/skills/{id}?scope=all\|desk&desk=` | `200 {"ok":true,"applies":"next_session"}` |
| `PATCH /v1/skills/{id}` `{"enabled":bool,"scope"?,"desk"?}` | `200 {"ok":true,"enabled":bool,"applies":"next_session"}` |

```
Skill = {"id","name","description","marketplace","official":bool,"category":str|null,
         "source":{"kind","url":str|null,"ref":str|null},"install_count":int|null,
         "installed":bool,"enabled":bool|null,"scope":"all"|"desk"|"synced"|"skills-dir"|null,
         "desks":[str]}
components = {"skills":[{"name","description"}],"commands":[str],"agents":[str],
              "hooks":bool,"mcp_servers":[str]}
```

- **The catalog** comes from `claude plugin list --json --available`, merged with the installed
  list and with each marketplace's `marketplace.json`, which supplies `category`. It is cached
  for 10 minutes and dropped after every change. Search is case-insensitive over name and
  description. Installed skills sort first, then by `install_count`, highest first; unknown
  counts sort last. `claude plugin marketplace update` runs in the background at most once
  every 6 hours. If a refresh fails, the last good listing is served with `"stale": true`.
- **`source.kind`** is `git-subdir`, `github`, `url`, `npm`, `path` (the plugin lives inside the
  marketplace clone; `url` is its `homepage`, or null) or **`command`**. Clients should accept
  any string here. `command` is a marketplace-declared install command, and C5 did not list it.
- **`scope`** is `all` (a user-scope install), `desk` (installed only for the agents listed in
  `desks`), `skills-dir` (`~/.claude/skills`), `synced` (claude.ai synced plugins), or null when
  the skill is not installed. `enabled` is null when it cannot be known, for example for an
  agent-only install whose settings file is unreadable.
- **`components`** are read from the installed copy (`installPath`) or from the marketplace
  clone. They are null for an external plugin that is not installed; the app then says
  "Contents are listed after install."
- **Install scope.** `all` runs `claude plugin install <id> --json -s user`. `desk` runs
  `… -s local` with `cwd` set to that agent's folder. That agent must exist and use Claude Code.
- **Confirmation.** A skill that installs by running a command declared by the marketplace
  gets `409 {"reason":"needs_confirmation","detail","command","sha256"}`. Nothing has run at
  that point. The app shows the command and re-POSTs with `"accept_command":"<sha256>"`, which
  becomes `--accept-command <sha256>`. The server re-checks the sha against the CLI before
  every accepted install; if the command or the catalog changed, the answer is 409 again with
  the new command. **The server never passes `-y`.** A command that only appears mid-install
  (a `headersHelper`) makes the op fail with `reason: needs_confirmation` and the same
  `command` / `sha256` fields.
- **One change at a time**, across processes too (a thread lock plus `flock` on
  `<CLAUDE_CONFIG_DIR>/agent-bus/skills.lock`). Each change has a 180-second limit.
  `applies: "next_session"` means each agent picks up the change the next time it starts.
- **Refusals:** `unknown_skill` 404, `unknown_desk` 404, `not_installed` 404, `unknown_op` 404,
  `already_installed` 409, `needs_confirmation` 409, `busy` 409, `bad_scope` 400, `bad_input`
  400, `install_failed` 502 (detail = the CLI's last stderr line, with secrets redacted),
  `catalog_unavailable` 502, `cli_missing` 503, `timed_out` 504. Ops still in memory are lost
  when the deck restarts.

## Measured on the box (2026-09-30)

Claude Code **2.1.286**. Every step ran with an isolated
`CLAUDE_CONFIG_DIR=/tmp/skills-probe-<ts>`, never the real `~/.claude`, and the directory was
deleted afterwards. The service itself then ran end to end against the real CLI in a second
isolated directory, which was also deleted.

1. **A new config dir has no marketplaces.** `marketplace list --json` returns `[]` and
   `list --json --available` returns `{"installed":[],"available":[]}`. After
   `marketplace add anthropics/claude-plugins-official` (3.9 s, a git clone) there are 315
   plugins. Source kinds: 164 `url`, 98 `git-subdir`, and 53 bare strings (in-clone paths).
   Keys: `pluginId, name, description, marketplaceName, source, installCount, version`.
   `category` is only in the clone's `.claude-plugin/marketplace.json`, and 14 plugins have
   none.
2. **An installed plugin drops out of `available`.** Its `installCount` is therefore only known
   from an earlier listing. The service remembers counts it has seen; otherwise it reports null.
3. **Every mutating `--json` command prints one result line on stdout:**
   `{"command":"install","outcome":"ok","plugin":…,"pluginId":…,"scope":"user","message":…}`.
   Exit status is 0 on success and 1 on failure, and failures carry `failureCode`. Seen:
   `not_found`, `not_installed`, `not_installed_at_scope`, `already_in_goal_state` (a second
   `disable`/`enable`), `command_source_refused` and `error_policy`. Installing something
   already installed exits 0 with `installedVersion`. On failure, stderr repeats the message
   as `✘ Failed to …`.
4. **`-s local` is keyed by the working directory.** It writes
   `<cwd>/.claude/settings.local.json` → `{"enabledPlugins":{"<id>":true}}` and leaves the
   repo's committed `.claude/settings.json` untouched. `installed_plugins.json` records
   `projectPath`. The file is gitignored on the box only through the user's global
   `~/.config/git/ignore` (`**/.claude/settings.local.json`). A repo without that rule would
   show it as untracked, but it never gets committed on its own. `uninstall -s local` from
   another cwd fails with `not_installed_at_scope`, and from the right cwd it leaves
   `{"enabledPlugins":{}}`.
5. **A new session loads it.** A `claude -p --output-format stream-json` session in the desk's
   cwd listed the plugin in its `init` event (`plugins`, `agents`, `slash_commands`). A session
   in another cwd did not. Changes apply to the next session, not to running ones.
6. **`enable`/`disable` without `-s` auto-detect the scope**, and `list` reports `enabled` as
   seen from the caller's cwd. That is why the service always passes `-s` with the right cwd,
   and reads an agent-only install's `enabled` from its `settings.local.json`.
7. **Confirmation shape.** For a test marketplace with
   `"source":{"source":"command","command":"true"}`, `install --json` with stdin closed and no
   `-y` exits 1. Stdout holds **human text first**, then:
   `{"command":"install","outcome":"failed","failureCode":"command_source_refused","message":…,
   "shownCommand":{"kind":"command_source","pluginId":…,"command":"true","mode":"copy",
   "catalogRevision":"sha256:…","sha256":"<64 hex>"}}`. Nothing was run. A wrong
   `--accept-command` gives the same refusal plus `"acceptCommandMatched":false`. The right one
   runs the command (here it failed with `error_policy`, because `true` prints no plugin
   directory). The service therefore takes the **last** JSON line of stdout.
8. `marketplace update` for two marketplaces took 0.9 s. `list --json --available` took
   0.4–0.8 s. An invalid `-s` is rejected before anything runs.
