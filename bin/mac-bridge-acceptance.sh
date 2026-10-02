#!/usr/bin/env bash
# Live acceptance for the Mac bridge (docs/plans/2026-09-30-mac-bridge.md, L-1..L-6).
#
#   DECK_URL=http://your-deck:7789 DECK_TOKEN_FILE=~/.claude/agent-bus/deck-token.txt \
#     bin/mac-bridge-acceptance.sh
#
# Env:  DECK_URL          required: your deck (with or without /v1)
#       DECK_TOKEN        bearer (or DECK_TOKEN_FILE=<path>); never printed
#       DECK_ACCEPT_WRITE 1 = run the parts that message mac-probe / drive the Mac UI
#       DECK_MAC_*        UI hooks, see tests/live/test_mac_bridge_live.py
#
# Exit 0 only when every line PASSES. Any skip (incl. read-only mode) exits 1:
# a skipped line is unproven, not green. There is no ALLOW_SKIPS override.
set -uo pipefail
cd "$(dirname "$0")/.."

: "${DECK_URL:?set DECK_URL to your deck, e.g. DECK_URL=https://deck.example.com}"
export DECK_URL
if [[ -z "${DECK_TOKEN:-}" && -z "${DECK_TOKEN_FILE:-}" ]]; then
  echo "mac-bridge-acceptance: set DECK_TOKEN (or DECK_TOKEN_FILE)" >&2
  exit 2
fi
PY="${PYTHON:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
out="$(mktemp)"; trap 'rm -f "$out"' EXIT

echo "deck: $DECK_URL   writes: ${DECK_ACCEPT_WRITE:-0}"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -o addopts= -m live -p no:cacheprovider -W ignore \
  --tb=short -q -rsfE tests/live/test_mac_bridge_live.py 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}

if grep -Eq '[0-9]+ skipped' "$out"; then
  echo "mac-bridge-acceptance: RED - skipped lines are unproven, not passing" >&2
  rc=1
fi
if [[ $rc -eq 0 ]] && ! grep -Eq '[0-9]+ passed' "$out"; then
  echo "mac-bridge-acceptance: RED - nothing passed" >&2
  rc=1
fi

cat <<'EOM'

MANUAL (installed app, real box):
  L-6  lid closed 2 min mid-idle -> L-2 behaviour; reopen -> online <= 40 s (DECK_LID_TEST=1)
  UI   YOS_SCREEN_IS_FREE=1 xcodebuild ... test  (macos/UITests/MacBridgeUITests.swift)
EOM
exit "$rc"
