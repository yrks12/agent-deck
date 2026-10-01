#!/usr/bin/env bash
#
# Install (or re-install) EVERYTHING the always-on box needs to run Agent Deck
# and to actually hire with it. One command:
#
#   sudo /opt/agent-deck/deploy/install-deck.sh
#
# Optionally, and only when you mean it:
#
#   sudo /opt/agent-deck/deploy/install-deck.sh --with-docker
#
# In order: base tooling -> node -> the `claude` CLI -> the token -> the venv ->
# the firewall -> (docker) -> the unit -> preflight -> start -> VERIFY.
#
# The verification at the end is the point. Earlier versions installed the deck,
# saw /api/state answer, and called that a deploy -- while the box had no CLI to
# hire with and every session hook it installed posted into a closed socket. A
# step whose proof you did not see is a step that did not happen, so the last
# phase exercises each piece against the running service and prints what it got.
#
# The one thing this script cannot do is log the CLI in. `claude setup-token`
# needs the owner at a terminal. When that is the only thing missing the script
# stops before it enables anything and prints the exact command.
#
# Run it as many times as you like. Every step is written to produce the same
# state on run two as on run one: directories are made with -p, the unit is
# copied over the top rather than edited in place, nothing is ever appended to,
# and the token is generated ONCE -- on the first run only, guarded on the env
# file's absence. Regenerating it would silently lock out the Mac that already
# has it saved.
#
# It refuses rather than improvises. Ports another service on this box owns
# (`deck.reserved_ports` in deck.toml) are never taken; if the deck's own port
# is held by something else, this script stops and tells you what, instead of
# guessing. And before it enables anything it hands the measured facts to
# server/deploy_check.preflight(), which refuses a box that would come up green
# and answer nothing.
#
# WHAT IT READS. /etc/agent-deck/deck.toml (K1): the service user, the app dir,
# the bind address and port, the network mode. The units are RENDERED from it
# by deploy/render.py -- there are no static unit files any more. A box that
# was installed before deck.toml existed (a running agentdeck.service, no
# deck.toml) is migrated on the first run: its deck.toml is inferred from the
# live unit, so the rendered unit says what the running one says (K6).
#
# What it does NOT do: stop, disable or take the port of any other service.

set -euo pipefail

# ── configuration ────────────────────────────────────────────────────────────

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "${SRC_DIR}/.." && pwd)}"

ETC_DIR="/etc/agent-deck"
ENV_FILE="${ETC_DIR}/agentdeck.env"
DECK_TOML="${DECK_CONFIG:-${ETC_DIR}/deck.toml}"
UNIT_NAME="agentdeck.service"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}"

VENV_DIR="${REPO_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
HOOKS_DIR="${REPO_DIR}/hooks"

# Flags (K4; the same on deploy/install.sh, which passes them through).
# On a box that already has deck.toml, a flag that disagrees with it is refused
# by key name rather than silently winning or silently losing.
ASSUME_YES=0 FORCE_DOCKER=0 NO_DOCKER=0 SKIP_LOGIN=0 FROM_LEGACY=0
OPT_TLS="" OPT_HOSTNAME="" OPT_EMAIL="" CLAUDE_TOKEN_FILE="" OPENAI_KEY_FILE=""
need() { [ -n "${2:-}" ] || { printf 'install-deck: %s needs a value\n' "$1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --hostname) need "$1" "${2:-}"; OPT_HOSTNAME="$2"; shift ;;
    --hostname=*) OPT_HOSTNAME="${1#*=}" ;;
    --tls=*) OPT_TLS="${1#*=}" ;;
    --tls) need "$1" "${2:-}"; OPT_TLS="$2"; shift ;;
    --acme-email) need "$1" "${2:-}"; OPT_EMAIL="$2"; shift ;;
    --acme-email=*) OPT_EMAIL="${1#*=}" ;;
    --no-docker) NO_DOCKER=1 ;;
    --with-docker) FORCE_DOCKER=1 ;;
    --claude-token-file) need "$1" "${2:-}"; CLAUDE_TOKEN_FILE="$2"; shift ;;
    --claude-token-file=*) CLAUDE_TOKEN_FILE="${1#*=}" ;;
    --openai-key-file) need "$1" "${2:-}"; OPENAI_KEY_FILE="$2"; shift ;;
    --openai-key-file=*) OPENAI_KEY_FILE="${1#*=}" ;;
    --skip-login) SKIP_LOGIN=1 ;;
    --profile-from-legacy) FROM_LEGACY=1 ;;
    *) printf 'install-deck: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done
case "$OPT_TLS" in
  ""|sslip|domain|self-signed|tunnel|none) ;;
  *) printf 'install-deck: --tls must be sslip, domain, self-signed, tunnel or none\n' >&2; exit 2 ;;
esac
[ "$FORCE_DOCKER$NO_DOCKER" != "11" ] \
  || { printf 'install-deck: --with-docker and --no-docker contradict each other\n' >&2; exit 2; }

say()  { printf 'install-deck: %s\n' "$*"; }
die()  { printf 'install-deck: %s\n' "$*" >&2; exit 1; }

# put <src> <dst>  -> 0 if it changed (and was installed), 1 if already identical.
# Whole-file replace, never an edit in place, so run two cannot diverge.
put() {
  cmp -s "$1" "$2" 2>/dev/null && return 1
  install -m 0644 -o root -g root -D "$1" "$2"
}

# ── refuse before you change anything ────────────────────────────────────────

[ "$(id -u)" = "0" ] || die "run me with sudo: I write ${UNIT_DST} and ${ETC_DIR}"

# render.py runs under the system python BEFORE the venv exists, and needs
# tomllib (3.11+). Ubuntu 24.04 ships 3.12; 22.04 ships 3.10 and is refused.
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
  || die "this box's python3 is older than 3.11 (or missing). Agent Deck needs \
Python 3.11+; use Ubuntu 24.04."
RENDER=(python3 "${SRC_DIR}/render.py")

# ── deck.toml: read it, migrate a box that predates it, or write a fresh one ──

mkdir -p "$ETC_DIR"
# 0755, not 0700: deck.toml is not secret and the service user (the deck, the
# doctor) reads it. The secret file beside it stays 0600 root.
chmod 0755 "$ETC_DIR"

FRESH=0
if [ ! -f "$DECK_TOML" ]; then
  if [ -f "$UNIT_DST" ]; then
    # K6. The box runs a unit this script installed before deck.toml existed.
    # Infer the profile FROM that unit, so the rendered unit is the running one.
    HAS_DOCKER=no; command -v docker >/dev/null 2>&1 && HAS_DOCKER=yes
    HAS_WA=no; grep -qs '^DECK_WA_URL=.' "$ENV_FILE" && HAS_WA=yes
    TOML_TMP="$(mktemp)"
    "${RENDER[@]}" infer --unit "$UNIT_DST" --docker "$HAS_DOCKER" --whatsapp "$HAS_WA" \
      > "$TOML_TMP" || die "could not read ${UNIT_DST} to migrate this box; nothing was changed"
    install -m 0644 -o root -g root "$TOML_TMP" "$DECK_TOML"
    rm -f "$TOML_TMP"
    say "migrated: wrote ${DECK_TOML} from the running ${UNIT_NAME}"
    say "  NOTE  two things a unit cannot tell me -- set them if this box has them:"
    say "        deck.reserved_ports       ports other services on this box own"
    say "        doctor.public_probe_urls  public sites to check around a docker install"
  elif [ "$FROM_LEGACY" = "1" ]; then
    die "--profile-from-legacy, but there is no ${UNIT_DST} to read a profile from"
  else
    # A fresh box (K4). Where the code lives decides how it is updated: a
    # release unpacked by install.sh under /opt/agent-deck/releases/<ver> is
    # served from the `current` link; anything else is a source checkout.
    case "$(readlink -f "$REPO_DIR")" in
      /opt/agent-deck/releases/*) NEW_APP_DIR=/opt/agent-deck/current NEW_SOURCE=release ;;
      *) NEW_APP_DIR="$REPO_DIR" NEW_SOURCE=rsync ;;
    esac
    # The address other machines reach: the source address of the default
    # route. render.py uses sslip.io only when it is a public IPv4.
    PUBLIC_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)"
    NEW_DOCKER=yes; [ "$NO_DOCKER" = "0" ] || NEW_DOCKER=no
    TOML_TMP="$(mktemp)"
    "${RENDER[@]}" new --ip "$PUBLIC_IP" --tls "$OPT_TLS" --hostname "$OPT_HOSTNAME" \
      --acme-email "$OPT_EMAIL" --docker "$NEW_DOCKER" --app-dir "$NEW_APP_DIR" \
      --install-source "$NEW_SOURCE" > "$TOML_TMP" \
      || { rm -f "$TOML_TMP"; die "nothing was changed (the line above says why)"; }
    eval "$("${RENDER[@]}" shell --config "$TOML_TMP")"
    say "this is a fresh box. The plan:"
    say "  install  python venv, node, the claude CLI$([ "$DESKS_DOCKER" = 1 ] && printf ', docker')$([ "$NET_MODE" = public ] && [ "$NET_TLS" != tunnel ] && printf ', caddy')"
    say "  run as   a service user '${DECK_USER}' (created if missing)$([ "$DESKS_DOCKER" = 1 ] && printf ' -- in the docker group, which makes it ROOT-EQUIVALENT: use a dedicated box')"
    if [ "$NET_MODE" = "public" ]; then
      say "  open     port 443 (and keep ssh open); the deck itself stays on 127.0.0.1:${DECK_PORT}"
      say "  address  https://${NET_HOSTNAME}  (tls: ${NET_TLS})"
    else
      say "  address  none from outside: the deck answers on ${DECK_URL} only"
    fi
    if [ "$ASSUME_YES" = "0" ]; then
      [ -t 0 ] || { rm -f "$TOML_TMP"; die "no terminal to ask on. Re-run with --yes to accept the plan above."; }
      read -r -p "install-deck: Continue? [Y/n] " ANSWER
      case "$ANSWER" in [nN]*) rm -f "$TOML_TMP"; die "stopped at your word; nothing was installed" ;; esac
    fi
    install -m 0644 -o root -g root "$TOML_TMP" "$DECK_TOML"
    rm -f "$TOML_TMP"
    FRESH=1
    say "wrote ${DECK_TOML}"
  fi
fi

SHELL_VARS="$("${RENDER[@]}" shell --config "$DECK_TOML")" \
  || die "${DECK_TOML} is not usable (the line above says which key); nothing was changed"
eval "$SHELL_VARS"
DECK_BASE="$DECK_URL"
BUS_DIR="${DECK_HOME}/.claude/agent-bus"
CLAUDE_VERSION="${CLAUDE_VERSION:-${CLAUDE_CHANNEL}}"
CLAUDE_BIN="${DECK_HOME}/.local/bin/claude"
WITH_DOCKER="$DESKS_DOCKER"
[ "$FORCE_DOCKER" = "0" ] || WITH_DOCKER=1

# A flag that disagrees with deck.toml is refused, naming the key. deck.toml
# is the box's memory; a flag on run five must not quietly rewrite run one.
disagree() {
  die "deck.toml says $1 = $2; you passed $3. Run  deckctl config set $1 <value>  \
first (or drop the flag). Nothing was changed."
}
[ -z "$OPT_TLS" ] || [ "$OPT_TLS" = "$NET_TLS" ] || disagree network.tls "$NET_TLS" "--tls=${OPT_TLS}"
[ -z "$OPT_HOSTNAME" ] || [ "$OPT_HOSTNAME" = "$NET_HOSTNAME" ] \
  || disagree network.hostname "${NET_HOSTNAME:-(empty)}" "--hostname ${OPT_HOSTNAME}"
[ -z "$OPT_EMAIL" ] || [ "$OPT_EMAIL" = "$ACME_EMAIL" ] \
  || disagree network.acme_email "${ACME_EMAIL:-(empty)}" "--acme-email ${OPT_EMAIL}"
[ "$NO_DOCKER" = "0" ] || [ "$DESKS_DOCKER" = "0" ] || disagree desks.docker true --no-docker

# The units start ${APP_DIR}/.venv/bin/python; this script builds the venv in
# the tree it runs from. If those are two different trees the service would
# start the wrong interpreter, or none.
[ "$(readlink -f "$APP_DIR")" = "$(readlink -f "$REPO_DIR")" ] \
  || die "deck.toml says deck.app_dir = ${APP_DIR}, but I am running from ${REPO_DIR}. \
Run the installer that lives in ${APP_DIR}, or fix deck.app_dir."

# The service user. A WireGuard box predates this installer and its user is the
# owner's own: missing means deck.toml is wrong, never "create one". A public or
# local box gets a system user of its own (K4).
if ! id "$DECK_USER" >/dev/null 2>&1; then
  [ "$NET_MODE" != "wireguard" ] \
    || die "no such user: ${DECK_USER} (deck.user in ${DECK_TOML})"
  say "creating the service user ${DECK_USER} (home ${DECK_HOME})"
  useradd --system --create-home --home-dir "$DECK_HOME" --shell /bin/bash --user-group "$DECK_USER"
fi

# Run something as the service user. `claude`, its credentials and the bus
# directory all belong to that user; doing any of it as root produces a box that
# looks equipped and cannot read its own login.
as_deck() { sudo -u "$DECK_USER" env HOME="$DECK_HOME" "$@"; }

# Every rendered file, into a scratch dir; installed later only where it differs.
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
"${RENDER[@]}" render --config "$DECK_TOML" --out "$RENDERED" \
  || die "could not render the units from ${DECK_TOML}; nothing was changed"
HAS_EDGE=0; [ ! -f "${RENDERED}/Caddyfile" ] || HAS_EDGE=1

# The admin CLI (K5), when this release ships it. Linked to the `current` tree
# so an update carries it along. Relinked only when it points elsewhere.
DECKCTL=""
if [ -x "${REPO_DIR}/bin/deckctl" ]; then
  DECKCTL=/usr/local/bin/deckctl
  [ "$(readlink "$DECKCTL" 2>/dev/null)" = "${APP_DIR}/bin/deckctl" ] \
    || ln -sfn "${APP_DIR}/bin/deckctl" "$DECKCTL"
fi

# Prints WHAT is listening on $1, or nothing at all if the port is free.
#
# The systemd unit name, when there is one. `ss` reports the kernel's comm
# string, and MEASURED on this box that string is the word `python` for both of
# the services that matter:
#
#   LISTEN 0 2048 10.0.0.1:7789 0.0.0.0:* users:(("python",pid=2221429,fd=12))
#   LISTEN 0 2048 10.0.0.1:7788 0.0.0.0:* users:(("python",pid=3626971,fd=6))
#
# That broke the re-run. `deploy_check._is_our_own` looks for "agentdeck" in the
# holder, could not find it in "python", and the installer refused to recognise
# its own running service -- so the FIRST install passed (7789 was free) and
# every one after it stopped at preflight. The judge was right: a bare `python`
# could be any of four services here. The measurement was the wrong measurement.
#
# Falling back to the comm string matters as much as the systemd lookup: an
# empty answer means "the port is free" to every caller above, so a holder we
# cannot name must still be named badly rather than not at all.
port_holder() {
  local pid unit
  pid="$(ss -ltnpH "sport = :$1" 2>/dev/null \
    | sed -n 's/.*,pid=\([0-9]*\),.*/\1/p' | head -n 1)"
  if [ -n "$pid" ]; then
    unit="$(systemctl status "$pid" 2>/dev/null | head -n 1 \
      | sed -n 's/.*[[:space:]]\([A-Za-z0-9@:._-]*\.service\)[[:space:]].*/\1/p')"
    if [ -n "$unit" ]; then
      printf '%s\n' "$unit"
      return 0
    fi
  fi
  ss -ltnpH "sport = :$1" 2>/dev/null \
    | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' \
    | head -n 1
}

# Ports another service on this box owns (deck.reserved_ports). A listener
# scan taken while that service restarts shows its port free; moving in would
# work once and break the neighbour on its next start. So the list, not the
# scan, is the rule -- and who holds each one is recorded now and compared
# after the install (RESERVED_AFTER), as a control.
RESERVED_BEFORE=""
for rp in $RESERVED_PORTS; do
  [ "$DECK_PORT" != "$rp" ] \
    || die "deck.port is ${rp}, which deck.reserved_ports says belongs to another \
service on this box. The deck does not take that port; pick another deck.port."
  RP_HOLDER="$(port_holder "$rp")" || true
  say "port ${rp} is reserved (held by ${RP_HOLDER:-nothing right now}) -- leaving it alone"
  RESERVED_BEFORE="${RESERVED_BEFORE} ${rp}=${RP_HOLDER:-free}"
done

# The public edge needs 443. A box that already serves a site there (nginx,
# apache) is not ours to take over: refuse, and say what holds it.
if [ "$HAS_EDGE" = "1" ]; then
  EDGE_HOLDER="$(port_holder 443)" || true
  case "$EDGE_HOLDER" in
    ""|caddy|caddy.service) ;;
    *) die "port 443 is held by '${EDGE_HOLDER}'. The public edge (caddy) needs it. \
Use a box of its own, or network.mode = \"local\" and reach the deck another way." ;;
  esac
fi

# Our own port. On a re-run the holder is our own service, which is not a clash.
DECK_HOLDER="$(port_holder "$DECK_PORT")" || true
[ -z "$DECK_HOLDER" ] || systemctl is-active --quiet "$UNIT_NAME" \
  || die "port ${DECK_PORT} is held by '${DECK_HOLDER}', which is not ${UNIT_NAME}. \
Stop it, or set another deck.port in ${DECK_TOML}."

# ── base tooling ─────────────────────────────────────────────────────────────
#
# Everything a running deck shells out to, swept out of the code rather than
# listed from memory:
#
#   server/office.py       git rev-parse, to put a branch on every card
#   server/spawn.py        claude  (and osascript, which Linux does not have --
#                          so on this box only `claude --bg` can start a desk)
#   hooks/*.js, approval   node
#   this script            ss (iproute2), openssl, curl, python3 -m venv
#   reachability at all    ufw
#
# Checked before installed, so a box that already has them never sees apt at
# all. `set -euo pipefail` would otherwise turn a missing `ss` into a bare exit
# 127 halfway through, with the box half-configured and nothing naming the line.

BASE_MISSING=""
for tool in python3 curl openssl ss git ufw node; do
  command -v "$tool" >/dev/null 2>&1 || BASE_MISSING="${BASE_MISSING} ${tool}"
done
python3 -c 'import venv' >/dev/null 2>&1 || BASE_MISSING="${BASE_MISSING} python3-venv"

if [ -n "$BASE_MISSING" ]; then
  say "missing base tooling:${BASE_MISSING} -- installing the named packages"
  # No `upgrade`. This box runs two other people's services; a blanket upgrade
  # is a deploy of everything at once that nobody reviewed and nobody can undo.
  # `nodejs` is the distro's, not a third-party apt source: adding one to a live
  # box for a runtime the hooks are happy with at 18+ is not worth the change.
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    python3-venv iproute2 openssl curl ca-certificates git ufw nodejs
else
  say "base tooling present: python3-venv, iproute2, openssl, curl, git, ufw, node"
fi

# The public edge: caddy (Ubuntu's own package, no third-party apt source) and
# qrencode, which prints the pairing code as a QR in the terminal.
if [ "$HAS_EDGE" = "1" ]; then
  if command -v caddy >/dev/null 2>&1 && command -v qrencode >/dev/null 2>&1; then
    say "edge tooling present: caddy, qrencode"
  else
    say "installing the public edge: caddy, qrencode"
    apt-get update -qq
    apt-get install -y --no-install-recommends caddy qrencode
  fi
fi

command -v node >/dev/null 2>&1 \
  || die "node is still missing after the package install. Every session hook \
runs under it and the board would stay blank. Install a node 18+ by hand and \
run me again."

# ── the CLI the deck hires with ──────────────────────────────────────────────
#
# A deck with no `claude` is a board with no players. preflight() below refuses
# such a box, which turns this into a failed install and a human reading a
# blocker -- one command means the command fixes it instead.
#
# Installed AS THE SERVICE USER. `claude` keeps its credentials in the
# installing user's home; as root they land in /root, where the service user
# cannot read them, and every spawn dies at the login prompt while the board
# shows the desk as WORKING.
#
# codex and opencode are deliberately NOT installed. server/pretrust.py returns
# `engine_not_covered` for both -- the deck cannot write either one's trust gate,
# so a desk opens and stalls on a prompt nothing outside the process can answer
# -- and server/spawn.py raises `background_unsupported` for any engine but
# claude, while its only other start path needs osascript. There is no code path
# on this box that can drive one. A binary the board can offer and never drive
# is worse than an absent one.

if as_deck test -x "$CLAUDE_BIN"; then
  say "claude is already installed for ${DECK_USER} at ${CLAUDE_BIN}"
else
  say "no claude for ${DECK_USER} -- installing the ${CLAUDE_VERSION} channel"
  CLAUDE_TMP="$(as_deck mktemp -t claude-install.XXXXXX)"
  sudo -u "$DECK_USER" curl -fsSL https://claude.ai/install.sh -o "$CLAUDE_TMP" \
    || die "could not fetch the claude installer; the box needs outbound https"
  sudo -u "$DECK_USER" bash "$CLAUDE_TMP" "$CLAUDE_VERSION" \
    || die "the claude installer failed; nothing here was enabled"
  rm -f "$CLAUDE_TMP"
fi

as_deck test -x "$CLAUDE_BIN" \
  || die "claude is still not at ${CLAUDE_BIN} after installing. Nothing was enabled."

# ── the login this script cannot perform ─────────────────────────────────────
#
# Checked HERE, before anything is enabled, rather than left to preflight: the
# distinction matters to whoever is reading the output. Every other blocker is
# something the script should have fixed. This one is the owner's, and the only
# useful thing to print is the command.
#
# A PROXY, not proof -- the credentials file means somebody logged in on this
# box once, not that the session is still live. `claude -p` is the only real
# proof and it spends tokens, so the runbook asks for it by hand.

# Two ways a box holds a login: the browser flow's credentials file, or a
# long-lived `claude setup-token` token in claude.oauth_env (what the units load).
logged_in() {
  as_deck test -s "${DECK_HOME}/.claude/.credentials.json" \
    || grep -qs '^CLAUDE_CODE_OAUTH_TOKEN=.' "$OAUTH_ENV"
}

# --claude-token-file: a `claude setup-token` token (sk-ant-oat...) the operator
# minted elsewhere. Written into claude.oauth_env, 0600, the service user's --
# never printed, never in argv (printf is a shell builtin), never in a unit.
# Rewritten only when it differs, so a re-run touches nothing.
write_oauth_env() {
  local secret_line tmp
  secret_line="$(head -n 1 "$1" | tr -d '[:space:]')"
  case "$secret_line" in
    sk-ant-oat*) ;;
    *) die "$1 does not hold a claude setup-token token (they start with sk-ant-oat)" ;;
  esac
  tmp="$(mktemp)"
  ( umask 077
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$secret_line" > "$tmp" )
  if ! cmp -s "$tmp" "$OAUTH_ENV"; then
    install -D -m 0600 -o "$DECK_USER" -g "$DECK_USER" "$tmp" "$OAUTH_ENV"
    chown "${DECK_USER}:${DECK_USER}" "$(dirname "$OAUTH_ENV")"
    say "wrote the claude login to ${OAUTH_ENV} (0600, ${DECK_USER})"
  fi
  rm -f "$tmp"
}

if [ -n "$CLAUDE_TOKEN_FILE" ]; then
  [ -r "$CLAUDE_TOKEN_FILE" ] || die "cannot read --claude-token-file ${CLAUDE_TOKEN_FILE}"
  if [ -n "$DECKCTL" ]; then
    "$DECKCTL" login --token-file "$CLAUDE_TOKEN_FILE" \
      || die "deckctl login refused ${CLAUDE_TOKEN_FILE} (the line above says why)"
  else
    write_oauth_env "$CLAUDE_TOKEN_FILE"
  fi
fi

if ! logged_in && [ "$SKIP_LOGIN" = "0" ] && [ -n "$DECKCTL" ] && [ -t 0 ]; then
  say "log Claude in: open the link it prints on any device, approve, paste back"
  "$DECKCTL" login || true
fi

if logged_in; then
  say "claude is installed and holds a credential for ${DECK_USER}"
elif [ "$SKIP_LOGIN" = "1" ]; then
  say "  NOTE  --skip-login: claude is NOT logged in, so no agent can start. \
Everything else is installed. Later:  sudo deckctl login  (or run me again with \
--claude-token-file <file>)."
else
  die "the box has claude but nobody is logged in, and a script cannot do it.

Run this, as him, on the box:

    sudo -u ${DECK_USER} env HOME=${DECK_HOME} ${CLAUDE_BIN} setup-token

then run me again with  --claude-token-file <a file holding the token it prints>
(or --skip-login to install everything else now). Nothing was enabled: a deck
that answers /api/state with no login is green on the board and dies at the
login prompt on the first hire."
fi

# ── the token a LOGIN SHELL can see ──────────────────────────────────────────
#
# MEASURED on this box on 2026-09-02, with the service perfectly healthy:
#
#     # su - agentdeck -c claude
#     Failed to authenticate: OAuth session expired and could not be refreshed
#
# The token lives in ${DECK_HOME}/.claude/oauth.env. systemd loads it through
# EnvironmentFile= in the unit; a login shell reads nothing of the sort. So
# every automated check passed -- they all asked whether a credentials FILE
# existed -- while a human, and anything run through `su -`, could not
# authenticate at all. Same false-pass shape as the hook that posted into a
# closed socket: the cheap proxy answered yes and the expensive truth was no.
#
# `set -a` is load-bearing. Sourced without it the variables are shell-local
# and the `claude` child process still sees nothing, which looks identical to
# not having done this at all.
#
# Guarded on a grep, because re-running this script is the box's only upgrade
# path and an unguarded append writes the block again on every run.

PROFILE="${DECK_HOME}/.profile"
PROFILE_MARK="# agent-deck: let a login shell see the claude token"

if as_deck test -r "$OAUTH_ENV"; then
  if grep -qF "$PROFILE_MARK" "$PROFILE" 2>/dev/null; then
    say "${PROFILE} already sources oauth.env"
  else
    say "teaching a login shell to read ${OAUTH_ENV}"
    # Appended as the service user so the file cannot end up owned by root.
    as_deck tee -a "$PROFILE" >/dev/null <<PROFILE_BLOCK

${PROFILE_MARK}
if [ -r "\$HOME/.claude/oauth.env" ]; then set -a; . "\$HOME/.claude/oauth.env"; set +a; fi
PROFILE_BLOCK
  fi
else
  say "  NOTE  no ${OAUTH_ENV}; a login shell will not be able to authenticate \
even though the service can"
fi

# ── directories: -p everywhere, so run two is a no-op ────────────────────────

mkdir -p "$BUS_DIR"                       # the event log the whole board reads
chown -R "${DECK_USER}:${DECK_USER}" "${DECK_HOME}/.claude"

# devices.json / pairing.json live here (K3): the service user's, nobody else's.
mkdir -p "$STATE_DIR"
chown "${DECK_USER}:${DECK_USER}" "$STATE_DIR"
chmod 0700 "$STATE_DIR"

# ── the token: first run only ────────────────────────────────────────────────
#
# Guarded on the env file's absence, not on "does the variable look unset".
# A second run must leave the existing token byte-for-byte alone: the Mac and
# the iOS client already hold it, and silently rotating it would present as
# "the deck stopped answering" with nothing in any log saying why.

if [ ! -f "$ENV_FILE" ]; then
  say "no ${ENV_FILE} yet -- generating a token (this happens once)"
  # umask first so the file is never world-readable, not even for the instant
  # between creation and chmod.
  ( umask 077; printf 'AGENT_DECK_TOKEN=%s\n' "$(openssl rand -hex 32)" > "$ENV_FILE" )
else
  say "keeping the existing token in ${ENV_FILE}"
fi

# root-only, deliberately. systemd's manager reads EnvironmentFile as root and
# hands the variable to the process after dropping privileges, so the service
# user never needs to read this file -- and an agent running as that user
# therefore cannot `cat` the deck's own token out of it.
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# ── the OpenAI key (optional: voice calls only) ──────────────────────────────
#
# Checked with one GET /v1/models before it is kept. The key travels in a 0600
# header file (`curl -H @file`), never in argv, where `ps` would show it to
# every user on the box. It lands in agentdeck.env beside the token, rewritten
# whole and only when it changes.
ENV_CHANGED=0
set_openai_key() {
  local key hdr code tmp
  key="$(head -n 1 "$1" | tr -d '[:space:]')"
  [ -n "$key" ] || return 0
  hdr="$(mktemp)"; tmp="$(mktemp)"
  ( umask 077; printf 'Authorization: Bearer %s\n' "$key" > "$hdr" )
  code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -H @"$hdr" \
    https://api.openai.com/v1/models 2>/dev/null || true)"
  rm -f "$hdr"
  if [ "$code" != "200" ]; then
    rm -f "$tmp"
    say "  NOTE  that OpenAI key was not accepted (HTTP ${code:-none}); voice calls stay off. \
Nothing was stored."
    return 0
  fi
  ( umask 077
    { grep -v '^OPENAI_API_KEY=' "$ENV_FILE" || true; printf 'OPENAI_API_KEY=%s\n' "$key"; } > "$tmp" )
  if ! cmp -s "$tmp" "$ENV_FILE"; then
    install -m 0600 -o root -g root "$tmp" "$ENV_FILE"
    ENV_CHANGED=1
    say "OpenAI key accepted and stored in ${ENV_FILE}"
  fi
  rm -f "$tmp"
}

if [ -n "$OPENAI_KEY_FILE" ]; then
  [ -r "$OPENAI_KEY_FILE" ] || die "cannot read --openai-key-file ${OPENAI_KEY_FILE}"
  set_openai_key "$OPENAI_KEY_FILE"
elif [ "$FRESH" = "1" ] && [ "$ASSUME_YES" = "0" ] && [ -t 0 ] \
    && ! grep -q '^OPENAI_API_KEY=.' "$ENV_FILE"; then
  KEY_TMP="$(mktemp)"
  ( umask 077
    read -r -s -p "install-deck: Voice calls need an OpenAI key (optional). Paste it or press Enter to skip: " PASTED
    printf '\n' >&2
    printf '%s\n' "$PASTED" > "$KEY_TMP" )
  set_openai_key "$KEY_TMP"
  rm -f "$KEY_TMP"
fi

# ── the python environment ───────────────────────────────────────────────────

if [ ! -x "$VENV_PY" ]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_PY" -m pip install --quiet --upgrade pip
fi
# PINNED. `--upgrade fastapi uvicorn` made run five a different box from run
# one, which is the definition of not idempotent, and is also how a box that
# worked in September stops importing in October with nothing changed on it.
# Checked before installed, so an unchanged box never runs pip at all.
# httpx: server/realtime.py imports it at module level. MEASURED in a fresh
# 24.04 container: without it the deck dies at start. The first box had
# httpx==0.28.1 installed by hand, which is why nobody saw it.
if ! "$VENV_PY" -c 'import sys, fastapi, uvicorn, httpx
sys.exit(0 if (fastapi.__version__, uvicorn.__version__, httpx.__version__)
         == ("0.141.1", "0.52.4", "0.28.1") else 1)' >/dev/null 2>&1; then
  "$VENV_PY" -m pip install --quiet fastapi==0.141.1 uvicorn==0.52.4 httpx==0.28.1
fi
chown -R "${DECK_USER}:${DECK_USER}" "$VENV_DIR"

# A release unpacks root-owned, but the deck writes its log and cache inside
# the app dir: create them and hand them over, or it cannot start logging.
touch "${REPO_DIR}/.agent-deck.log"
mkdir -p "${REPO_DIR}/.cache"
chown "${DECK_USER}:${DECK_USER}" "${REPO_DIR}/.agent-deck.log" "${REPO_DIR}/.cache"

# ── the firewall: the tunnel, and nothing else ───────────────────────────────
#
# This rule was added by hand on 2026-09-02 and before it the box answered
# itself and the Mac got nothing. A rule made by hand is a rule the next box
# does not have.
#
# `in on <tunnel>` is the whole safety property. The interface is deck.toml's
# network.wireguard_iface, which render.py refuses unless it is a plain
# interface name. ufw's own dedupe makes a re-run a no-op ("Skipping adding
# existing rule"), and nothing here ever deletes, resets or disables anything:
# other services on a shared box depend on that rule list.
#
# Only a WireGuard box gets a rule for the deck's port. A local box answers
# loopback only and needs none.

if [ "$NET_MODE" = "wireguard" ]; then
  [ -n "$WG_IFACE" ] || die "network.mode is wireguard but network.wireguard_iface is empty"
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    ufw allow in on "$WG_IFACE" to any port "$DECK_PORT" proto tcp comment "agent deck: VPN only" >/dev/null
    say "ufw: ${DECK_PORT}/tcp allowed on ${WG_IFACE} only (never on the public interface)"
  else
    say "ufw is not active -- no rule added. Reachability over the tunnel is whatever \
this box's packet filter already says."
  fi
elif [ "$HAS_EDGE" = "1" ]; then
  # A public box: 443 for the edge and the SSH port that is already in use --
  # nothing else, and never the deck's own port (it is on loopback anyway).
  # The SSH port is read from sshd itself; guessing 22 on a box that moved it
  # would lock the operator out the moment deny-incoming lands.
  SSH_PORT="$( (sshd -T 2>/dev/null || true) | awk '$1 == "port" {print $2; exit}')"
  SSH_PORT="${SSH_PORT:-22}"
  if ufw status 2>/dev/null | grep -q "^Status: active"; then
    # Somebody else's rule list: add ours, change nothing else.
    ufw allow 443/tcp comment "agent deck: public edge" >/dev/null
    say "ufw is active: 443/tcp added; existing rules and the default policy left as they are"
  else
    # Inactive: ours to set up, and SSH first, in the same run, before deny.
    ufw allow "${SSH_PORT}"/tcp comment "ssh (kept open by the agent deck installer)" >/dev/null
    ufw default deny incoming >/dev/null
    ufw allow 443/tcp comment "agent deck: public edge" >/dev/null
    ufw --force enable >/dev/null
    say "ufw enabled: deny incoming, allow ${SSH_PORT}/tcp (ssh) and 443/tcp (the edge)"
  fi
fi

# ── docker: deck.toml's desks.docker (or --with-docker) ──────────────────────
#
# A deliberate setting, and the reason is the packet filter. dockerd inserts its own
# chains into iptables ahead of ufw's, so a published container port is
# reachable on every interface REGARDLESS of the deny-incoming default -- on a
# box whose 80 and 443 already face the internet. It also enables ip_forward and
# adds a 172.17.0.0/16 bridge to a machine that is a WireGuard endpoint. That is
# a change to the network posture of a live box, so it is the operator's call
# (desks.docker) and not a side effect of installing a dashboard. A box migrated
# from a unit gets desks.docker = whether docker is already installed.
#
# When it is on, the daemon default is pinned to loopback: `-p 5900:5900`
# otherwise binds 0.0.0.0, and the agents'-own-computer containers publish a
# browser and a terminal. A container written later must not be able to expose a
# desk to the internet by omission.

if [ "$WITH_DOCKER" = "1" ]; then
  # A CONTROL, taken before anything is installed. This box serves
  # another site on public 80/443 through nginx, and the one
  # way this phase can do real damage is to that. One reading afterwards
  # cannot tell "docker broke the site" from "the site was already 403", so
  # the same probe runs before and after and the two are compared.
  # Measured by hand on 2026-09-02: https 403/403/403 and http 301, identical
  # both times.
  #
  # The sites are deck.toml's doctor.public_probe_urls; a box with none has
  # nothing public to break and the comparison is trivially equal.
  public_probe() {
    local url out=""
    for url in $PROBE_URLS; do
      out="${out} $(curl -sS --max-time 8 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    done
    printf '%s' "${out# }"
  }
  PUBLIC_BEFORE="$(public_probe)"
  say "public sites before docker: ${PUBLIC_BEFORE:-none listed} (${PROBE_URLS:-doctor.public_probe_urls is empty})"

  # daemon.json FIRST, before the package exists, so dockerd never starts even
  # once with 0.0.0.0 as its publish default. dockerd inserts its chains ahead
  # of ufw's, so a published port on this box is reachable from the internet
  # regardless of the deny-incoming default -- there is no "we'll fix it after
  # the install" here, the window IS the vulnerability.
  #
  #   ip           an accidental `-p 5900:5900` binds loopback, not 0.0.0.0
  #   iptables     left true on purpose: false breaks container egress, and the
  #                containment we want comes from `ip`, not from disarming the
  #                daemon's own NAT
  #   live-restore containers survive a dockerd restart, so an apt upgrade does
  #                not kill every desk that is mid-task
  DOCKER_CHANGED=0
  mkdir -p /etc/docker
  if [ ! -f /etc/docker/daemon.json ]; then
    DOCKER_CHANGED=1
    say "pinning docker's publish default to loopback BEFORE installing it"
    ( umask 022; printf '{\n  "ip": "127.0.0.1",\n  "iptables": true,\n  "live-restore": true\n}\n' \
        > /etc/docker/daemon.json )
  else
    say "/etc/docker/daemon.json already exists -- not touching it. CHECK that it \
sets \"ip\" to 127.0.0.1, or a published container port lands on 0.0.0.0."
  fi

  if command -v docker >/dev/null 2>&1; then
    say "docker is already installed -- leaving the package alone"
  else
    say "--with-docker: installing the distro's container runtime"
    apt-get update -qq
    apt-get install -y --no-install-recommends docker.io docker-buildx docker-compose-v2
    DOCKER_CHANGED=1
  fi
  # Restarted only when this run changed its config or installed it: a re-run
  # on a box whose desks are running must not bounce their daemon.
  if [ "$DOCKER_CHANGED" = "1" ]; then
    systemctl restart docker || die "dockerd did not come back after restart"
  else
    systemctl is-active --quiet docker || systemctl start docker \
      || die "dockerd is installed but will not start"
  fi

  # THE TRADE, stated where whoever runs this will read it.
  #
  # The deck runs as ${DECK_USER} and cannot reach /var/run/docker.sock without
  # this ("permission denied" -- measured). Adding the user to the docker group
  # makes it ROOT-EQUIVALENT on this machine: membership is enough to bind-mount
  # / into a privileged container. On a box that also serves a live public site,
  # that is a real widening and it is the owner's call, not a side effect.
  #
  # Rootless Docker is the right containment and is NOT taken here: Ubuntu
  # 24.04's archive has no docker-ce-rootless-extras (checked -- `apt-cache
  # policy` returns nothing; uidmap and slirp4netns ARE present and
  # ${DECK_USER} already has /etc/subuid and /etc/subgid entries), so it needs
  # Docker's own apt repo and a docker.io -> docker-ce swap on a live box.
  # Documented follow-up, deliberately not a silent gap.
  if id -nG "$DECK_USER" | tr ' ' '\n' | grep -qx docker; then
    say "${DECK_USER} is already in the docker group"
  else
    say "  RISK  adding ${DECK_USER} to the docker group makes it ROOT-EQUIVALENT \
on this box. Rootless docker is the containment this wants and is unavailable \
from 24.04's archive -- see the comment above this line."
    usermod -aG docker "$DECK_USER"
  fi

  # The desk image. Built here so `verify` below has something to exercise --
  # and it is exercised, not tagged and trusted. Proved by hand on the box:
  # start -> is_up True -> frame() returned a 12,591-byte JPEG -> exec_argv ran
  # `uname -sr` inside it ("Linux 6.8.0-134-generic") -> stop -> is_up False.
  DESK_CONTEXT="${REPO_DIR}/docker/desk-computer"
  if [ -d "$DESK_CONTEXT" ]; then
    if docker image inspect "$DESK_IMAGE" >/dev/null 2>&1; then
      say "desk image ${DESK_IMAGE} is already built"
    else
      say "building ${DESK_IMAGE} (a few minutes, ~1.5GB)"
      docker build -t "$DESK_IMAGE" "$DESK_CONTEXT" \
        || die "the desk image did not build; the deck can hire but no agent gets a computer"
    fi
  else
    say "  NOTE  no ${DESK_CONTEXT}; agents on this box get no computer"
  fi

  PUBLIC_AFTER="$(public_probe)"
  say "public sites after docker:  ${PUBLIC_AFTER:-none listed}"
  if [ "$PUBLIC_BEFORE" != "$PUBLIC_AFTER" ]; then
    die "a public site on this box changed across the docker install (${PROBE_URLS}): \
${PUBLIC_BEFORE} -> ${PUBLIC_AFTER}. Docker is installed and its chains are in \
the packet filter. Investigate before trusting this box:  iptables -S DOCKER-USER"
  fi
  say "public sites unchanged across the docker install"
else
  say "docker not installed (desks.docker = false in ${DECK_TOML}). Desks get no \
computer of their own."
fi

# ── the units: rendered from deck.toml, replaced only where they differ ──────
#
# `put` compares first, so an unchanged box sees no write and no restart.

UNIT_CHANGED=0
put "${RENDERED}/agentdeck.service" "$UNIT_DST" && UNIT_CHANGED=1 || true
[ "$UNIT_CHANGED" = "0" ] || systemctl daemon-reload

# ── preflight: measure, then let the tested judge decide ─────────────────────
#
# Facts in, blockers out. The judging lives in server/deploy_check.py, which is
# pure and unit-tested; this function only measures.

facts_json() {
  local py node_present claude_present claude_auth bus_ok token_ok free_mb ports
  py="$("$VENV_PY" -c 'import platform; print(platform.python_version())')"

  node_present=false
  command -v node >/dev/null 2>&1 && node_present=true || true

  claude_present=false
  command -v claude >/dev/null 2>&1 && claude_present=true || true
  sudo -u "$DECK_USER" test -x "${DECK_HOME}/.local/bin/claude" && claude_present=true || true

  # A PROXY, not proof: a stored credential file means somebody logged in on
  # this box at some point. Only a real one-shot prompt proves the session is
  # live, and that spends tokens -- the runbook asks you to run it by hand.
  claude_auth=false
  sudo -u "$DECK_USER" test -s "${DECK_HOME}/.claude/.credentials.json" && claude_auth=true || true

  bus_ok=false
  sudo -u "$DECK_USER" test -w "$BUS_DIR" && bus_ok=true || true

  token_ok=false
  grep -qE '^AGENT_DECK_TOKEN=.+' "$ENV_FILE" && token_ok=true || true

  free_mb="$(df -Pm "$REPO_DIR" | awk 'NR==2 {print $4}')"
  ports="$(ports_json)"

  cat <<JSON
{
  "python_version": "${py}",
  "node_present": ${node_present},
  "claude_present": ${claude_present},
  "claude_authenticated": ${claude_auth},
  "deck_port": ${DECK_PORT},
  "ports_in_use": ${ports},
  "bus_dir": "${BUS_DIR}",
  "bus_dir_writable": ${bus_ok},
  "token_configured": ${token_ok},
  "disk_free_mb": ${free_mb}
}
JSON
}

ports_json() {
  local out="" port holder
  for port in $RESERVED_PORTS "$DECK_PORT"; do
    holder="$(port_holder "$port")" || true
    [ -n "$holder" ] || continue
    [ -z "$out" ] || out="${out}, "
    out="${out}\"${port}\": \"${holder}\""
  done
  printf '{%s}' "$out"
}

say "preflight..."
# --skip-login drops exactly the blockers that vanish when the login is
# assumed present -- computed by the judge itself, never by matching its words.
BLOCKERS="$(facts_json | SKIP_LOGIN="$SKIP_LOGIN" PYTHONPATH="$REPO_DIR" "$VENV_PY" -c '
import json, os, sys
from server.deploy_check import preflight
facts = json.load(sys.stdin)
out = preflight(facts)
if os.environ.get("SKIP_LOGIN") == "1":
    keep = set(preflight({**facts, "claude_authenticated": True}))
    out = [b for b in out if b in keep]
sys.stdout.write("\n  ".join(out))')"

[ -z "$BLOCKERS" ] || die "this box is not ready:
  ${BLOCKERS}
Nothing was enabled. Fix the above and run me again."

say "preflight clean"

# ── start it, and prove it answers ───────────────────────────────────────────

systemctl enable "$UNIT_NAME" >/dev/null 2>&1
# Restarted when its unit changed, when it is not running, or on a box deployed
# from source (install_source = rsync): there the code under it may have changed
# with no unit change, and re-running this script IS the deploy. A release box
# gets new code only through `deckctl update`, which restarts it itself.
if [ "$UNIT_CHANGED" = "1" ] || [ "$ENV_CHANGED" = "1" ] || [ "$INSTALL_SOURCE" = "rsync" ] \
    || ! systemctl is-active --quiet "$UNIT_NAME"; then
  systemctl restart "$UNIT_NAME"
  sleep 2
else
  say "${UNIT_NAME} unchanged and running -- not restarted"
fi

# ── the public edge: caddy on 443, a certificate, and the pin ───────────────
#
# K10. The Caddyfile is rendered (deploy/templates/Caddyfile.in); it forwards
# /v1/* and /healthz to the loopback deck and 404s everything else.
#
# The certificate, in order of preference (plan section 2):
#   sslip / domain  a publicly trusted one from Caddy's default issuers (Let's
#                   Encrypt, then ZeroSSL), over TLS-ALPN on 443 -- port 80
#                   stays closed. Waited for up to 120 s.
#   self-signed     one long-lived key and certificate minted here ONCE, and
#                   its SPKI pin written to /etc/agent-deck/tls-pin for the
#                   pairing code (`tls internal` would rotate the key every 12
#                   hours and break every paired Mac).
# If the trusted certificate does not arrive in time, the box switches itself
# to self-signed, SAYS SO, and records the switch in deck.toml.

TLS_DIR="${ETC_DIR}/tls"
CADDYFILE=/etc/caddy/Caddyfile

mint_self_signed() {
  local san tmp
  if [ ! -s "${TLS_DIR}/key.pem" ] || [ ! -s "${TLS_DIR}/cert.pem" ]; then
    mkdir -p "$TLS_DIR"
    case "$NET_HOSTNAME" in
      *[!0-9.]*) san="DNS:${NET_HOSTNAME}" ;;
      *)         san="IP:${NET_HOSTNAME}" ;;
    esac
    ( umask 077
      openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -days 3650 -subj "/CN=${NET_HOSTNAME}" -addext "subjectAltName=${san}" \
        -keyout "${TLS_DIR}/key.pem" -out "${TLS_DIR}/cert.pem" >/dev/null 2>&1 ) \
      || die "could not mint the self-signed certificate for ${NET_HOSTNAME}"
    say "minted a self-signed certificate for ${NET_HOSTNAME} (10 years, one key)"
  fi
  # caddy reads it; nobody else does.
  chown -R root:caddy "$TLS_DIR"
  chmod 0750 "$TLS_DIR"
  chmod 0640 "${TLS_DIR}/key.pem"
  chmod 0644 "${TLS_DIR}/cert.pem"
  # K2's pin: sha256 of the leaf's SubjectPublicKeyInfo, base64. Not secret.
  TLS_PIN="sha256/$(openssl x509 -in "${TLS_DIR}/cert.pem" -pubkey -noout \
    | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64)"
  tmp="$(mktemp)"
  printf '%s\n' "$TLS_PIN" > "$tmp"
  put "$tmp" "${ETC_DIR}/tls-pin" || true
  rm -f "$tmp"
}

install_caddyfile() {
  local pkg_md5
  # Ours (it carries the marker), or the caddy package's untouched default.
  # Anything else is somebody's site, and this script does not overwrite it.
  if [ -f "$CADDYFILE" ] && ! grep -q "managed by agent-deck" "$CADDYFILE"; then
    pkg_md5="$(dpkg-query -W -f='${Conffiles}\n' caddy 2>/dev/null \
      | awk '$1 == "/etc/caddy/Caddyfile" {print $2}')"
    [ -n "$pkg_md5" ] && [ "$(md5sum < "$CADDYFILE" | cut -d' ' -f1)" = "$pkg_md5" ] \
      || die "${CADDYFILE} has a site in it that is not Agent Deck's. I will not overwrite \
it. Use a box of its own, or move that site elsewhere and run me again."
  fi
  caddy validate --config "${RENDERED}/Caddyfile" --adapter caddyfile >/dev/null 2>&1 \
    || die "the rendered Caddyfile does not validate:  caddy validate --config ${RENDERED}/Caddyfile"
  if put "${RENDERED}/Caddyfile" "$CADDYFILE"; then
    systemctl enable caddy >/dev/null 2>&1
    systemctl reload-or-restart caddy || die "caddy did not take the new Caddyfile: journalctl -u caddy -n 50"
    say "caddy: serving https://${NET_HOSTNAME} (/v1/* and /healthz only)"
  else
    systemctl enable caddy >/dev/null 2>&1
    systemctl is-active --quiet caddy || systemctl start caddy \
      || die "caddy will not start: journalctl -u caddy -n 50"
  fi
}

# Any HTTP answer through a verified TLS handshake: the certificate is good.
edge_trusted() {
  local code
  local resolve=()
  [ -z "$EDGE_RESOLVE" ] || resolve=(--resolve "$EDGE_RESOLVE")
  code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    "${resolve[@]}" "https://${NET_HOSTNAME}/healthz" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

if [ "$HAS_EDGE" = "1" ]; then
  [ "$NET_TLS" != "self-signed" ] || mint_self_signed
  install_caddyfile
  if [ "$NET_TLS" = "sslip" ] || [ "$NET_TLS" = "domain" ]; then
    say "waiting up to 120 s for a publicly trusted certificate for ${NET_HOSTNAME}..."
    for _ in $(seq 1 24); do edge_trusted && break; sleep 5; done
    if edge_trusted; then
      say "certificate for ${NET_HOSTNAME} is publicly trusted"
    else
      say "  NOTE  no trusted certificate for ${NET_HOSTNAME} within 120 s (Let's Encrypt \
and ZeroSSL). Switching this box to its OWN certificate: the pairing code will carry \
its pin, so the app still connects securely. To try again later: \
deckctl config set network.tls ${NET_TLS}"
      "${RENDER[@]}" set --config "$DECK_TOML" network.tls self-signed \
        || die "could not record network.tls self-signed in ${DECK_TOML}"
      NET_TLS=self-signed
      "${RENDER[@]}" render --config "$DECK_TOML" --out "$RENDERED" \
        || die "could not re-render the Caddyfile for self-signed"
      mint_self_signed
      install_caddyfile
    fi
  fi
  [ -z "${TLS_PIN:-}" ] || say "tls pin: ${TLS_PIN}  (in ${ETC_DIR}/tls-pin; the pairing code carries it)"
fi

# ── verify: exercise every piece against the service that is now running ─────
#
# The phase this script did not have. It previously curled /api/state and
# called that a deploy -- which is exactly the state the box was found in on
# 2026-09-02: green, answering, with every hook it installs posting into a
# closed socket and nothing on the board saying so.
#
# Each check runs, prints what it got, and records a failure rather than
# aborting. The install has already happened by this point; the useful thing is
# the complete list of what is and is not true, not the first thing that broke.

FAILED=""

check() {          # check <name> <verdict:ok|no> <detail>
  if [ "$2" = "ok" ]; then
    printf 'install-deck:   PASS  %-28s %s\n' "$1" "$3"
  else
    printf 'install-deck:   FAIL  %-28s %s\n' "$1" "$3" >&2
    FAILED="${FAILED} $1"
  fi
}

# THE SMOKE ALARM (H1). deckdoctor checks disk, login, the deck and unread
#    messages every 10 minutes; the journal cap and the log rotation stop two
#    known disk eaters. Installed BEFORE the verification on purpose: a box whose
#    verification fails is exactly the one that needs the alarm. Re-run safe:
#    a file is replaced only when its content differs, and journald is only
#    restarted when its cap actually changed. Nothing here touches agentdeck.
HEALTH_CHANGED=0
put "${RENDERED}/deckdoctor.service" /etc/systemd/system/deckdoctor.service && HEALTH_CHANGED=1 || true
put "${SRC_DIR}/deckdoctor.timer"   /etc/systemd/system/deckdoctor.timer   && HEALTH_CHANGED=1 || true
put "${RENDERED}/agent-deck-log.logrotate" /etc/logrotate.d/agent-deck || true
if put "${SRC_DIR}/journald-cap.conf" /etc/systemd/journald.conf.d/agent-deck-cap.conf; then
  systemctl restart systemd-journald
  say "journal capped at 1G"
fi
[ "$HEALTH_CHANGED" = "0" ] || systemctl daemon-reload
systemctl enable --now deckdoctor.timer >/dev/null 2>&1 \
  && check "deckdoctor timer" ok "$(systemctl is-active deckdoctor.timer), every 10 min" \
  || check "deckdoctor timer" no "could not enable deckdoctor.timer"

say "verifying..."

# Read once, before the first check that needs it. /api authenticates now --
# an unauthenticated probe would 401 on a healthy box and refuse the install.
TOKEN="$(sed -n 's/^AGENT_DECK_TOKEN=//p' "$ENV_FILE" | head -n 1)"

# 1. the service answers on the address it claims, and on nothing wider.
STATE_CODE="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN}" \
  "${DECK_BASE}/api/state" 2>/dev/null || echo 000)"
[ "$STATE_CODE" = "200" ] \
  && check "service answers" ok "${DECK_BASE}/api/state -> 200" \
  || check "service answers" no "${DECK_BASE}/api/state -> ${STATE_CODE}; see \
journalctl -u ${UNIT_NAME} -n 50 --no-pager"

# 2. the token authenticates. Anonymously /v1 answers 503 auth_not_configured,
#    and a box that "answers" 503 to everything looks healthy and does nothing.
V1_CODE="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN}" "${DECK_BASE}/v1/agents" 2>/dev/null || echo 000)"
case "$V1_CODE" in
  200) check "token authenticates" ok "/v1/agents -> 200" ;;
  503) check "token authenticates" no "/v1/agents -> 503: the service is running \
without AGENT_DECK_TOKEN. Check EnvironmentFile in ${UNIT_DST}." ;;
  *)   check "token authenticates" no "/v1/agents -> ${V1_CODE}" ;;
esac

# 3. A REAL HOOK ROUND TRIP -- the check that would have caught this box.
#    cc-approve.js always exits 0 and always prints a decision, even when it
#    reached nothing; that is deliberate, so a broken deck never hangs a
#    session, and it means "the hook ran" proves nothing at all. The reason
#    string is the discriminator: "deck unreachable" / "timed out" when the
#    socket refused, a rule verdict when the deck answered.
HOOK_URL="$(systemctl show -p Environment --value "$UNIT_NAME" \
  | tr ' ' '\n' | sed -n 's/^DECK_URL=//p' | head -n 1)"
[ -n "$HOOK_URL" ] || HOOK_URL="$DECK_BASE"
HOOK_SAID="$(printf '%s' \
  '{"session_id":"install-verify","cwd":"/tmp","tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}' \
  | as_deck env DECK_URL="$HOOK_URL" node "${HOOKS_DIR}/cc-approve.js" 2>/dev/null || true)"
case "$HOOK_SAID" in
  *"deck unreachable"*|*"timed out"*|*"bad DECK_URL"*|"")
    check "hook reaches the deck" no "cc-approve.js at ${HOOK_URL} said: \
${HOOK_SAID:-nothing}. Every approval from a desk this box hires is lost and \
the desk waits on a modal nobody can clear." ;;
  *)
    check "hook reaches the deck" ok "cc-approve.js at ${HOOK_URL} got an answer" ;;
esac

# 4. the address the daemon itself hands to the desks it hires, computed the way
#    the daemon computes it rather than assumed from the unit.
DERIVED_URL="$(cd "$REPO_DIR" && as_deck env DECK_URL="$HOOK_URL" \
  "$VENV_PY" -c 'from server import approval; print(approval.deck_url())' 2>/dev/null || true)"
[ "$DERIVED_URL" = "$HOOK_URL" ] \
  && check "hired desks are pointed" ok "approval.deck_url() -> ${DERIVED_URL}" \
  || check "hired desks are pointed" no "approval.deck_url() -> ${DERIVED_URL:-nothing}, \
but the unit says ${HOOK_URL}"

# 5. the CLI runs as the user that will run it.
CLI_VERSION="$(as_deck "$CLAUDE_BIN" --version 2>/dev/null | head -n 1 || true)"
[ -n "$CLI_VERSION" ] \
  && check "the CLI runs" ok "${CLI_VERSION}" \
  || check "the CLI runs" no "${CLAUDE_BIN} --version produced nothing as ${DECK_USER}"

# 5b. A LOGIN SHELL can authenticate, which is a different question from "the
#     service can" and was measured false on this box while every other check
#     passed. `su -` is the point: it is what makes the profile run, and the
#     profile is where the token now comes from. --version does NOT touch the
#     credential, so this asks for something that does.
#
#     The token is never printed. `claude auth` is read-only and spends nothing;
#     the runbook line at the end still asks for one real `-p` round trip,
#     because only that proves the session is live rather than merely present.
LOGIN_SAID="$(su - "$DECK_USER" -c "${CLAUDE_BIN} --version" 2>&1 | tail -n 1 || true)"
case "$LOGIN_SAID" in
  *"OAuth"*|*"authenticate"*|*"not logged in"*|"")
    check "a login shell authenticates" no "su - ${DECK_USER} -c claude said: \
${LOGIN_SAID:-nothing}. The service will work and every hands-on command will \
not -- check that ${PROFILE} sources ${OAUTH_ENV} with 'set -a'." ;;
  *)
    check "a login shell authenticates" ok "su - ${DECK_USER}: ${LOGIN_SAID}" ;;
esac

# 6. CAN THIS BOX TELL ANYONE ANYTHING? Only asked on a box whose deck.toml
#    says doctor.whatsapp = true (a migrated box: when agentdeck.env already
#    names a bridge). Proved by reaching the bridge, not by stat-ing a file. A
#    path check passes on a box that can reach nothing, and the deck then pages
#    nobody, silently -- the same false-pass shape as the hook that posted into
#    a closed socket.
#
#    There is no bridge on the box and there cannot easily be one: pairing is
#    interactive, and a second Baileys client on the same number gets the
#    number flagged. Its only route is a Mac's bridge across the tunnel, so
#    the deck talks HTTP to DECK_WA_URL rather than shelling out to a local
#    wa-send.js that is not there. MEASURED on the first box: the tunnel was
#    fine and the firewall was not the cause -- the Mac's bridge listened on
#    127.0.0.1 only (BOT_HOST=127.0.0.1 in its .env).
if [ "$WHATSAPP" = "1" ]; then
  WA_URL="$(sed -n 's/^DECK_WA_URL=//p' "$ENV_FILE" | head -n 1)"
  if [ -z "$WA_URL" ]; then
    say "  NOTE  whatsapp               DECK_WA_URL is unset and there is no \
${DECK_HOME}/Projects/comunicate_with_me/bin/wa-send.js, so this box CANNOT \
page anyone at all. Nobody is told what happens here unless they are looking at \
the board."
    say "        To give it one, on the MAC (not from here -- that bridge sends as \
its owner's WhatsApp and binding it wider is the owner's call):"
    say "          1. set BOT_HOST=<the Mac's tunnel address> in \
~/Projects/comunicate_with_me/.env and restart the bridge"
    say "          2. put DECK_WA_URL=http://<the Mac's tunnel address>:7799 and the \
bridge's BOT_TOKEN as DECK_WA_TOKEN into ${ENV_FILE}"
    say "        Rollback is one line: put BOT_HOST back to 127.0.0.1 and restart."
  else
    WA_STATUS="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer $(sed -n 's/^DECK_WA_TOKEN=//p' "$ENV_FILE" | head -n 1)" \
      "${WA_URL}/status" 2>/dev/null || echo 000)"
    [ "$WA_STATUS" = "200" ] \
      && check "the box can page him" ok "${WA_URL}/status -> 200" \
      || check "the box can page him" no "${WA_URL}/status -> ${WA_STATUS}. This \
box CANNOT reach him: a blocked agent waits and nobody is told."
  fi
fi

# 6b. the agents' computer, exercised rather than tagged. `docker images |
#     grep` proves a tag exists; it does not prove a container starts, paints a
#     screen, or runs a command. Start -> frame -> exec -> stop, then assert it
#     really stopped, so verification does not leak a container per install.
if [ "$WITH_DOCKER" = "1" ] && command -v docker >/dev/null 2>&1; then
  # `start`/`is_up`/`frame`/`stop` all take a DESK NAME, and `exec_argv` is
  # pure -- it builds the `docker exec` argv and runs nothing -- so the exec
  # leg has to be run here rather than awaited from it. Read off
  # server/sandbox.py; getting this wrong would make the check fail on a box
  # whose computer works perfectly.
  COMPUTER_SAID="$(cd "$REPO_DIR" && as_deck "$VENV_PY" - <<'PY' 2>&1 | tail -n 1
import subprocess
from server import sandbox

DESK = "install-verify"
try:
    sandbox.start(DESK)
    up = sandbox.is_up(DESK)
    shot = sandbox.frame(DESK)
    ran = subprocess.run(sandbox.exec_argv(DESK, ["uname", "-sr"]),
                         capture_output=True, timeout=30)
    kernel = ran.stdout.decode("utf-8", "replace").strip()
finally:
    sandbox.stop(DESK)
print(f"up={up} frame={len(shot or b'')}B exec={kernel[:40]!r} "
      f"stopped={not sandbox.is_up(DESK)}")
PY
)"
  case "$COMPUTER_SAID" in
    *"up=True"*"stopped=True"*)
      check "an agent gets a computer" ok "$COMPUTER_SAID" ;;
    *)
      check "an agent gets a computer" no "start/frame/exec/stop did not \
complete: ${COMPUTER_SAID:-nothing}. A desk that asks for a computer stalls." ;;
  esac
fi

# 6c. THE EDGE, from the outside in: through caddy on 443 with this box's own
#     name. /v1 must reach the deck; the board and /api must NOT (404 from
#     caddy). /healthz is the unauthenticated liveness route (K3); a server
#     older than the pairing work answers 404 there, which is named, not failed.
if [ "$HAS_EDGE" = "1" ]; then
  # A name is pinned to loopback (DNS may not hairpin); an IP is dialled as it
  # is, because with no SNI caddy picks the certificate by the arrival address
  # (render.py _edge_resolve).
  EDGE_CURL=(curl -sS --max-time 10 -o /dev/null -w '%{http_code}')
  [ -z "$EDGE_RESOLVE" ] || EDGE_CURL+=(--resolve "$EDGE_RESOLVE")
  # Self-signed is trusted by pin, not by a CA; the pin was just computed from
  # the key caddy serves, so -k here skips only the chain, not the check.
  [ "$NET_TLS" != "self-signed" ] || EDGE_CURL+=(-k)
  EDGE="https://${NET_HOSTNAME}"
  EDGE_V1="$("${EDGE_CURL[@]}" -H "Authorization: Bearer ${TOKEN}" "${EDGE}/v1/agents" 2>/dev/null || true)"
  [ "$EDGE_V1" = "200" ] \
    && check "edge forwards /v1" ok "${EDGE}/v1/agents -> 200" \
    || check "edge forwards /v1" no "${EDGE}/v1/agents -> ${EDGE_V1:-nothing}; journalctl -u caddy -n 50"
  EDGE_API="$("${EDGE_CURL[@]}" -H "Authorization: Bearer ${TOKEN}" "${EDGE}/api/state" 2>/dev/null || true)"
  [ "$EDGE_API" = "404" ] \
    && check "edge hides the board" ok "${EDGE}/api/state -> 404" \
    || check "edge hides the board" no "${EDGE}/api/state -> ${EDGE_API:-nothing}: the \
board and /api must never be public"
  EDGE_HZ="$("${EDGE_CURL[@]}" "${EDGE}/healthz" 2>/dev/null || true)"
  case "$EDGE_HZ" in
    200) check "edge health" ok "${EDGE}/healthz -> 200" ;;
    404) say "  NOTE  ${EDGE}/healthz -> 404: this server predates the /healthz route" ;;
    *)   check "edge health" no "${EDGE}/healthz -> ${EDGE_HZ:-nothing}" ;;
  esac
fi

# 7. the neighbours are exactly as we found them. Not a formality: the one way
#    this script can do real damage is to another service on a shared box, and
#    it is cheaper to prove it did not than to discover it tomorrow. Same holder
#    on every reserved port as before the install (the control taken at the top).
if [ -n "$RESERVED_PORTS" ]; then
  RESERVED_AFTER=""
  for rp in $RESERVED_PORTS; do
    RP_HOLDER="$(port_holder "$rp")" || true
    RESERVED_AFTER="${RESERVED_AFTER} ${rp}=${RP_HOLDER:-free}"
  done
  [ "$RESERVED_BEFORE" = "$RESERVED_AFTER" ] \
    && check "neighbours untouched" ok "reserved ports held as before:${RESERVED_AFTER}" \
    || check "neighbours untouched" no "reserved ports changed hands:${RESERVED_BEFORE} \
->${RESERVED_AFTER} -- investigate before anything else"
fi

[ -z "$FAILED" ] || die "verification failed:${FAILED}
The unit is running. Do not trust the board until the above is fixed -- a green
card with a broken hook is the failure this phase exists to make visible."

say "verified"
say "Agent Deck is serving on ${DECK_BASE}/ and will restart on boot"
say "the token is in ${ENV_FILE} -- read it there, do not paste it into a message"
say "one thing a script cannot prove: that the login is still live. Spend a few \
tokens once:  sudo -u ${DECK_USER} env HOME=${DECK_HOME} ${CLAUDE_BIN} -p 'reply with the single word ok'"

# The doctor's full check list and the pairing code are deckctl's (K5, K7);
# the installer only calls them. A release without deckctl says so.
if [ -n "$DECKCTL" ]; then
  "$DECKCTL" doctor || say "  NOTE  the doctor has findings -- each line above says what to do"
  if [ "$HAS_EDGE" = "1" ]; then
    "$DECKCTL" pair || say "  NOTE  no pairing code yet:  sudo deckctl pair"
  fi
elif [ "$HAS_EDGE" = "1" ]; then
  say "  NOTE  this release has no deckctl yet, so no pairing code is printed. The \
app can use the master token in ${ENV_FILE} with https://${NET_HOSTNAME}."
fi
