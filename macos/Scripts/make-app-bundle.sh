#!/bin/bash
# SCREEN: none — builds a bundle, never starts one.
#
# Wraps the SwiftPM executable in a .app so it gets a Dock icon, a menu bar and
# a stable bundle id (which the Keychain item is scoped to).
#
#   Scripts/make-app-bundle.sh [debug|release]
#
# Output: dist/Shaliach.app
#
# The product is Shaliach (it was "Agent Deck" until 0.9.2). The executable inside
# keeps the name "Agent Deck" on purpose: it is not shown to people (macOS shows
# CFBundleName), and tests, idle-cpu.sh and pgrep-based tooling find the process by
# it. The bundle id is unchanged, so keychain items, settings and pairings carry over.
set -euo pipefail

CONFIG="${1:-release}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/dist/Shaliach.app"
# One version for app + server: repo-root VERSION (plan K8).
VERSION="$(tr -d '[:space:]' < "$ROOT/../VERSION")"
# DECK_BUNDLE_ID: this machine's id for the app (Scripts/bundle-id.sh).
. "$ROOT/Scripts/bundle-id.sh"

# DECK_NEUTRAL_PATHS=1 (release-mac.sh sets it): Swift bakes source paths into the
# binary (#file, debug info); map the checkout to "agent-deck" so a published build
# never carries the builder's home directory or user name.
FLAGS=()
if [ "${DECK_NEUTRAL_PATHS:-0}" = 1 ]; then
  SRC_ROOT="$(cd "$ROOT/.." && pwd -P)"
  FLAGS=(-Xswiftc -file-prefix-map -Xswiftc "$SRC_ROOT=agent-deck"
         -Xswiftc -debug-prefix-map -Xswiftc "$SRC_ROOT=agent-deck")
fi
swift build --package-path "$ROOT" -c "$CONFIG" ${FLAGS[@]+"${FLAGS[@]}"}
BIN="$(swift build --package-path "$ROOT" -c "$CONFIG" ${FLAGS[@]+"${FLAGS[@]}"} --show-bin-path)/DeckApp"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Agent Deck"
# SwiftPM resource bundles (SwiftTerm's Metal shaders) go where a packaged
# app's code looks for them. The default renderer does not need it; the
# optional Metal one finds it here instead of crashing on another Mac.
for b in "$(dirname "$BIN")"/*.bundle; do
  [ -e "$b" ] && cp -R "$b" "$APP/Contents/Resources/"
done

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Shaliach</string>
  <key>CFBundleDisplayName</key><string>Shaliach</string>
  <key>CFBundleExecutable</key><string>Agent Deck</string>
  <key>CFBundleIdentifier</key><string>$DECK_BUNDLE_ID</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Shaliach listens only while you hold the mic button or are on a call, so you can talk to your agents instead of typing.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Shaliach turns what you say into text on this Mac and sends that text to the agent you are talking to. Your voice recording never leaves the Mac.</string>
</dict>
</plist>
PLIST

# The Dock icon. Must happen before the signing step below -- the signature
# has to cover the resource, or re-signing later invalidates it.
"$ROOT/Scripts/embed-icon.sh" "$APP"

# ── SIGN IT, and this is what stops the keychain asking every single time ────
#
# He photographed the dialog: "Agent Deck wants to use your confidential
# information stored in '<bundle id>' in your keychain." The very next
# screenshot was an app with an EMPTY SIDEBAR -- because the roster call has no
# token until that dialog is answered, and an unanswered dialog looks exactly
# like a deck that is down.
#
# MEASURED on the bundle that produced both:
#
#   codesign -dv "dist/Agent Deck.app"   (the bundle was named so then)
#     Identifier=DeckApp
#     CodeDirectory ... flags=0x20002(adhoc,linker-signed)
#     Signature=adhoc          TeamIdentifier=not set
#
# A keychain ACL is bound to the CODE IDENTITY of the process that created the
# item. For an ad-hoc, linker-signed binary that identity is its cdhash -- a
# hash of today's bytes -- so "Always Allow" is granted to a binary that will
# not exist after the next compile, and macOS is right to ask again. Signing
# with one self-signed certificate makes the identity the CERTIFICATE, which
# does not move when the code does.
#
# Not trusted by Gatekeeper, and it does not need to be: this app is built and
# run on his own machine, never distributed. `codesign` signs happily with an
# untrusted self-signed cert; only distribution would care.
#
# Create it once, if it is missing:
#   openssl req -x509 -newkey rsa:2048 -keyout k.pem -out c.pem -days 3650 \
#     -nodes -subj "/CN=Agent Deck Local" -addext "extendedKeyUsage=codeSigning"
#   openssl pkcs12 -export -legacy -inkey k.pem -in c.pem -out d.p12 \
#     -passout pass:deck -name "Agent Deck Local"
#   security import d.p12 -k ~/Library/Keychains/login.keychain-db \
#     -P deck -T /usr/bin/codesign -A
#
# MEASURED 2026-09-30 -- the self-signed cert is NOT enough on its own. The
# login keychain also keeps a PARTITION LIST per item, and for an app with no
# Apple team the partition is its code hash (`cdhash:...`), new on every build.
# The deck token's list had grown to 28 cdhashes: every rebuild asked again,
# and while it asked the app froze (now fixed: KeychainReadGate reads off the
# main thread). Two probe builds signed with an Apple Development identity got
# partition `teamid:<team>` instead, and the second read the first one's item
# with no dialog. So on a machine that has one, point DECK_SIGN_IDENTITY at it
# (its SHA-1 from `security find-identity -v -p codesigning`) in
# ~/.config/agent-deck/build.env. It is not the default here because a build
# made for someone else must not carry the builder's personal Apple ID.
# The data-protection keychain is not an option for this build: SecItemAdd
# with kSecUseDataProtectionKeychain answered -34018 (missing entitlement).
DECK_SIGN_IDENTITY="${DECK_SIGN_IDENTITY:-Agent Deck Local}"

if security find-certificate -c "$DECK_SIGN_IDENTITY" >/dev/null 2>&1 \
   || security find-identity -v -p codesigning | grep -q "$DECK_SIGN_IDENTITY"; then
  codesign --force --sign "$DECK_SIGN_IDENTITY" "$APP"
  echo "signed with: $DECK_SIGN_IDENTITY (the keychain will stop asking)"
else
  # Loud, and it still builds. A build that refused here would be a worse
  # trade: he would have no app at all rather than an app that asks once.
  echo "WARNING: no '$DECK_SIGN_IDENTITY' identity in the keychain."
  echo "         The bundle stays ad-hoc signed, so macOS will ask for your"
  echo "         keychain password after every rebuild -- and until you answer"
  echo "         it, the app opens with an empty sidebar. See the comment above"
  echo "         this line for the one-time command that creates it."
fi

echo "built: $APP"
