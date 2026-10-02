#!/usr/bin/env bash
#
# Shaliach -- from nothing to a running deck, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/yrks12/shaliach/main/install.sh | bash
#
# With options (everything after `--` goes to the installer):
#
#   ... | bash -s -- --ssh-tunnel                   # nothing public but SSH
#   ... | bash -s -- --domain deck.example.com      # your own domain, real certificate
#   ... | bash -s -- --tls=self-signed              # no public IPv4
#   ... | bash -s -- --no-docker                    # skip the per-agent desktops
#
# Read it first if you prefer:  curl -fsSLO <the url above>; less install.sh; bash install.sh
#
# What it does:
#   Ubuntu 24.04+ / Debian 12+  (a server): installs git, clones Shaliach to
#       /opt/agent-deck, runs deploy/install-deck.sh (which installs everything
#       else and proves it answers), then prints how to connect the app and,
#       last, how to sign Claude in.
#   macOS (your laptop): clones to ~/agent-deck, makes a Python environment,
#       and runs the deck on 127.0.0.1 as a LaunchAgent.
#
# Run it again to upgrade: the checkout is fast-forwarded to the latest code and
# the installer re-run; your token, login and paired devices are kept. A
# checkout with local edits is never overwritten -- it stops and names them.
#
# Settings (environment):
#   AGENT_DECK_REPO      owner/repo on GitHub (the one place the repository is named)
#   AGENT_DECK_REPO_URL  a full clone URL instead (a mirror, a fork, file://...)
#   AGENT_DECK_REF       branch or tag (default main)
#   AGENT_DECK_DIR       where the code goes
#   AGENT_DECK_PORT      laptop mode: the port (default 7788)

set -euo pipefail

AGENT_DECK_REPO="${AGENT_DECK_REPO:-yrks12/shaliach}"

main() {
  local slug="$AGENT_DECK_REPO"
  local ref="${AGENT_DECK_REF:-main}"
  local url="${AGENT_DECK_REPO_URL:-https://github.com/${slug}.git}"
  local raw="https://raw.githubusercontent.com/${slug}/${ref}"
  local os_release="${AGENT_DECK_OS_RELEASE:-/etc/os-release}"
  local kernel dir
  local -a pass=()
  local tunnel=0 user_skip_login=0 token_file=0 assume_yes=0

  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) usage; return 0 ;;
      --ssh-tunnel|--tunnel) tunnel=1; pass+=(--tls=none) ;;
      --domain) [ -n "${2:-}" ] || die "--domain needs a hostname"; pass+=(--tls=domain --hostname "$2"); shift ;;
      --domain=*) pass+=(--tls=domain --hostname "${1#*=}") ;;
      --dir) [ -n "${2:-}" ] || die "--dir needs a path"; AGENT_DECK_DIR="$2"; shift ;;
      --dir=*) AGENT_DECK_DIR="${1#*=}" ;;
      --ref) [ -n "${2:-}" ] || die "--ref needs a branch or tag"; ref="$2"; raw="https://raw.githubusercontent.com/${slug}/${ref}"; shift ;;
      --skip-login) user_skip_login=1 ;;
      --claude-token-file|--claude-token-file=*) token_file=1; pass+=("$1") ;;
      --yes|-y) assume_yes=1 ;;
      *) pass+=("$1") ;;
    esac
    shift
  done

  kernel="$(uname -s)"
  case "$kernel" in
    Linux) ;;
    Darwin) laptop "$url" "$ref" "$raw"; return ;;
    *) die "this is ${kernel}. Shaliach installs on Ubuntu 24.04+, Debian 12+, or macOS (laptop mode)." ;;
  esac

  # ── a server: refuse what will not work, before anything changes ──────────
  [ -r "$os_release" ] || die "cannot read ${os_release}; Shaliach installs on Ubuntu 24.04+ or Debian 12+"
  local os_id os_ver major
  os_id="$(. "$os_release" && printf '%s' "${ID:-}")"
  os_ver="$(. "$os_release" && printf '%s' "${VERSION_ID:-}")"
  major="${os_ver%%.*}"
  case "$os_id" in
    ubuntu) [ "${major:-0}" -ge 24 ] 2>/dev/null \
              || die "Ubuntu ${os_ver} ships Python older than 3.11, which Shaliach needs. Use Ubuntu 24.04." ;;
    debian) [ "${major:-0}" -ge 12 ] 2>/dev/null \
              || die "Debian ${os_ver} ships Python older than 3.11, which Shaliach needs. Use Debian 12 or Ubuntu 24.04." ;;
    *) die "this is ${os_id:-an unknown Linux}. Shaliach installs on Ubuntu 24.04+ or Debian 12+." ;;
  esac
  case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) die "unsupported CPU $(uname -m); Shaliach runs on x86_64 and arm64" ;;
  esac
  if [ "$(id -u)" != "0" ]; then
    command -v sudo >/dev/null 2>&1 || die "run me as root, or install sudo"
    SUDO=sudo
  fi

  dir="${AGENT_DECK_DIR:-/opt/agent-deck}"

  # ── git and the basics (only when missing, so a re-run is quick) ──────────
  if ! dpkg -s git ca-certificates curl python3 python3-venv >/dev/null 2>&1; then
    say "installing git, curl and python3"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      git ca-certificates curl python3 python3-venv >/dev/null
  fi

  checkout "$url" "$ref" "$dir" as_root

  # ── the installer does the rest, and proves it ────────────────────────────
  # Claude's sign-in is left for the very end (below), so the deck is up and
  # answering before anyone is asked to open a link.
  local tty=0
  if { : </dev/tty; } 2>/dev/null; then tty=1; fi
  pass+=(--skip-login)
  [ "$assume_yes" = 0 ] && [ "$tty" = 1 ] || pass+=(--yes)
  [ "$tty" = 1 ] || say "no terminal to ask on: installing with --yes"
  say "running ${dir}/deploy/install-deck.sh"
  # Its output is kept (and shown as it comes) so the pairing code it prints can
  # be reused below instead of minting a second one.
  local out
  out="$(mktemp)"
  trap "rm -f '${out}'" EXIT
  if [ "$tty" = 1 ]; then
    as_root "${dir}/deploy/install-deck.sh" "${pass[@]}" </dev/tty 2>&1 | tee "$out"
  else
    as_root "${dir}/deploy/install-deck.sh" "${pass[@]}" </dev/null 2>&1 | tee "$out"
  fi

  # ── how to connect, then the one step that needs a person ────────────────
  local ctl="${dir}/bin/deckctl" host port user oauth home code
  host="$(as_root "$ctl" config get network.hostname 2>/dev/null || true)"
  port="$(as_root "$ctl" config get deck.port 2>/dev/null || true)"; port="${port:-7789}"
  user="$(as_root "$ctl" config get deck.user 2>/dev/null || true)"
  oauth="$(as_root "$ctl" config get claude.oauth_env 2>/dev/null || true)"
  home="$(getent passwd "${user:-agentdeck}" 2>/dev/null | cut -d: -f6 || true)"

  echo
  say "────────────────────────────────────────────────────────────────"
  if [ -n "$host" ] && [ "$tunnel" = 0 ]; then
    code="$(grep -oE 'ADK1\.[A-Za-z0-9_-]+' "$out" | tail -n 1 || true)"
    [ -n "$code" ] || code="$(as_root "$ctl" pair --json 2>/dev/null \
      | sed -n 's/.*"code": *"\([^"]*\)".*/\1/p' | head -n 1)"
    say "Shaliach is serving on https://${host}"
    if [ -n "$code" ]; then
      say "Connect the Mac app (installs it and fills in this one-time code, good for 15 minutes):"
      say "  curl -fsSL ${raw}/scripts/install-mac.sh | bash -s -- --pair ${code}"
      say "iPhone: paste the same code under Settings -> Deck (a code works once: for a"
      say "  second device run  sudo deckctl pair  again)."
    else
      say "Pairing code: run  sudo deckctl pair  and paste it into the app."
    fi
  else
    say "Shaliach is serving on 127.0.0.1:${port} on this server (nothing public but SSH)."
    say "From your Mac, open the tunnel and install the app:"
    say "  ssh -N -L ${port}:127.0.0.1:${port} root@<this server>"
    say "  curl -fsSL ${raw}/scripts/install-mac.sh | bash"
    say "Then in the app: Connect -> address http://127.0.0.1:${port}, and the token from:"
    say "  sudo sed -n 's/^AGENT_DECK_TOKEN=//p' /etc/agent-deck/agentdeck.env"
  fi

  if as_root grep -qs '^CLAUDE_CODE_OAUTH_TOKEN=.' "${oauth:-/nonexistent}" \
      || as_root test -s "${home:-/nonexistent}/.claude/.credentials.json"; then
    say "Claude is signed in. You are done."
  elif [ "$tty" = 1 ] && [ "$user_skip_login" = 0 ] && [ "$token_file" = 0 ]; then
    say "Last step: sign Claude in. Open the link below on any device, approve, and"
    say "paste back what it shows you (needs a Claude subscription)."
    if as_root "$ctl" login </dev/tty; then
      say "Claude is signed in. You are done."
    else
      say "Sign-in did not finish. Later:  sudo deckctl login"
    fi
  else
    say "Last step: sign Claude in. This prints a link to open on any device:"
    say "  sudo deckctl login"
  fi
}

SUDO=""
as_root() { if [ -n "$SUDO" ]; then sudo "$@"; else "$@"; fi; }
as_user() { "$@"; }
say() { printf 'agent-deck: %s\n' "$*"; }
die() { printf 'agent-deck: %s\n' "$*" >&2; exit 1; }

usage() {
  local self="${BASH_SOURCE[0]:-}"
  [ -f "$self" ] && sed -n '3,33p' "$self" | sed 's/^# \{0,1\}//' \
    || say "usage: curl -fsSL <url>/install.sh | bash -s -- [--ssh-tunnel | --domain HOST] [installer flags]"
}

# checkout <url> <ref> <dir> <runner>: clone, or fast-forward a clean checkout.
checkout() {
  local url="$1" ref="$2" dir="$3" run="$4" dirty old new
  if [ -d "${dir}/.git" ]; then
    dirty="$("$run" git -C "$dir" status --porcelain --untracked-files=no)"
    [ -z "$dirty" ] || die "${dir} has local changes, so I will not overwrite it:
${dirty}
Commit or discard them (git -C ${dir} stash), then run me again."
    old="$("$run" git -C "$dir" rev-parse HEAD)"
    "$run" git -C "$dir" fetch -q --depth 1 "$url" "$ref" \
      || die "could not fetch ${ref} from ${url}"
    "$run" git -C "$dir" reset -q --hard FETCH_HEAD
    new="$("$run" git -C "$dir" rev-parse HEAD)"
    if [ "$old" = "$new" ]; then
      say "${dir} is already at the latest ${ref} (${new:0:12})"
    else
      say "upgraded ${dir}: ${old:0:12} -> ${new:0:12}"
    fi
  elif [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    die "${dir} exists and is not a Shaliach checkout. Move it, or set AGENT_DECK_DIR."
  else
    say "downloading Shaliach (${ref}) to ${dir}"
    "$run" mkdir -p "$(dirname "$dir")"
    "$run" git clone -q --depth 1 --branch "$ref" "$url" "$dir" \
      || die "could not clone ${url}"
  fi
}

# ── laptop mode: macOS, loopback only, your own Claude login ────────────────
laptop() {
  local url="$1" ref="$2" raw="$3"
  local dir="${AGENT_DECK_DIR:-$HOME/agent-deck}" port="${AGENT_DECK_PORT:-7788}"
  local label="dev.agentdeck.server" uid tok plist logf py tmp wait
  uid="$(id -u)"
  tok="$HOME/.claude/agent-bus/deck-token.txt"
  plist="$HOME/Library/LaunchAgents/${label}.plist"
  logf="$HOME/Library/Logs/agent-deck.log"

  command -v git >/dev/null 2>&1 \
    || die "git is missing. Run  xcode-select --install , then run me again."
  checkout "$url" "$ref" "$dir" as_user

  # Python 3.11+: uv if present, else a new-enough python3. macOS's own is 3.9.
  if command -v uv >/dev/null 2>&1; then
    [ -x "${dir}/.venv/bin/python" ] || (cd "$dir" && uv venv -q --python 3.13 .venv)
    (cd "$dir" && uv pip install -q --python .venv/bin/python -r requirements.txt)
  else
    py="$(command -v python3 || true)"
    [ -n "$py" ] && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
      || die "Shaliach needs Python 3.11+. Install uv (brew install uv) or python@3.13, then run me again."
    [ -x "${dir}/.venv/bin/python" ] || "$py" -m venv "${dir}/.venv"
    "${dir}/.venv/bin/python" -m pip install -q -r "${dir}/requirements.txt"
  fi
  command -v node >/dev/null 2>&1 || say "  NOTE  node is missing; the session hooks need it:  brew install node"

  # One token for this deck, made once and never printed.
  mkdir -p "$(dirname "$tok")" "$(dirname "$plist")" "$(dirname "$logf")"
  [ -s "$tok" ] || (umask 077; openssl rand -hex 32 > "$tok")
  chmod 0600 "$tok"

  tmp="$(mktemp)"
  cat > "$tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>AGENT_DECK_TOKEN="\$(cat "\$HOME/.claude/agent-bus/deck-token.txt")" exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port ${port}</string>
  </array>
  <key>WorkingDirectory</key><string>${dir}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${logf}</string>
  <key>StandardErrorPath</key><string>${logf}</string>
</dict>
</plist>
PLIST
  if launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
    if cmp -s "$tmp" "$plist"; then
      launchctl kickstart -k "gui/${uid}/${label}"
    else
      mv "$tmp" "$plist"
      launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
      launchctl bootstrap "gui/${uid}" "$plist"
    fi
  else
    # Not ours yet: refuse a port someone else already answers on.
    if curl -fsS -m 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
      rm -f "$tmp"
      die "something already answers on 127.0.0.1:${port}. Set AGENT_DECK_PORT=<another port> and run me again."
    fi
    mv "$tmp" "$plist"
    launchctl bootstrap "gui/${uid}" "$plist"
  fi
  rm -f "$tmp"

  wait="${AGENT_DECK_HEALTH_WAIT:-30}"
  until curl -fsS -m 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; do
    wait=$((wait - 1))
    [ "$wait" -gt 0 ] || die "the deck did not answer on 127.0.0.1:${port}. Its log: ${logf}"
    sleep 1
  done

  echo
  say "────────────────────────────────────────────────────────────────"
  say "Shaliach is running on http://127.0.0.1:${port} (this Mac only; starts at login)."
  say "Web board:  open \"http://127.0.0.1:${port}/?t=\$(cat ~/.claude/agent-bus/deck-token.txt)\""
  say "Mac app:    curl -fsSL ${raw}/scripts/install-mac.sh | bash"
  say "            then Connect -> address http://127.0.0.1:${port}, token: pbcopy < ~/.claude/agent-bus/deck-token.txt"
  if command -v claude >/dev/null 2>&1; then
    say "Claude:     agents use the claude login on this Mac. Not signed in yet? Run  claude  once."
  else
    say "Claude:     install Claude Code (https://docs.claude.com/en/docs/claude-code), then run  claude  once to sign in."
  fi
}

# Everything above is a definition. Nothing runs until this last line, so a
# download cut off half-way (curl | bash) executes nothing at all.
main "$@"
