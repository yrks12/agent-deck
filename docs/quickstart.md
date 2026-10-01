# Quickstart

Two ways to try Agent Deck. Pick one.

| | Where it runs | Time (measured) | Needs |
|---|---|---|---|
| **A. On your laptop** | macOS or Linux, loopback only | ~5 min | Python 3.11+, Node, Claude Code already signed in |
| **B. On a server** | a fresh Ubuntu 24.04 VPS, always on | ~10 min | root on the VPS, a Claude subscription (for `claude setup-token`) |

Both end the same way: the deck answers `/healthz`, you create an agent, you send
it a message. Replace `<REPO_URL>` below with this repository's clone URL.

Before you start, read [security.md](security.md). Agents started by the deck run
with full access to the machine they are on.

## A. On your laptop (no server, nothing exposed)

The deck binds `127.0.0.1` only. It also shows the Claude Code sessions you
already have running on this machine, because it reads `~/.claude`.

You need:

- Python 3.11 or newer. macOS ships 3.9 as `python3`, so use
  [uv](https://docs.astral.sh/uv/) (below) or `brew install python@3.13`.
- Node.js (the session hooks are small Node scripts).
- Claude Code installed, and `claude` already signed in in your terminal.

```sh
git clone <REPO_URL> agent-deck && cd agent-deck
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# One token for this deck, kept where the deck and its hooks look for it.
mkdir -p ~/.claude/agent-bus
[ -s ~/.claude/agent-bus/deck-token.txt ] || (umask 077; openssl rand -hex 32 > ~/.claude/agent-bus/deck-token.txt)
export AGENT_DECK_TOKEN="$(cat ~/.claude/agent-bus/deck-token.txt)"

.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 7788
```

Without `AGENT_DECK_TOKEN` the API answers `503 auth_not_configured` to every
`/v1` request. That is on purpose. Export the token as shown above before you start.

In a second terminal:

```sh
TOKEN="$(cat ~/.claude/agent-bus/deck-token.txt)"
curl -s http://127.0.0.1:7788/healthz                       # {"ok":true}

# Create your first agent
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -X POST http://127.0.0.1:7788/v1/agents \
  -d '{"name":"helper","engine":"claude","cwd":"/tmp","charter":"A general helper."}'

# Talk to it (this starts a Claude Code session and uses your plan or API credit)
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -X POST 'http://127.0.0.1:7788/v1/threads/direct:helper/messages' \
  -d '{"text":"Hi. In one line, what can you do?"}'
```

To open the web board, go to `http://127.0.0.1:7788/?t=<your token>`. The token is
swapped for an HttpOnly cookie and removed from the URL. The macOS app looks for
a deck at `http://127.0.0.1:7788` by default (see [The macOS app](#the-macos-app)).

The deck puts a new agent in a workspace it creates under
`~/.claude/agent-bus/workspaces/<name>`, not in the `cwd` you send. The response's
`seat` field tells you where it went.

Run one deck per user account. Two decks on the same account compete for the same
socket under `/tmp/cc-socks`.

## B. On a server (Ubuntu 24.04)

Use a VPS that does nothing else: 2 vCPU, 4 GB RAM, 40 GB disk, any provider.
The deck's service user can run anything on it (see [security.md](security.md)).

SSH in as root and run:

```sh
apt-get update && apt-get install -y git
git clone <REPO_URL> /opt/agent-deck
/opt/agent-deck/deploy/install-deck.sh
```

The installer:

1. Prints its plan (what it will install, the address you will get) and asks
   `Continue? [Y/n]`.
2. Installs Python, Node, Claude Code for a new system user `agentdeck`, Caddy,
   and Docker. Docker gives each agent its own desktop. Building that image takes
   a few minutes and about 1.5 GB. Pass `--no-docker` to skip it.
3. Signs Claude in, using `sudo deckctl login` under the hood. It prints a link.
   Open it on any device, approve, and paste the code back. Then paste the
   long-lived token (`sk-ant-oat…`) it shows you. With no browser handy, pass
   `--skip-login` and run `sudo deckctl login` later.
4. Opens only SSH and 443 in `ufw`. Keeps the deck itself on `127.0.0.1:7789`.
5. Checks every piece (`PASS`/`FAIL` lines), runs `deckctl doctor`, and prints a
   one-time pairing code.

You can safely run it again. It keeps your token and changes only what differs.

Your API token:

```sh
sudo sed -n 's/^AGENT_DECK_TOKEN=//p' /etc/agent-deck/agentdeck.env
```

Then, from your laptop (set `DECK` to the address the installer printed):

```sh
DECK=https://203-0-113-7.sslip.io
TOKEN=<the token above>
curl -s $DECK/healthz
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -X POST $DECK/v1/agents -d '{"name":"helper","engine":"claude","cwd":"/tmp","charter":"A general helper."}'
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -X POST "$DECK/v1/threads/direct:helper/messages" -d '{"text":"Hi. In one line, what can you do?"}'
```

### Choose how you reach it

None of these need a VPN.

| Option | Install with | You reach it at | Notes |
|---|---|---|---|
| **Public HTTPS (default)** | *(no flag)* | `https://a-b-c-d.sslip.io` | Real Let's Encrypt/ZeroSSL certificate, no domain needed. Caddy forwards only `/v1/*` and `/healthz`. The board and `/api` stay private. |
| **Your own domain** | `--tls=domain --hostname deck.example.com` | `https://deck.example.com` | Point an A record at the VPS first. Add `--acme-email you@example.com` if you like. |
| **SSH tunnel only** | `--tls=none` | `http://127.0.0.1:7789` via `ssh -L 7789:127.0.0.1:7789 -N root@<vps>` | Nothing public but SSH. The simplest private setup. The board works through the tunnel too. |
| **Tailscale** | `--tls=none`, then `sudo tailscale serve --bg 7789` | `https://<machine>.<tailnet>.ts.net` | Only your tailnet can reach it, but it sees the whole deck, board included. Documented, not tested by us yet. |
| **No public IPv4** | *(automatic)* or `--tls=self-signed` | `https://<its IP>` | Uses a self-signed certificate that the pairing code pins. The macOS app cannot use it until its pairing screen ships, so use the SSH tunnel or Tailscale for now. |

The same choices as commands:

```sh
/opt/agent-deck/deploy/install-deck.sh                                    # public HTTPS on sslip.io
/opt/agent-deck/deploy/install-deck.sh --tls=domain --hostname deck.example.com --acme-email you@example.com
/opt/agent-deck/deploy/install-deck.sh --tls=none                         # SSH tunnel or Tailscale
/opt/agent-deck/deploy/install-deck.sh --tls=self-signed                  # no public IPv4
/opt/agent-deck/deploy/install-deck.sh --no-docker --skip-login           # fastest try-out
```

For a non-interactive install (cloud-init, CI), pass
`--yes --claude-token-file /root/claude-token` to `install-deck.sh` (a file holding an `sk-ant-oat…`
token from `claude setup-token`). You can add `--openai-key-file /root/openai-key`
for voice calls, which are optional.

Useful afterwards:

```sh
sudo deckctl status      # one screen: running, address, certificate, login, version
sudo deckctl doctor      # every check, with a plain-English fix for each failure
sudo deckctl login       # sign Claude in again (tokens last about a year)
sudo deckctl uninstall   # remove it; keeps your data unless --purge
journalctl -u agentdeck -f
```

## The macOS app

The app is optional. It is a native client for any deck.

- **Build it yourself** (Xcode 15 or newer, macOS 14 or newer):
  `macos/Scripts/make-app-bundle.sh release` puts `Agent Deck.app` in `macos/dist/`.
  A build you made yourself opens normally.
- **Downloaded app, not notarized yet.** The first time you open it, macOS blocks it.
  Go to **System Settings → Privacy & Security**, scroll to the message about
  Agent Deck, and click **Open Anyway**. On macOS 15 and later, right-click → Open
  no longer skips the check. From Terminal, the equivalent is
  `xattr -dr com.apple.quarantine "/Applications/Agent Deck.app"`.
  A signed and notarized build needs a paid Apple Developer account. That is not
  set up yet.

Connect it under **Settings → Deck**. Set **Base URL** to your deck's address
(`http://127.0.0.1:7788` for option A, the HTTPS address or
`http://127.0.0.1:7789` through the SSH tunnel for option B). Then paste the
token under **API token → Save to Keychain**.

## What works with which agent runtime

| | Claude Code | OpenCode | Codex |
|---|---|---|---|
| Start an agent from the deck | yes | yes (macOS Terminal only) | launches only |
| Background (headless) sessions | yes | no | no |
| Approvals, permissions, hooks | yes | no | no |
| Deck tools inside the session (ask, message another agent, its own computer) | yes | no | no |
| Wake and resume | yes | no | no |
| Shows up live on the board | yes | yes | no |

Claude Code is the supported runtime. The others are partial. Pull requests are welcome.

## If something goes wrong

- `sudo deckctl doctor` names the problem and the fix, one sentence each.
- `/v1` answers `503 auth_not_configured`: the server was started without
  `AGENT_DECK_TOKEN`.
- An agent is created but never answers: Claude is not signed in on that machine.
  Run `sudo deckctl login` (server) or `claude` once in a terminal (laptop).
- "port 443 is held by …": the VPS already runs a web server. Use a fresh VPS, or
  install with `--tls=none` and choose the SSH tunnel.

### Advanced: an existing WireGuard setup

An older install that binds the deck to a WireGuard address still works.
The installer detects it and keeps it as it is (`network.mode = "wireguard"`
in `/etc/agent-deck/deck.toml`). New installs never need WireGuard.

## Measured

Measured on 2026-09-30, following these steps word for word in a clean Ubuntu 24.04
container (systemd, arm64) with `--yes --no-docker --skip-login`:

- clone: 6-7 s
- installer: 38-44 s, every install check PASS
- `/healthz` over HTTPS, first agent created, first message queued: 52-61 s from
  the start of the clone (two runs)
- `sudo deckctl login` reached Claude's sign-in link, which is where a person takes over

Option A on a Mac with warm package caches took 7 s from clone to first agent.
A real VPS will take longer, mostly for downloads and the Docker desk image.
The only step that needs you is signing Claude in (about 2 minutes). That step
and a real reply from the agent were not part of the measurement, because they
need a paid Claude account.
