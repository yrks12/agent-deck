# Connectors & Skills

The store where the owner (or a desk) finds a **connector** (an MCP server) or a
**skill** (a `SKILL.md` folder) and puts it on one desk, some desks, or all of
them. It replaces the sidebar's empty "Plugins" row.

Code: `server/connectors.py` (catalog, trust, install), `server/connectors_api.py`
(the `/v1/store/*` router), `server/connector_run.py` (the launcher that hands a
connector its key at run time). Tests: `tests/test_connectors*.py`.

## Sources (trusted by default)

| Source | What | Where it is read |
|---|---|---|
| `anthropic-skills` | Anthropic's skills, one item per skill folder | `github.com/anthropics/skills`, `.claude-plugin/marketplace.json` (`plugins[].skills[]` are `./skills/<name>` paths) plus each `SKILL.md` front matter |
| `mcp-registry` | Connectors | `https://registry.modelcontextprotocol.io/v0/servers?search=<namespace>&version=latest`, one call per trusted publisher namespace |
| `claude-plugins-official` | Anthropic's plugin directory: `kind: "plugin"` | `github.com/anthropics/claude-plugins-official`, `.claude-plugin/marketplace.json` at a pinned commit |
| `github` | Official repos a vendor ships but has not put in the registry | `api.github.com/search/repositories`, `topic:mcp-server` limited to trusted orgs (listed, not installable) |

Every `mcp-registry` item with a GitHub repository also gets `stars` and
`updated_at` from `api.github.com/repos/<owner>/<repo>`.

The catalog is cached on the server (`<bus>/connectors/catalog.json`) and
refreshed at most once a day, in the background; a failed refresh keeps serving
the last good copy with `"stale": true`.

**Unverified** items are never cached. With `trust=all` the server searches the
registry live for the query and returns everything it finds, each item marked
`unverified` unless the rules below say otherwise.

## Trust — when the badge is true

`trust` is one of:

* `official` — the publisher is on the deck's `PUBLISHERS` list **and** the item
  proves it: a registry item's namespace is exactly one of that publisher's
  namespaces (`com.microsoft`, `io.github.github`, ...) and, when the item names
  a repository, the repository's owner is one of that publisher's GitHub orgs.
  Anthropic's `anthropics/skills` items are official (publisher Anthropic).
* `verified` — a plugin listed in Anthropic's curated plugin directory and made
  by someone else (Adobe, Salesforce, ...). Anthropic's own plugins (author
  Anthropic, kept in `./plugins/` or an `anthropics` repo) are `official`.
  Nothing else is `verified`. (A registry namespace only
  proves someone owns a domain; `app.vercel.<anything>` is anyone's Vercel app,
  and `com.microsoft/esrp-oss-mcp-test` points at a personal repo. Neither is a
  badge.)
* `unverified` — everything else. Hidden unless the client asks `trust=all`.
  Installing one from the app needs `"accept_unverified": true`; a desk that
  asks for one files a tap-to-approve card instead.

`trust_note` says in one sentence why (e.g. "Published by Microsoft
(com.microsoft), source github.com/MicrosoftDocs/mcp").

## Auth

`auth` is one of:

* `none` — a remote server that answered an MCP `initialize` without
  credentials (probed on refresh), or a package with no secret variables.
* `api_key` — needs the fields in `secrets`. The values go into the box's vault
  (`vault.json`, mode 0600) under `CONNECTOR_<SLUG>_<FIELD>`, granted only to
  the desks it was installed on. They are **never** in a response, a log, the
  desk's `--mcp-config` or a command line: a stdio connector is started by
  `python -m server.connector_run`, which reads the vault and `exec`s the real
  command with the key in its environment; a remote one gets its header from a
  `headersHelper` that runs the same launcher.
* `oauth` — a remote server that answered 401 with no key. Installed through
  **Connect** (below); a plain install answers `needs_oauth`.
* `unknown` — a remote server that has not been probed yet. Install probes it
  first.

## API

Every route needs the `/v1` bearer. Refusals are `{"ok": false, "reason", "detail"}`.

```
GET  /v1/store/catalog?kind=connector|skill&q=&trust=trusted|all&limit=50&offset=0
     -> {"items":[Item], "total":int, "refreshed_at":ISO|null, "stale":bool}
GET  /v1/store/item?id=<id>
     -> Item (+ "readme": str|null for skills)
POST /v1/store/install    {"id", "desks":["atlas",...] | "all",
                           "secrets":{"FIELD":"value"}?, "accept_unverified":bool?}
     -> {"ok":true, "item":Item, "installed":[Installed], "reload":[Reload]}
POST /v1/store/update     {"id", "desks":[...] | "all"}   -> same shape as install
POST /v1/store/uninstall  {"id", "desks":[...] | "all"}
     -> {"ok":true, "removed":[{"desk","id"}], "reload":[Reload]}
GET  /v1/store/installed?desk=<name>?
     -> {"desks":[{"desk":str, "items":[Installed]}]}
POST /v1/store/refresh    -> {"ok":true, "refreshing":true}
```

```
Item = {
  "id": "mcp:com.microsoft/microsoft-learn-mcp" | "skill:anthropics/skills:pdf" | "gh:owner/repo",
  "kind": "connector" | "skill",
  "name": str, "title": str, "description": str,
  "publisher": str, "source": "mcp-registry" | "anthropic-skills" | "github",
  "repo": "https://github.com/..." | null,
  "stars": int | null, "updated_at": ISO | null,
  "trust": "official" | "verified" | "unverified", "trust_note": str,
  "version": str | null,
  "auth": "none" | "api_key" | "oauth" | "unknown",
  "secrets": [{"name": str, "label": str, "description": str, "required": bool}],
  "installable": bool, "why_not": str | null,
  "installed_on": [desk names]
}
Installed = {"id", "kind", "name", "title", "desk", "version": str|null,
             "commit": str|null, "installed_at": ISO, "trust",
             "update_available": bool}
Reload = {"desk", "action": "respawned" | "restarted" | "next_wake" | "after_turn" | "none",
          "detail": str}
```

`desks: "all"` means every desk on the roster that runs Claude Code.

Refusals: `unknown_item` 404, `unknown_desk` 404, `not_installed` 404,
`unverified` 409 (resend with `accept_unverified`), `missing_secret` 400 (detail
names the field), `needs_oauth` 409, `not_installable` 409, `fetch_failed` 502,
`bad_input` 400.

## Where an install lands

* **Skill** → `<desk cwd>/.claude/skills/<name>/`, downloaded file by file from
  the repository **at a pinned commit**, with `.deck-install.json` beside
  `SKILL.md` (`id`, `commit`, `installed_at`). Uninstall removes only a folder
  that carries that marker. Two desks that share a folder share its skills, and
  the Installed tab says so because it reads the folder, not a ledger.
* **Connector** → `<bus>/connectors/installed.json` (per desk), merged into the
  desk's `--mcp-config` by `deck_mcp.config`. Pinned: the registry `version`,
  and for an npm package `npx -y <package>@<version>`.

## Taking effect

MEASURED on the box (Claude Code 2.1.286, 2026-09-30): a desk does not re-read
its MCP config per message, and `claude --bg --resume` re-applies its saved
options. So:

* The desk's `--mcp-config` is now a **file**
  (`<CLAUDE_HOME>/deck-mcp/<desk>.json`), not inline JSON. A background job
  saves the path, and a stop + `--resume` of the same session re-reads it:
  measured, a server renamed in the file between stop and resume came back
  under its new name, same session id, and answered a real query.
* After an install, update or uninstall the deck reloads each affected desk:
  a live desk is respawned in place (`claude respawn`, same session) **once it is idle** (`after_turn`), with
  one line telling it what changed; a desk still running an old inline config
  is restarted through the ordinary start path (conversation replayed); a
  sleeping desk needs nothing — it re-reads the file on its next wake.
* New MCP tools may sit behind ToolSearch on the first turn; the reload note
  tells the desk the server's name.

## Desks can use it

Two deck tools (`server/deck_mcp.py`), always loaded:

* `store_search(query, kind?)` — the trusted catalog, with what is installed.
* `store_install(id, desks?, secrets?)` — installs on itself by default.
  Trusted items install at once (owner ruling 2026-08-27: full autonomy) and the
  desk is reloaded after its turn. An unverified item files a decision card in
  the owner's thread ("Install X on Y? Unverified: ...") and nothing is
  installed until he taps **Install**.

## OAuth: Connect

```
POST /v1/store/connect           {"id", "desks"}
     -> {"ok", "authorize_url", "state", "redirect_uri", "expires_at"}
POST /v1/store/connect/complete  {"state","code"} | {"callback_url"}
     -> the install response
GET  /v1/store/connect/status?state= -> {"state": pending|done|failed|expired, "id", "detail"}
```

1. The deck runs MCP discovery (RFC 9728 -> RFC 8414), registers itself
   (RFC 7591) and builds a PKCE S256 consent URL with `resource`.
2. The app opens `authorize_url` in his browser. **He** consents; the deck never does.
3. The provider redirects to `http://127.0.0.1:47689/callback?code&state`.
   The app listens there while he signs in and posts the URL to
   `connect/complete`. Pasting the address bar into the app works the same way.
4. Access and refresh token become `CONNECTOR_<SLUG>_ACCESS_TOKEN` /
   `_REFRESH_TOKEN` in the vault, granted to the chosen desks; `oauth.json`
   (0600) keeps only the token endpoint, client id and expiry. Desks reach
   the server with a `headersHelper`; `connector_run` refreshes a token within
   five minutes of expiry under a lock, and the deck refreshes due tokens every
   ten minutes. Another desk can be added later without signing in again.

MEASURED 2026-09-30, ten vendor servers (Notion, Linear, Vercel, Atlassian,
Stripe, Zapier, Webflow, Wix, Airtable, GitLab): all ten publish discovery and
dynamic registration and accept the loopback redirect; Notion and Zapier
refuse any non-loopback plain-http redirect, and Linear, Vercel and Airtable
refuse the deck's own address. That is why the redirect is loopback.

## Plugins

`kind: "plugin"` items install with `claude plugin install <name>@claude-plugins-official
--json -s local` in each desk's folder (measured in docs/skills.md), after
adding the directory to the CLI once. Never `-y`; a plugin that installs by
running a command is listed but not installable. Plugins are folder-scoped like
skills. The CLI keeps the pinned version; the Installed tab shows it.
