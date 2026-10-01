#!/bin/bash
# SCREEN: none — builds and signs bundles, never starts one.
#
# Release builds of Agent Deck through Xcode, one per distribution channel
# (contract C1, docs/plans/2026-09-30-submission-ready.md):
#
#   Scripts/build-release.sh direct      AgentDeck     Developer ID, hardened runtime
#   Scripts/build-release.sh appstore    AgentDeckMAS  App Sandbox, Mac App Store
#   Scripts/build-release.sh all         both
#
# Steps per channel:
#   1. xcodegen generate --spec App/project.yml
#   2. xcodebuild archive  (Release, universal arm64 + x86_64,
#      MARKETING_VERSION = repo VERSION, CURRENT_PROJECT_VERSION = commit count)
#   3. sign:
#      - an Apple identity exists (DECK_TEAM_ID set, and "Developer ID
#        Application" / "Apple Distribution" in the keychain; appstore also
#        needs DECK_MAS_PROFILE): xcodebuild -exportArchive with
#        App/ExportOptions-<channel>.plist. Direct gets a signed .app, App Store
#        gets the .pkg for Transporter.
#      - otherwise (today, owner decision D1 open): copy the .app out of the
#        archive and re-sign it with DECK_SIGN_IDENTITY (default: the
#        self-signed "Agent Deck Local" when the keychain has it, else ad-hoc)
#        using the SAME entitlements and hardened-runtime flag the real
#        identity will get. Everything up to notarization is exercised; only
#        the identity differs.
#   4. Scripts/preflight.sh <channel> on the result. Its exit status is this
#      script's exit status.
#
# Output: macos/App/build/<channel>/Agent Deck.app (+ .xcarchive, logs, and
# for an App Store export the .pkg). Gitignored.
#
# Notarization, stapling and the DMG are slice X3; this script stops at a
# signed bundle.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACOS="$(cd "$SCRIPTS/.." && pwd)"
APPDIR="$MACOS/App"
REPO="$(cd "$MACOS/.." && pwd)"

case "${1:-}" in
  direct|appstore) CHANNELS=("$1") ;;
  all) CHANNELS=(direct appstore) ;;
  *) echo "usage: $0 direct|appstore|all" >&2; exit 2 ;;
esac

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "xcodegen is not installed. Install it with: brew install xcodegen" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
. "$MACOS/Scripts/bundle-id.sh"
BUILD_NUMBER="$(git -C "$REPO" rev-list --count HEAD)"
echo "== Agent Deck $VERSION ($BUILD_NUMBER)"

xcodegen generate --quiet --spec "$APPDIR/project.yml"
PROJECT="$APPDIR/AgentDeck.xcodeproj"

has_identity() { security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$1"; }

STATUS=0
for CHANNEL in "${CHANNELS[@]}"; do
  case "$CHANNEL" in
    direct)
      SCHEME=AgentDeck
      ENTITLEMENTS="$APPDIR/AgentDeck-Direct.entitlements"
      APPLE_IDENTITY="Developer ID Application" ;;
    appstore)
      SCHEME=AgentDeckMAS
      ENTITLEMENTS="$APPDIR/AgentDeck-AppStore.entitlements"
      APPLE_IDENTITY="Apple Distribution" ;;
  esac

  OUT="$APPDIR/build/$CHANNEL"
  ARCHIVE="$OUT/AgentDeck.xcarchive"
  APP="$OUT/Agent Deck.app"
  rm -rf "$OUT"; mkdir -p "$OUT"

  echo "== [$CHANNEL] archive $SCHEME (log: $OUT/archive.log)"
  if ! xcodebuild archive \
      -project "$PROJECT" -scheme "$SCHEME" -configuration Release \
      -destination 'generic/platform=macOS' \
      -archivePath "$ARCHIVE" \
      -derivedDataPath "$APPDIR/build/DerivedData-$CHANNEL" \
      MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
      DECK_BUNDLE_ID="$DECK_BUNDLE_ID" DECK_COPYRIGHT="$DECK_COPYRIGHT" \
      CODE_SIGN_IDENTITY="-" CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM="" \
      > "$OUT/archive.log" 2>&1; then
    grep -E "error:|BUILD FAILED|ARCHIVE FAILED" "$OUT/archive.log" | head -20 >&2 || true
    echo "== [$CHANNEL] archive FAILED" >&2
    STATUS=1; continue
  fi

  real_ok=no
  if [ -n "${DECK_TEAM_ID:-}" ] && has_identity "$APPLE_IDENTITY"; then
    [ "$CHANNEL" = direct ] || [ -n "${DECK_MAS_PROFILE:-}" ] && real_ok=yes
  fi

  if [ "$real_ok" = yes ]; then
    echo "== [$CHANNEL] export with $APPLE_IDENTITY (team $DECK_TEAM_ID)"
    OPTS="$OUT/ExportOptions.plist"
    cp "$APPDIR/ExportOptions-$CHANNEL.plist" "$OPTS"
    plutil -replace teamID -string "$DECK_TEAM_ID" "$OPTS"
    if [ "$CHANNEL" = appstore ]; then
      /usr/libexec/PlistBuddy -c "Add :provisioningProfiles:$DECK_BUNDLE_ID string $DECK_MAS_PROFILE" "$OPTS"
    fi
    xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportPath "$OUT/export" \
      -exportOptionsPlist "$OPTS" > "$OUT/export.log" 2>&1 \
      || { echo "== [$CHANNEL] export FAILED (see $OUT/export.log)" >&2; STATUS=1; continue; }
    if [ -d "$OUT/export/Agent Deck.app" ]; then
      ditto "$OUT/export/Agent Deck.app" "$APP"
    else
      # An App Store export is a .pkg; preflight reads the app inside the archive.
      ditto "$ARCHIVE/Products/Applications/Agent Deck.app" "$APP"
    fi
  else
    IDENTITY="${DECK_SIGN_IDENTITY:-}"
    if [ -z "$IDENTITY" ]; then
      if security find-certificate -c "Agent Deck Local" >/dev/null 2>&1; then
        IDENTITY="Agent Deck Local"
      else
        IDENTITY="-"
      fi
    fi
    echo "== [$CHANNEL] no '$APPLE_IDENTITY' + DECK_TEAM_ID: signing with '$IDENTITY' (same shape, not uploadable)"
    ditto "$ARCHIVE/Products/Applications/Agent Deck.app" "$APP"
    # Inside-out: anything nested first, then the app with its entitlements.
    while IFS= read -r -d '' nested; do
      codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$nested"
    done < <(find "$APP/Contents" \( -name '*.framework' -o -name '*.dylib' -o -name '*.xpc' \) -prune -print0)
    codesign --force --sign "$IDENTITY" --options runtime --timestamp=none \
      --entitlements "$ENTITLEMENTS" "$APP"
  fi

  echo "== [$CHANNEL] preflight"
  "$SCRIPTS/preflight.sh" "$CHANNEL" "$APP" || STATUS=1
done

exit "$STATUS"
