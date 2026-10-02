# Security

Shaliach starts AI coding agents and gives them real work to do. Read this page
before you put it on a machine you care about. Report a vulnerability through
GitHub Security Advisories, as described in [SECURITY.md](../SECURITY.md).

## 1. Agents have full access to the machine they run on

This is the design, and it is the most important thing on this page.

- Agents the deck starts run Claude Code in `bypassPermissions` mode. To stop
  Claude Code's own first-run prompt from stalling them, the deck writes
  `bypassPermissionsModeAccepted` and a workspace-trust entry into `~/.claude.json`
  for the service user, only for workspaces it created itself.
- The deck's approval hooks can still stop a tool call and ask you, following
  the rules in each agent's permissions ("Allow once", "Always allow", "Deny").
  They are a convenience, not a sandbox. An agent can still do anything its Unix
  user can do.
- On a server, the service user `agentdeck` is in the `docker` group whenever
  desk computers are on. That makes it **root-equivalent**. **Use a VPS dedicated
  to Shaliach**, not one that also hosts your website or database.
- The optional Mac bridge lets agents on your server run jobs on your Mac. With
  "Full access" on, they can do anything your macOS account can. Turn it on only
  for a server you control. Every job is logged, shown in the menu bar, and can be
  stopped.
- Agents read and write the repositories you point them at, and use your
  credentials there (git remotes, cloud CLIs, `.env` files). Before you start an
  agent, think about what is reachable from its workspace.

## 2. The bearer token

- Every `/v1` route requires `Authorization: Bearer <token>`. The server compares
  it in constant time and fails closed: if `AGENT_DECK_TOKEN` is unset, `/v1`
  answers `503` to everything.
- On a server, the installer generates the token once and stores it in
  `/etc/agent-deck/agentdeck.env` (`0600`, root). It is never printed, never
  passed as a command-line argument, and never written into a unit file.
- The token is also copied to `~/.claude/agent-bus/deck-token.txt` (`0600`) for
  the service user, because the hooks and local tools need it. **An agent running
  as that user can read it.** Do not treat the token as a boundary between you
  and your own agents.
- Paired devices get their own tokens (`adt_…`). Only hashes are stored, and
  `sudo deckctl devices` / `sudo deckctl revoke <id>` manage them.
- Wrong guesses are rate limited per client address and logged as
  `deck-auth-fail` lines (the token itself is never logged).
- To rotate the master token: edit `AGENT_DECK_TOKEN` in
  `/etc/agent-deck/agentdeck.env`, run `sudo systemctl restart agentdeck`, and
  paste the new token into your clients. Treat any token that was pasted into a
  chat or transcript as leaked, and rotate it.

## 3. Network exposure

The deck itself always binds `127.0.0.1`. How the outside world reaches it is up
to you:

| Setup | What is exposed | Recommended for |
|---|---|---|
| Laptop (quickstart A) | nothing | trying it |
| SSH tunnel (`--tls=none`) | only SSH | the most private server setup |
| Tailscale (`--tls=none` + `tailscale serve`) | the whole deck, to your tailnet only | a team that already uses Tailscale |
| Public HTTPS (default) | `443`: only `/v1/*` and `/healthz`, through Caddy. The web board and `/api` answer 404 | using the macOS app from anywhere |

Rules we follow and you should too:

- **Never bind the deck to `0.0.0.0`**, and never port-forward `7788`/`7789` on a
  router. The API can read every agent's transcript and inject text into a live
  session. The bearer token is a second lock, not the first one.
- On a public server, `ufw` allows only SSH and 443. The installer adds the SSH
  rule before it turns the firewall on, and it never deletes rules you already
  have.
- Docker's publish default is pinned to `127.0.0.1` before Docker first starts,
  so a desk container cannot accidentally publish a port to the internet.
- Reach the web board on a server with `ssh -L 7789:127.0.0.1:7789 <server>` and
  open `http://127.0.0.1:7789/?t=<token>`.

## 4. Where secrets are stored

| What | Where | Protection |
|---|---|---|
| Deck master token | `/etc/agent-deck/agentdeck.env` (server), the environment (laptop), mirrored to `~/.claude/agent-bus/deck-token.txt` | file mode `0600` |
| Claude login | kept by the Claude CLI itself (`~/.claude/.credentials.json` of the service user); Shaliach stores no Claude token | the CLI's own file mode, service user |
| OpenAI key (optional, voice calls) | `/etc/agent-deck/agentdeck.env` | `0600`, root |
| Credential vault (secrets you hand an agent) | `~/.claude/agent-bus/vault.json` | `0600`; values are redacted from what agents and clients see |
| Login vault (browser cookies shared between desks) | `~/.claude/agent-bus/browser/login-vault/cookies.json` | `0700`/`0600`, service user |
| macOS app token | your login Keychain | Keychain |

These files are **not encrypted at rest**. They are plain files protected by Unix
permissions. Anyone with root, or with the service user's access (which includes
every agent), can read them. Signing in to a site on one desk's browser shares
that login with every desk. To keep each desk separate, set
`[desks] shared_logins = false` in `/etc/agent-deck/deck.toml`.

The plan-usage meter is on by default. It reads the Claude CLI's own login to
call an undocumented Anthropic endpoint, so the API and the apps label it
unofficial. Turn it off with `sudo deckctl config set claude.usage_meter false`;
then it sends nothing.

Redaction of secrets in transcripts and API responses matches known patterns
(`sk-…`, `ghp_…` and similar) and the values in the vault. It is not a guarantee.
A secret in an unfamiliar format can get through.

## 5. What gets changed on your machine

- **Server:** a system user `agentdeck`, `/opt/agent-deck`, `/etc/agent-deck`,
  `/var/lib/agent-deck`, the units `agentdeck.service` and `deckdoctor.timer`, a
  Caddy site (`/etc/caddy/Caddyfile`, which it refuses to overwrite if it
  belongs to anything else), `ufw` rules, a journald size cap, and a logrotate
  file. `sudo deckctl uninstall` removes the service and the edge but keeps your
  data. `--purge` removes that too.
- **Claude Code settings:** the deck does not edit your `~/.claude/settings.json`.
  Agents it starts get their hooks from a separate `--settings` file under
  `~/.claude/agent-bus/`. To make sessions you started yourself show live state,
  add `hooks/cc-bus.js` to your own settings (optional). If you do, remove it to
  undo.
- **macOS app:** a Keychain item and its preferences. Delete the app and the
  Keychain item "deck-api-token" to remove it.

## 6. Known limits

- No built-in multi-user access control. Anyone with the master token is the owner.
- The vault, login vault, browser takeover, and Mac bridge have not had an
  independent security review yet.
- `curl | bash` style installs mean trusting this repository. You can read
  `deploy/install-deck.sh` before you run it. It stops before it changes
  anything if a check fails.
