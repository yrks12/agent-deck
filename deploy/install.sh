#!/usr/bin/env bash
#
# Agent Deck -- the one-line install for a fresh Ubuntu 24.04 box (plan K4):
#
#   curl -fsSL <INSTALL_URL> | sudo bash
#   curl -fsSL <INSTALL_URL> | sudo bash -s -- --yes --tls=domain --hostname deck.example.com
#
# Or read it first (recommended):  curl -fsSLO <INSTALL_URL>; less install.sh; sudo bash install.sh
#
# This file is short on purpose: it is what you are asked to trust unread. It
# only fetches the release manifest, downloads the server tarball, REFUSES it
# unless its sha256 matches the manifest, unpacks it to
# /opt/agent-deck/releases/<version>, points /opt/agent-deck/current at it, and
# hands every flag to current/deploy/install-deck.sh, which does the install.
#
# Re-running it is safe: an unpacked release is reused, and `current` is never
# moved to a newer release here -- that is `deckctl update`, which can roll back.

set -euo pipefail

# Filled in when this file is published (Owner decision D2); DECK_RELEASE_BASE wins.
DEFAULT_RELEASE_BASE=""

main() {
  local base prefix os_release tmp manifest version url want got rel cur tty_ok arg
  base="${DECK_RELEASE_BASE:-$DEFAULT_RELEASE_BASE}"
  prefix="${DECK_PREFIX:-/opt/agent-deck}"
  os_release="${DECK_OS_RELEASE:-/etc/os-release}"

  say() { printf 'agent-deck: %s\n' "$*"; }
  die() { printf 'agent-deck: %s\n' "$*" >&2; exit 1; }

  # ── refuse before touching anything ──────────────────────────────────────
  [ "$(id -u)" = "0" ] || die "run me as root:  curl -fsSL <url> | sudo bash"
  [ -r "$os_release" ] || die "cannot read ${os_release}; this installer is for Ubuntu 24.04"
  local os_id os_ver
  os_id="$(. "$os_release" && printf '%s' "${ID:-}")"
  os_ver="$(. "$os_release" && printf '%s' "${VERSION_ID:-}")"
  [ "$os_id" = "ubuntu" ] || die "this is ${os_id:-an unknown system}; Shaliach installs on Ubuntu 24.04"
  case "$os_ver" in
    24.04) ;;
    22.04|20.04) die "Ubuntu ${os_ver} ships Python older than 3.11, which Shaliach needs. Use Ubuntu 24.04." ;;
    *) die "Ubuntu ${os_ver} is not supported yet (needs Python 3.11+); use Ubuntu 24.04." ;;
  esac
  case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) die "unsupported CPU $(uname -m); Agent Deck runs on x86_64 and arm64" ;;
  esac
  [ -n "$base" ] || die "no release location: set DECK_RELEASE_BASE=<url> (this copy was not published with one)"
  for arg in curl python3 tar sha256sum; do
    command -v "$arg" >/dev/null 2>&1 || die "needs ${arg}:  apt-get install -y ${arg/sha256sum/coreutils}"
  done

  # Prompts need a terminal. Under `curl | bash` stdin is the script itself, so
  # the installer gets /dev/tty; with none at all, --yes is required.
  tty_ok=0
  if [ -t 0 ] || { : </dev/tty; } 2>/dev/null; then tty_ok=1; fi
  if [ "$tty_ok" = "0" ]; then
    case " $* " in *" --yes "*|*" -y "*) ;; *) die "no terminal to ask questions on: re-run with --yes (and --claude-token-file / --skip-login)" ;; esac
  fi

  # ── the manifest and the tarball, verified ───────────────────────────────
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 "${base%/}/manifest.json" -o "$tmp/manifest.json" \
    || die "could not fetch ${base%/}/manifest.json"
  manifest="$(python3 - "$tmp/manifest.json" <<'PY'
import json, re, sys
m = json.load(open(sys.argv[1]))
v, s = m["version"], m["server"]
assert re.fullmatch(r"\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?", v), v
assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"]), "sha256"
print(v, s["url"], s["sha256"])
PY
)" || die "the release manifest at ${base%/}/manifest.json is not one I can read"
  read -r version url want <<<"$manifest"
  rel="${prefix}/releases/${version}"
  cur="${prefix}/current"

  if [ -f "${rel}/.release-sha256" ] && [ "$(cat "${rel}/.release-sha256")" = "$want" ]; then
    say "release ${version} is already unpacked at ${rel}"
  else
    say "downloading Shaliach ${version}"
    curl -fsSL --retry 3 "$url" -o "$tmp/server.tar.gz" || die "could not download ${url}"
    got="$(sha256sum "$tmp/server.tar.gz" | cut -d' ' -f1)"
    [ "$got" = "$want" ] || die "sha256 mismatch for ${url}: the manifest says ${want}, \
the download is ${got}. Nothing was installed."
    mkdir -p "${prefix}/releases"
    rm -rf "${rel}.partial"
    mkdir -p "${rel}.partial"
    tar -xzf "$tmp/server.tar.gz" -C "${rel}.partial" --strip-components=1 --no-same-owner \
      || die "the release tarball did not unpack"
    printf '%s\n' "$want" > "${rel}.partial/.release-sha256"
    rm -rf "$rel"
    mv "${rel}.partial" "$rel"
    say "verified (sha256) and unpacked to ${rel}"
  fi

  if [ -e "$cur" ] || [ -L "$cur" ]; then
    if [ "$(readlink -f "$cur")" != "$(readlink -f "$rel")" ]; then
      say "an install already exists ($(readlink "$cur")); leaving it where it is."
      say "to move to ${version}, run:  sudo deckctl update"
    fi
  else
    ln -sfn "$rel" "${cur}.new"
    mv -T "${cur}.new" "$cur" 2>/dev/null || mv "${cur}.new" "$cur"
    say "${cur} -> ${rel}"
  fi

  [ -x "${cur}/deploy/install-deck.sh" ] || die "${cur}/deploy/install-deck.sh is missing; the release is incomplete"
  rm -rf "$tmp"
  trap - EXIT
  if [ -t 0 ] || [ "$tty_ok" = "0" ]; then
    exec "${cur}/deploy/install-deck.sh" "$@"
  fi
  exec "${cur}/deploy/install-deck.sh" "$@" </dev/tty
}

# Everything is inside main() and called on the last line, so `curl | bash`
# has read the whole file before any of it runs.
main "$@"
