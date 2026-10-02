#!/usr/bin/env bash
# Build the macOS app release LOCALLY: an ad-hoc-signed .zip and its sha256.
# Publishes nothing -- uploading the two files to a GitHub release is a separate,
# deliberate step. scripts/install-mac.sh looks for exactly these two names:
#
#   dist/release/Shaliach-<VERSION>-macos.zip
#   dist/release/Shaliach-<VERSION>-macos.zip.sha256
#
#   scripts/release-mac.sh
#
# The build is the neutral public one, whatever machine makes it: the bundle id is
# dev.agentdeck.app, the builder's ~/.config/agent-deck/build.env is NOT read, and
# the bundle is re-signed ad hoc (`codesign --sign -`), never with the builder's own
# certificate or Apple ID. Ad hoc + not notarized means Gatekeeper blocks a
# downloaded copy until its quarantine flag is cleared; install-mac.sh does that.
set -euo pipefail

ROOT="${AGENT_DECK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
APP="${ROOT}/macos/dist/Shaliach.app"
OUT="${ROOT}/dist/release"
NAME="Shaliach-${VERSION}-macos.zip"

die() { printf 'release-mac: %s\n' "$*" >&2; exit 1; }
[ "$(uname -s)" = "Darwin" ] || die "builds on macOS only"

DECK_BUILD_ENV=/dev/null \
DECK_NEUTRAL_PATHS=1 \
DECK_BUNDLE_ID=dev.agentdeck.app \
DECK_COPYRIGHT="© 2026 Shaliach contributors" \
DECK_SIGN_IDENTITY="no-such-identity (release builds are ad hoc)" \
  "${ROOT}/macos/Scripts/make-app-bundle.sh" release

[ -d "$APP" ] || die "the build did not produce ${APP}"
# The executable keeps its pre-rename name "Agent Deck" (see make-app-bundle.sh).
# The linker's debug map still names each object file by its absolute build path;
# strip it so the shipped binary carries no path from the builder's machine.
BIN="${APP}/Contents/MacOS/Agent Deck"
case "$(file -b "$BIN" 2>/dev/null)" in *Mach-O*) strip -S "$BIN" ;; esac
if grep -qF "$(cd "$ROOT" && pwd -P)" "$BIN" || grep -qF "$HOME" "$BIN"; then
  die "${BIN} still carries a path from this machine; refusing to package it"
fi
codesign --force --deep --sign - "$APP"
# Read it whole first: `codesign | grep -q` under pipefail fails on grep's early exit.
SIGNATURE="$(codesign -dv "$APP" 2>&1 || true)"
case "$SIGNATURE" in *Signature=adhoc*) ;; *) die "${APP} is not ad-hoc signed after signing" ;; esac

mkdir -p "$OUT"
rm -f "${OUT}/${NAME}" "${OUT}/${NAME}.sha256"
ditto -c -k --sequesterRsrc --keepParent "$APP" "${OUT}/${NAME}"
(cd "$OUT" && shasum -a 256 "$NAME" > "${NAME}.sha256")

echo "built ${OUT}/${NAME}"
echo "      ${OUT}/${NAME}.sha256  ($(cut -d' ' -f1 < "${OUT}/${NAME}.sha256"))"
echo "not published: attach both files to a GitHub release to make install-mac.sh use them"
