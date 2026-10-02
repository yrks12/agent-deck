#!/bin/bash
# SCREEN: none — writes into an already-assembled .app bundle, never starts one.
#
# Builds AppIcon.icns from macos/Resources/AppIcon.png and drops it into a
# bundle's Contents/Resources/, so the Dock and Finder have an icon to show
# instead of the generic one. Split out of make-app-bundle.sh so it can be
# exercised on its own, against a throwaway bundle, without paying for a
# `swift build` — the property worth testing is "does a real AppIcon.png turn
# into a real AppIcon.icns", which has nothing to do with compiling Swift.
#
#   Scripts/embed-icon.sh <path-to-.app>
set -euo pipefail

APP="${1:?usage: embed-icon.sh <path-to-.app>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PNG="$ROOT/Resources/AppIcon.png"
RESOURCES="$APP/Contents/Resources"

if [ ! -f "$SOURCE_PNG" ]; then
  echo "embed-icon: missing $SOURCE_PNG" >&2
  exit 1
fi
mkdir -p "$RESOURCES"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"

# The hand-tuned set from generate-app-icon.py when it is committed: its 16
# and 32 px faces are drawn at that size (bigger eyes, no blush), which a
# `sips` shrink of the 1024 master cannot do -- that blurs the face into a
# pink square in Finder's list view.
TUNED="$ROOT/Resources/AppIcon.iconset"
if [ -d "$TUNED" ]; then
  cp "$TUNED"/icon_*.png "$ICONSET/"
else
  # Apple's full standard size set for a hand-built .iconset, 16pt..512pt plus
  # their @2x doubles (1024 is the 512@2x slot iconutil expects).
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$SOURCE_PNG" \
      --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$SOURCE_PNG" \
      --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
fi

iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"
echo "embedded: $RESOURCES/AppIcon.icns"
