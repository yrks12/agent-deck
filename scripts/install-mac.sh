#!/usr/bin/env bash
#
# Agent Deck for macOS -- the app, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/yrks12/agent-deck/main/scripts/install-mac.sh | bash
#
# With the pairing code your server printed (the app opens with it filled in):
#
#   curl -fsSL .../scripts/install-mac.sh | bash -s -- --pair ADK1.xxxxxxxx
#
# What it does:
#   1. Takes the latest release .zip when there is one, and refuses it unless its
#      published .sha256 matches. With no release, it builds from source
#      (`make app`; needs Xcode or the Command Line Tools, a few minutes).
#   2. Installs "Agent Deck.app" into /Applications (or --prefix DIR), replacing
#      an older copy in place, and clears macOS's download quarantine flag on it.
#      The app is not notarized; this is the step that lets it open.
#   3. Opens it. With --pair, the Connect screen opens with the code filled in.
#
# Run it again to upgrade. Options:
#   --pair CODE     a one-time pairing code from  sudo deckctl pair  on your server
#   --prefix DIR    install somewhere other than /Applications
#   --from-source   skip the release lookup and build from source
#   --src DIR       build from this checkout (default: this script's checkout,
#                   else ~/agent-deck-src, cloned or fast-forwarded)
#   --no-open       install only
#
# Settings (environment): AGENT_DECK_REPO (owner/repo), AGENT_DECK_REPO_URL (a full
# clone URL), AGENT_DECK_REF (default main).
#
# While the repository is private, downloads need a token:  GH_TOKEN=... (or GITHUB_TOKEN)
#   GH_TOKEN=$(gh auth token) bash scripts/install-mac.sh
# Once it is public no token is needed. A pre-release is used when no stable release exists.

set -euo pipefail

AGENT_DECK_REPO="${AGENT_DECK_REPO:-yrks12/agent-deck}"

main() {
  local slug="$AGENT_DECK_REPO"
  local ref="${AGENT_DECK_REF:-main}"
  local url="${AGENT_DECK_REPO_URL:-https://github.com/${slug}.git}"
  local api="${AGENT_DECK_API:-https://api.github.com}"
  local prefix="${AGENT_DECK_PREFIX:-/Applications}"
  local pair="" src="" from_source=0 open_app=1

  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) sed -n '3,33p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; return 0 ;;
      --pair) [ -n "${2:-}" ] || die "--pair needs the code (it starts with ADK1.)"; pair="$2"; shift ;;
      --pair=*) pair="${1#*=}" ;;
      --prefix) [ -n "${2:-}" ] || die "--prefix needs a directory"; prefix="$2"; shift ;;
      --prefix=*) prefix="${1#*=}" ;;
      --src) [ -n "${2:-}" ] || die "--src needs a directory"; src="$2"; shift ;;
      --src=*) src="${1#*=}" ;;
      --from-source) from_source=1 ;;
      --no-open) open_app=0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done

  [ "$(uname -s)" = "Darwin" ] || die "this installs the macOS app, and this is not macOS. \
For a server, use install.sh."
  if [ -n "$pair" ] && ! [[ "$pair" =~ ^ADK[0-9]+\.[A-Za-z0-9_-]+$ ]]; then
    die "that is not a pairing code. Copy the whole line that starts with ADK1. from  sudo deckctl pair"
  fi

  local work app=""
  work="$(mktemp -d)"
  trap "rm -rf '${work}'" EXIT

  # ── 1. a release, verified -- or the source ─────────────────────────────
  if [ "$from_source" = 0 ]; then
    app="$(from_release "$api" "$slug" "$work")" || exit 1
  fi
  if [ -z "$app" ]; then
    [ -n "$src" ] || src="$(find_source "$url" "$ref")"
    command -v swift >/dev/null 2>&1 \
      || die "building the app needs Xcode or the Command Line Tools:  xcode-select --install"
    say "building Agent Deck.app from ${src} (a few minutes the first time)"
    make -C "$src" app >&2
    app="${src}/macos/dist/Agent Deck.app"
    [ -d "$app" ] || die "the build finished but ${app} is not there"
  fi

  # ── 2. install in place, clear the quarantine flag ───────────────────────
  local target="${prefix}/Agent Deck.app" admin=""
  mkdir -p "$prefix" 2>/dev/null || true
  [ -w "$prefix" ] || admin=sudo
  if pgrep -f "${target}/Contents/MacOS/" >/dev/null 2>&1; then
    say "quitting the running copy at ${target} so it can be replaced"
    osascript -e "tell application \"${target}\" to quit" >/dev/null 2>&1 || true
    local n=10
    while [ "$n" -gt 0 ] && pgrep -f "${target}/Contents/MacOS/" >/dev/null 2>&1; do
      sleep 1; n=$((n - 1))
    done
  fi
  $admin rm -rf "${target}.new"
  $admin ditto "$app" "${target}.new"
  $admin rm -rf "$target"
  $admin mv "${target}.new" "$target"
  $admin xattr -dr com.apple.quarantine "$target" 2>/dev/null || true
  say "installed ${target}"

  # ── 3. open it ────────────────────────────────────────────────────────────
  if [ "$open_app" = 1 ]; then
    if [ -n "$pair" ]; then
      open "$target" --args --pair "$pair"
      say "Agent Deck is open on the Connect screen with your code filled in: click Connect."
    else
      open "$target"
      say "Agent Deck is open. To connect it: Connect -> paste the pairing code from your"
      say "server (sudo deckctl pair), or enter the deck's address and token."
    fi
  fi
}

say() { printf 'agent-deck: %s\n' "$*"; }
die() { printf 'agent-deck: %s\n' "$*" >&2; exit 1; }

# from_release <api> <slug> <workdir>: prints the path of a verified, unpacked app,
# or nothing when there is no release yet. Exits non-zero on a bad download.
#
# /releases/latest never returns a pre-release, so while only a pre-release exists it
# 404s; the newest entry of the releases list is used then. A private repository's
# browser_download_url does not accept a token: with GH_TOKEN the files come from the
# asset API (Accept: application/octet-stream) instead. The token is only ever sent as
# a header, never printed.
from_release() {
  local api="$1" slug="$2" work="$3" json zip_name zip_url sum_url want got
  local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  local -a auth=()
  [ -z "$token" ] || auth=(-H "Authorization: token ${token}")
  json="$(curl -fsSL ${auth[@]+"${auth[@]}"} "${api}/repos/${slug}/releases/latest" 2>/dev/null)" \
    || json="$(curl -fsSL ${auth[@]+"${auth[@]}"} "${api}/repos/${slug}/releases?per_page=10" 2>/dev/null)" \
    || json=""
  zip_name="$(asset_field "$json" 1 | grep -E '^AgentDeck-.*-macos\.zip$' | head -n 1 || true)"
  if [ -z "$zip_name" ]; then
    say "no macOS app release found: building from source instead" >&2
    return 0
  fi
  zip_url="$(asset_url "$json" "$zip_name" "$token")"
  sum_url="$(asset_url "$json" "${zip_name}.sha256" "$token")"
  [ -n "$sum_url" ] || die "the release has ${zip_name} but no .sha256 beside it; refusing an unverified download"
  say "downloading ${zip_name}" >&2
  local -a get=(${auth[@]+"${auth[@]}"})
  [ -z "$token" ] || get+=(-H "Accept: application/octet-stream")
  curl -fsSL ${get[@]+"${get[@]}"} -o "${work}/app.zip" "$zip_url" || die "could not download ${zip_name}"
  want="$(curl -fsSL ${get[@]+"${get[@]}"} "$sum_url" | awk '{print $1; exit}')" \
    || die "could not download ${zip_name}.sha256"
  got="$(shasum -a 256 "${work}/app.zip" | awk '{print $1}')"
  [ -n "$want" ] && [ "$want" = "$got" ] \
    || die "sha256 mismatch for ${zip_name}: published ${want:-nothing}, downloaded ${got}. Nothing was installed."
  ditto -x -k "${work}/app.zip" "${work}/unpacked"
  [ -d "${work}/unpacked/Agent Deck.app" ] || die "the release .zip has no Agent Deck.app inside"
  printf '%s\n' "${work}/unpacked/Agent Deck.app"
}

# asset_field <json> <1=name|2=api url|3=browser url>: one line per release asset, in
# the order the API lists them (newest release first). Needs no jq.
asset_field() {
  printf '%s\n' "$1" | grep -oE '"(url|name|browser_download_url)": *"[^"]*"' | sed -E 's/^"([a-z_]+)": *"(.*)"$/\1 \2/' \
    | awk -v col="$2" '
        $1 == "url" && $2 ~ /\/releases\/assets\// { u = $2 }
        $1 == "name" { n = $2 }
        $1 == "browser_download_url" { print (col == 1 ? n : col == 2 ? u : $2) }'
}

# asset_url <json> <name> <token>: where to fetch that asset: the asset API when there is
# a token (works for a private repo), else the public download URL.
asset_url() {
  local names urls i=0 line
  names="$(asset_field "$1" 1)"
  if [ -n "$3" ]; then urls="$(asset_field "$1" 2)"; else urls="$(asset_field "$1" 3)"; fi
  while IFS= read -r line; do
    i=$((i + 1))
    if [ "$line" = "$2" ]; then printf '%s\n' "$urls" | sed -n "${i}p"; return; fi
  done <<EOF
${names}
EOF
}

# find_source <url> <ref>: this script's own checkout, else ~/agent-deck-src.
find_source() {
  local url="$1" ref="$2" here dir dirty
  here="${BASH_SOURCE[0]:-}"
  if [ -f "$here" ]; then
    here="$(cd "$(dirname "$here")/.." && pwd)"
    if [ -f "${here}/macos/Package.swift" ]; then printf '%s\n' "$here"; return; fi
  fi
  dir="${AGENT_DECK_SRC:-$HOME/agent-deck-src}"
  command -v git >/dev/null 2>&1 || die "git is missing:  xcode-select --install"
  if [ -d "${dir}/.git" ]; then
    dirty="$(git -C "$dir" status --porcelain --untracked-files=no)"
    [ -z "$dirty" ] || die "${dir} has local changes; I will not overwrite them (or pass --src DIR)"
    git -C "$dir" fetch -q --depth 1 "$url" "$ref" >&2 || die "could not fetch ${ref} from ${url}"
    git -C "$dir" reset -q --hard FETCH_HEAD >&2
  elif [ -e "$dir" ]; then
    die "${dir} exists and is not a checkout; move it or pass --src DIR"
  else
    say "downloading the source to ${dir}" >&2
    git clone -q --depth 1 --branch "$ref" "$url" "$dir" >&2 || die "could not clone ${url}"
  fi
  printf '%s\n' "$dir"
}

# Everything above is a definition. Nothing runs until this last line, so a
# download cut off half-way (curl | bash) executes nothing at all.
main "$@"
