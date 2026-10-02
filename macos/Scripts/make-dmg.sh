#!/bin/bash
# SCREEN: none — builds a disk image, never starts the app.
#
# Packs "Agent Deck.app" + an Applications symlink into a DMG.
#
#   Scripts/make-dmg.sh [debug|release]
#
# Output: dist/AgentDeck-<VERSION>.dmg   (VERSION = repo-root VERSION file)
#
# Signing has two paths, chosen by environment, no code change:
#
#   FREE (default)  make-app-bundle.sh signs with DECK_SIGN_IDENTITY
#                   (default "Agent Deck Local", self-signed). Gatekeeper will
#                   warn on a friend's Mac; that is expected for a build that is not notarized.
#
#   PAID            set DEVELOPER_ID ("Developer ID Application: Name (TEAM)")
#                   and NOTARY_PROFILE (a `xcrun notarytool store-credentials`
#                   profile). The app is re-signed with hardened runtime, the
#                   DMG is signed, notarized, and stapled. Inert unless BOTH
#                   are set; with only one of them the script refuses.
set -euo pipefail

CONFIG="${1:-release}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/../VERSION")"
APP="$ROOT/dist/Agent Deck.app"
DMG="$ROOT/dist/AgentDeck-$VERSION.dmg"

if [ -n "${DEVELOPER_ID:-}" ] && [ -z "${NOTARY_PROFILE:-}" ]; then
  echo "DEVELOPER_ID is set but NOTARY_PROFILE is not; set both or neither." >&2
  exit 2
fi
if [ -z "${DEVELOPER_ID:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "NOTARY_PROFILE is set but DEVELOPER_ID is not; set both or neither." >&2
  exit 2
fi

"$ROOT/Scripts/make-app-bundle.sh" "$CONFIG"

if [ -n "${DEVELOPER_ID:-}" ]; then
  echo "paid path: re-signing with $DEVELOPER_ID (hardened runtime)"
  codesign --force --options runtime --timestamp --sign "$DEVELOPER_ID" "$APP"
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "Agent Deck $VERSION" -srcfolder "$STAGE" \
  -fs HFS+ -format UDZO -ov "$DMG" >/dev/null

if [ -n "${DEVELOPER_ID:-}" ]; then
  codesign --force --timestamp --sign "$DEVELOPER_ID" "$DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  echo "notarized + stapled"
else
  echo "free path: self-signed (${DECK_SIGN_IDENTITY:-Agent Deck Local}); not notarized"
fi

echo "built: $DMG"
