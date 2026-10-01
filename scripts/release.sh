#!/bin/bash
# Build a release LOCALLY: app bundle -> DMG -> server tarball -> manifest.json.
# Publishes nothing. RELEASE_BASE is only the URL written into manifest.json;
# uploading dist/release/ is a separate step (owner decision D2).
#
#   RELEASE_BASE=https://host/path scripts/release.sh
#
# Signing (see macos/Scripts/make-dmg.sh): free self-signed by default,
# DEVELOPER_ID + NOTARY_PROFILE switch on the paid path.
set -euo pipefail

: "${RELEASE_BASE:?RELEASE_BASE is required (URL prefix for manifest.json)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
OUT="$ROOT/dist/release"

"$ROOT/macos/Scripts/make-dmg.sh" release
DMG="$ROOT/macos/dist/AgentDeck-$VERSION.dmg"

mkdir -p "$OUT"
python3 "$ROOT/scripts/release_manifest.py" --out "$OUT" --dmg "$DMG" \
  --base "$RELEASE_BASE" --notes "${RELEASE_NOTES:-}"
echo "release $VERSION ready in $OUT (not published)"
