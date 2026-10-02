#!/usr/bin/env bash
# Install the Agent Deck iPhone app on your own phone with a FREE Apple ID
# (no paid developer account). Plug in and unlock the iPhone, have Xcode
# installed, then:
#
#   curl -fsSL https://raw.githubusercontent.com/yrks12/agent-deck/main/scripts/install-iphone.sh | bash
#
# Options (pass after `bash -s --` when piping):
#   --device ID   device identifier (default: the single connected iPhone)
#   --team ID     Apple developer team id (default: detected from Xcode)
#   --src DIR     where to keep the source checkout (default: ~/agent-deck-src)
#   --dry-run     print the build and install commands instead of running them
#
# A free-team build stops opening after 7 days. Re-run the same command to
# renew it; the bundle id is remembered in ~/.config/agent-deck/iphone.env so a
# re-run never burns a new app id.
set -euo pipefail

AGENT_DECK_REPO="${AGENT_DECK_REPO:-yrks12/agent-deck}"
AGENT_DECK_REF="${AGENT_DECK_REF:-main}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# Run, or (with --dry-run) just print, a command.
run_or_print() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

# Pick the one connected iPhone. Prints its identifier.
find_device() {
  local json="$1" want="$2" rows n
  xcrun devicectl list devices --json-output "$json" >/dev/null 2>&1 \
    || die "could not list devices: is Xcode installed and its license accepted?"
  rows="$(python3 - "$json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for d in data.get("result", {}).get("devices", []):
    hw = d.get("hardwareProperties", {})
    if hw.get("deviceType") != "iPhone":
        continue
    if d.get("connectionProperties", {}).get("tunnelState") != "connected":
        continue
    name = hw.get("marketingName") or d.get("deviceProperties", {}).get("name", "iPhone")
    # xcodebuild -destination id= needs the hardware UDID, not the CoreDevice id.
    print("%s\t%s" % (hw.get("udid") or d.get("identifier", ""), name))
PY
)"
  if [ -n "$want" ]; then
    printf '%s\n' "$want"
    return 0
  fi
  [ -n "$rows" ] || die "no iPhone found. Plug it in with a cable, unlock it, tap Trust, and enable Developer Mode (Settings > Privacy & Security), then run this again."
  n="$(printf '%s\n' "$rows" | wc -l | tr -d ' ')"
  if [ "$n" -gt 1 ]; then
    {
      printf 'error: several iPhones are connected; pick one with --device ID:\n'
      printf '%s\n' "$rows" | awk -F'\t' '{ printf "  %s  %s\n", $1, $2 }'
    } >&2
    exit 1
  fi
  printf '%s\n' "$rows" | cut -f1
}

# Print every distinct team id this Mac can sign for, one per line.
list_teams() {
  {
    defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier 2>/dev/null \
      | sed -n 's/.*teamID = \([A-Z0-9]*\);.*/\1/p' || true
    defaults read com.apple.dt.Xcode IDEProvisioningTeams 2>/dev/null \
      | sed -n 's/.*teamID = \([A-Z0-9]*\);.*/\1/p' || true
  } | sort -u
  local certs ou
  certs="$(security find-certificate -a -c "Apple Development" -p 2>/dev/null || true)"
  [ -n "$certs" ] || return 0
  printf '%s\n' "$certs" | awk -v dir="$1" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = dir "/cert" n ".pem" }
    f { print > f }
    /-----END CERTIFICATE-----/ { close(f); f = "" }'
  for c in "$1"/cert*.pem; do
    [ -f "$c" ] || continue
    ou="$(openssl x509 -noout -subject < "$c" 2>/dev/null | sed -n 's/.*OU=\([A-Z0-9]*\).*/\1/p' || true)"
    [ -z "$ou" ] || printf '%s\n' "$ou"
  done
}

pick_team() {
  local given="$1" tmp="$2" teams n
  if [ -n "$given" ]; then printf '%s\n' "$given"; return 0; fi
  teams="$(list_teams "$tmp" | sort -u | sed '/^$/d')"
  if [ -z "$teams" ]; then
    {
      printf 'error: no Apple developer team found.\n'
      printf 'Add your Apple ID (free is fine) in Xcode > Settings > Accounts, let it\n'
      printf 'create the Personal Team, then run this again. Or pass --team TEAMID.\n'
    } >&2
    exit 1
  fi
  n="$(printf '%s\n' "$teams" | wc -l | tr -d ' ')"
  if [ "$n" -gt 1 ]; then
    {
      printf 'error: several teams found; choose one with --team TEAMID:\n'
      printf '%s\n' "$teams" | sed 's/^/  /'
    } >&2
    exit 1
  fi
  printf '%s\n' "$teams"
}

# Resolve the source tree: the checkout this script lives in, else a clone.
resolve_src() {
  local src="$1" here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"
  if [ -z "$src" ] && [ -n "$here" ] && [ -f "$here/../ios/project.yml" ]; then
    (cd "$here/.." && pwd)
    return 0
  fi
  src="${src:-$HOME/agent-deck-src}"
  if [ -d "$src/.git" ]; then
    git -C "$src" fetch --quiet origin "$AGENT_DECK_REF" >&2
    git -C "$src" merge --ff-only --quiet FETCH_HEAD >&2
  else
    git clone --quiet --branch "$AGENT_DECK_REF" "https://github.com/$AGENT_DECK_REPO.git" "$src" >&2
  fi
  printf '%s\n' "$src"
}

main() {
  local device="" team="" src="" DRY_RUN=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --device) [ $# -ge 2 ] || die "--device needs a value"; device="$2"; shift 2 ;;
      --team)   [ $# -ge 2 ] || die "--team needs a value"; team="$2"; shift 2 ;;
      --src)    [ $# -ge 2 ] || die "--src needs a value"; src="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; return 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done

  command -v xcrun >/dev/null 2>&1 || die "Xcode is required (install it from the App Store, then open it once)."
  command -v xcodebuild >/dev/null 2>&1 || die "xcodebuild not found: install Xcode."
  command -v git >/dev/null 2>&1 || die "git is required."
  command -v xcodegen >/dev/null 2>&1 || die "xcodegen is required: brew install xcodegen"
  command -v python3 >/dev/null 2>&1 || die "python3 is required."

  local work
  work="$(mktemp -d "${TMPDIR:-/tmp}/agent-deck-iphone.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$work'" EXIT

  device="$(find_device "$work/devices.json" "$device")"
  team="$(pick_team "$team" "$work")"
  src="$(resolve_src "$src")"

  # Free teams cannot reuse a bundle id another account registered, so each
  # team gets its own suffix, remembered so a re-run renews instead of
  # registering a second app id.
  local cfg="$HOME/.config/agent-deck/iphone.env" suffix=""
  if [ -f "$cfg" ]; then
    local saved_team saved_suffix
    saved_team="$(sed -n 's/^IPHONE_TEAM=//p' "$cfg" | tail -1)"
    saved_suffix="$(sed -n 's/^IPHONE_BUNDLE_SUFFIX=//p' "$cfg" | tail -1)"
    if [ "$saved_team" = "$team" ] && [ -n "$saved_suffix" ]; then suffix="$saved_suffix"; fi
  fi
  if [ -z "$suffix" ]; then
    suffix="u$(printf '%s' "$team" | tr '[:upper:]' '[:lower:]')"
    mkdir -p "$(dirname "$cfg")"
    printf 'IPHONE_TEAM=%s\nIPHONE_BUNDLE_SUFFIX=%s\n' "$team" "$suffix" > "$cfg"
  fi
  local bundle="dev.agentdeck.$suffix"

  say "iPhone:    $device"
  say "Team:      $team"
  say "Bundle id: $bundle.ios"

  cd "$src"
  DECK_BUNDLE_ID="$bundle" xcodegen generate --spec ios/project.yml

  local derived="$HOME/.cache/agent-deck/iphone-derived"
  local app="$derived/Build/Products/Debug-iphoneos/Agent Deck.app"
  run_or_print xcodebuild \
    -project ios/AgentDeckPhone.xcodeproj -scheme AgentDeckPhone \
    -configuration Debug -sdk iphoneos \
    -destination "id=$device" -derivedDataPath "$derived" \
    -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
    -skipPackagePluginValidation -skipMacroValidation \
    DEVELOPMENT_TEAM="$team" PRODUCT_BUNDLE_IDENTIFIER="$bundle.ios" \
    DECK_BUNDLE_ID="$bundle" CODE_SIGN_STYLE=Automatic \
    CODE_SIGN_IDENTITY="Apple Development" \
    build
  if [ "$DRY_RUN" != 1 ] && [ ! -d "$app" ]; then
    die "build finished but $app is missing."
  fi
  run_or_print xcrun devicectl device install app --device "$device" "$app"

  say ""
  if [ "$DRY_RUN" = 1 ]; then
    say "Dry run: nothing was built or installed. A real run ends with:"
  else
    say "Installed. One-time step on the iPhone:"
  fi
  say "  Settings > General > VPN & Device Management > your Apple ID > Trust."
  say ""
  say "A free Apple ID signs the app for 7 days; after that it stops opening."
  say "To renew, re-run the same command: it reuses the same app id and signs it"
  say "for another 7 days. Your pairing and settings are kept."
}

main "$@"
