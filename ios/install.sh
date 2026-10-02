#!/usr/bin/env bash
# Build the Agent Deck iPhone app from this checkout and install it on the iPhone
# plugged into this Mac, signed with your FREE Apple ID (no paid developer account,
# no TestFlight, no App Store).
#
#   ios/install.sh              # finds the iPhone and your team, builds, installs
#   ios/install.sh --dry-run    # prints the xcodegen / xcodebuild / install steps only
#   ios/install.sh --team ABCDE12345 --device <id>
#
# Needs: Xcode (opened once), `brew install xcodegen`, your Apple ID added under
# Xcode > Settings > Accounts, and the iPhone unlocked, trusted, in Developer Mode.
#
# The 7-day limit is Apple's: a free Apple ID signs an app for 7 days, then it stops
# opening. Run this again to renew; it reuses the same app id and keeps your pairing.
#
# Same steps as scripts/install-iphone.sh (the curl one-liner); this is the entry
# point for a checkout.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/install-iphone.sh" "$@"
