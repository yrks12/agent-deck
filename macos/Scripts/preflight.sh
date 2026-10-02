#!/bin/bash
# SCREEN: none — reads a built bundle, never starts it.
#
# Gate G-static of docs/plans/2026-09-30-submission-ready.md: every submission
# requirement that can be checked on this Mac, without Apple, before an upload.
#
#   Scripts/preflight.sh direct|appstore [path/to/Agent Deck.app]
#
# Default bundle: macos/App/build/<channel>/Agent Deck.app (what
# build-release.sh leaves behind). Prints one line per check:
#
#   PASS <check>  <detail>
#   FAIL <check>  <detail>     a real submission blocker
#   WARN <check>  <detail>     expected until the owner's Apple account exists
#
# Exit 0 = no FAIL, 1 = at least one FAIL, 2 = usage. Never prints secrets:
# it reads only the bundle, its signature and its entitlements.
#
# DECK_EXPECT_VERSION overrides the version the bundle must carry (default:
# the repo-root VERSION file).
set -uo pipefail

usage() {
  echo "usage: $0 direct|appstore [path/to/Agent Deck.app]" >&2
  exit 2
}

CHANNEL="${1:-}"
case "$CHANNEL" in direct|appstore) ;; *) usage ;; esac

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACOS="$(cd "$SCRIPTS/.." && pwd)"
APP="${2:-$MACOS/App/build/$CHANNEL/Agent Deck.app}"
EXPECT_VERSION="${DECK_EXPECT_VERSION:-$(tr -d '[:space:]' < "$MACOS/../VERSION" 2>/dev/null)}"

FAILS=0; WARNS=0; PASSES=0
pass() { printf 'PASS %-18s %s\n' "$1" "$2"; PASSES=$((PASSES + 1)); }
fail() { printf 'FAIL %-18s %s\n' "$1" "$2"; FAILS=$((FAILS + 1)); }
warn() { printf 'WARN %-18s %s\n' "$1" "$2"; WARNS=$((WARNS + 1)); }

echo "preflight: channel=$CHANNEL"
echo "preflight: bundle=$APP"

finish() {
  echo "preflight $CHANNEL: $PASSES passed, $FAILS failed, $WARNS warnings"
  [ "$FAILS" -eq 0 ] && exit 0 || exit 1
}

INFO="$APP/Contents/Info.plist"
. "$MACOS/Scripts/bundle-id.sh"
if [ ! -d "$APP" ] || [ ! -f "$INFO" ]; then
  fail bundle "no app bundle with a Contents/Info.plist at $APP (run Scripts/build-release.sh $CHANNEL)"
  finish
fi
pass bundle "found"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# plist <file> <key>: the raw value, or empty when absent.
plist() { plutil -extract "$2" raw -o - "$1" 2>/dev/null; }

# ── Info.plist (contract C2) ────────────────────────────────────────────────
problems=()
expect_value() {  # key wanted-value
  local got; got="$(plist "$INFO" "$1")"
  [ "$got" = "$2" ] || problems+=("$1=${got:-<missing>} (want $2)")
}
expect_present() {  # key
  local got; got="$(plist "$INFO" "$1")"
  if [ -z "$got" ]; then problems+=("$1 <missing>")
  elif [[ "$got" == *'$('* ]]; then problems+=("$1 unexpanded: $got")
  fi
}
expect_value CFBundlePackageType APPL
expect_value CFBundleIdentifier "$DECK_BUNDLE_ID"
expect_value LSMinimumSystemVersion 14.0
expect_value LSApplicationCategoryType public.app-category.developer-tools
expect_value ITSAppUsesNonExemptEncryption false
expect_value NSSupportsAutomaticTermination false
expect_value NSSupportsSuddenTermination false
for key in CFBundleName CFBundleDisplayName CFBundleExecutable \
           NSHumanReadableCopyright NSMicrophoneUsageDescription \
           NSSpeechRecognitionUsageDescription NSLocalNetworkUsageDescription; do
  expect_present "$key"
done
if [ ${#problems[@]} -eq 0 ]; then
  pass plist-keys "every C2 key present with its required value"
else
  fail plist-keys "$(IFS=';'; echo "${problems[*]}")"
fi

# ── Version ─────────────────────────────────────────────────────────────────
short="$(plist "$INFO" CFBundleShortVersionString)"
build="$(plist "$INFO" CFBundleVersion)"
vproblems=()
[ "$short" = "$EXPECT_VERSION" ] || vproblems+=("CFBundleShortVersionString=${short:-<missing>}, repo VERSION=$EXPECT_VERSION")
[[ "$build" =~ ^[0-9]+$ ]] || vproblems+=("CFBundleVersion=${build:-<missing>} is not a monotonic integer")
if [ ${#vproblems[@]} -eq 0 ]; then
  pass version "$short ($build)"
else
  fail version "$(IFS=';'; echo "${vproblems[*]}")"
fi

# ── Privacy manifest ────────────────────────────────────────────────────────
PRIV="$APP/Contents/Resources/PrivacyInfo.xcprivacy"
if [ ! -f "$PRIV" ]; then
  fail privacy-manifest "Contents/Resources/PrivacyInfo.xcprivacy missing"
elif ! plutil -lint -s "$PRIV" >/dev/null 2>&1; then
  fail privacy-manifest "PrivacyInfo.xcprivacy is not a valid plist"
elif [ "$(plist "$PRIV" NSPrivacyTracking)" != "false" ]; then
  fail privacy-manifest "NSPrivacyTracking is not false"
else
  pass privacy-manifest "present, tracking=false"
fi

# ── Icon ────────────────────────────────────────────────────────────────────
icon_name="$(plist "$INFO" CFBundleIconName)"
icon_file="$(plist "$INFO" CFBundleIconFile)"
if [ -n "$icon_name" ] && [ -f "$APP/Contents/Resources/Assets.car" ]; then
  pass icon "asset catalog icon '$icon_name'"
elif [ -n "$icon_file" ] && [ -f "$APP/Contents/Resources/${icon_file%.icns}.icns" ]; then
  pass icon "icns '$icon_file'"
else
  fail icon "no CFBundleIconName + Assets.car (or CFBundleIconFile + .icns) in the bundle"
fi

# ── Architectures ───────────────────────────────────────────────────────────
EXE="$APP/Contents/MacOS/$(plist "$INFO" CFBundleExecutable)"
if [ ! -f "$EXE" ]; then
  fail arch "main executable missing: $EXE"
else
  archs="$(lipo -archs "$EXE" 2>/dev/null)"
  missing=()
  for a in arm64 x86_64; do [[ " $archs " == *" $a "* ]] || missing+=("$a"); done
  if [ ${#missing[@]} -eq 0 ]; then
    pass arch "universal ($archs)"
  else
    fail arch "has [$archs], missing ${missing[*]} (universal arm64 + x86_64 required)"
  fi
fi

# ── Signature ───────────────────────────────────────────────────────────────
if verify="$(codesign --verify --deep --strict "$APP" 2>&1)"; then
  pass signature "codesign --verify --deep --strict ok"
else
  fail signature "$(echo "$verify" | head -2 | tr '\n' ' ')"
fi

details="$(codesign -dv --verbose=2 "$APP" 2>&1)"
authority="$(echo "$details" | sed -n 's/^Authority=//p' | head -1)"
team="$(echo "$details" | sed -n 's/^TeamIdentifier=//p' | head -1)"
case "$CHANNEL" in
  direct)   want_authority="Developer ID Application" ;;
  appstore) want_authority="Apple Distribution" ;;
esac
if [[ "$authority" == "$want_authority"* || ( "$CHANNEL" = appstore && "$authority" == "3rd Party Mac Developer Application"* ) ]] \
   && [ -n "$team" ] && [ "$team" != "not set" ]; then
  pass identity "$authority (team $team)"
else
  warn identity "signed by '${authority:-ad-hoc}', team '${team:-not set}'; a real upload needs '$want_authority' (owner decision D1)"
fi

# ── Entitlements ────────────────────────────────────────────────────────────
ENT="$TMP/ent.plist"
codesign -d --entitlements - --xml "$APP" > "$ENT" 2>/dev/null
[ -s "$ENT" ] || printf '<?xml version="1.0"?><plist version="1.0"><dict/></plist>' > "$ENT"
# Top-level keys of the entitlements dict.
# (Entitlement values are booleans or string arrays, so every `"x":` is a key.)
ent_keys="$(plutil -convert json -o - "$ENT" 2>/dev/null | grep -o '"[^"]*":' | tr -d '":')"
# PlistBuddy, not plutil: plutil reads the dots in "com.apple.security.x" as a key path.
has_ent() { [ "$(/usr/libexec/PlistBuddy -c "Print :$1" "$ENT" 2>/dev/null)" = "true" ]; }

if has_ent com.apple.security.get-task-allow; then
  fail get-task-allow "com.apple.security.get-task-allow is set: debuggable, notarization and App Review reject it"
else
  pass get-task-allow "absent"
fi

case "$CHANNEL" in
  direct)
    allowed=(com.apple.security.device.audio-input)
    required=(com.apple.security.device.audio-input) ;;
  appstore)
    allowed=(com.apple.security.app-sandbox com.apple.security.network.client
             com.apple.security.device.audio-input com.apple.security.files.user-selected.read-only)
    required=("${allowed[@]}") ;;
esac
extra=(); absent=()
while IFS= read -r key; do
  [ -z "$key" ] && continue
  [ "$key" = com.apple.security.get-task-allow ] && continue
  [[ " ${allowed[*]} " == *" $key "* ]] || extra+=("$key")
done <<< "$ent_keys"
for key in "${required[@]}"; do has_ent "$key" || absent+=("$key"); done
if [ ${#extra[@]} -eq 0 ] && [ ${#absent[@]} -eq 0 ]; then
  pass entitlements "exactly the $CHANNEL set (${required[*]##*.security.})"
else
  msg=""
  [ ${#absent[@]} -gt 0 ] && msg+="missing: ${absent[*]}. "
  [ ${#extra[@]} -gt 0 ] && msg+="not allowed for $CHANNEL: ${extra[*]}"
  fail entitlements "$msg"
fi

sandboxed=no; has_ent com.apple.security.app-sandbox && sandboxed=yes
case "$CHANNEL:$sandboxed" in
  appstore:yes) pass sandbox "App Sandbox on (App Review 2.4.5(i))" ;;
  appstore:no)  fail sandbox "App Sandbox is OFF; the Mac App Store requires it" ;;
  direct:no)    pass sandbox "off (the Mac bridge needs the real shell)" ;;
  direct:yes)   fail sandbox "App Sandbox is ON in the direct build; the Mac bridge cannot run sandboxed" ;;
esac

# ── Hardened runtime ────────────────────────────────────────────────────────
flags="$(echo "$details" | sed -n 's/.*flags=\(0x[0-9a-f]*([^)]*)\).*/\1/p' | head -1)"
if [[ "$flags" == *runtime* ]]; then
  pass hardened-runtime "$flags"
elif [ "$CHANNEL" = direct ]; then
  fail hardened-runtime "flags=${flags:-none}: notarization requires --options runtime"
else
  pass hardened-runtime "off ($flags); not required under the App Sandbox"
fi

# ── Code the App Store build must not contain ──────────────────────────────
# Every Mach-O in the bundle (the executable, Xcode's debug dylib, frameworks).
found=()
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q 'Mach-O' || continue
  syms="$(nm -u "$f" 2>/dev/null)"
  for s in _posix_spawn _posix_spawnp _fork _execve _execvp '_OBJC_CLASS_$_NSTask'; do
    grep -qxF "$s" <<< "$syms" && found+=("${s#_}")
  done
  strs="$(strings -a "$f" 2>/dev/null)"
  for s in sandbox-exec /usr/sbin/screencapture; do
    grep -qF "$s" <<< "$strs" && found+=("\"$s\"")
  done
done < <(find "$APP/Contents" -type f \( -path '*/MacOS/*' -o -path '*/Frameworks/*' \) -print0)
uniq_found="$(printf '%s\n' "${found[@]+"${found[@]}"}" | sed '/^$/d' | sort -u | tr '\n' ' ')"
if [ "$CHANNEL" = direct ]; then
  pass forbidden-symbols "not restricted for direct${uniq_found:+ (uses: $uniq_found)}"
elif [ -z "$uniq_found" ]; then
  pass forbidden-symbols "no process-spawning or sandbox-exec code in the binary"
else
  fail forbidden-symbols "App Store binary references: $uniq_found(App Review 2.5.2; slice X2 moves the Mac bridge executor out of DeckKit)"
fi

finish
