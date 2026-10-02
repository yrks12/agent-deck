#!/usr/bin/env bash
# Live acceptance for the 2026-09-30 overhaul (docs/plans/2026-09-30-overhaul.md).
#
#   DECK_URL=https://<your-deck>/v1 DECK_TOKEN=... bin/overhaul-acceptance.sh
#
# Env:  DECK_URL          REQUIRED, e.g. https://<your-deck>/v1 (no default)
#       DECK_TOKEN        the bearer token (or DECK_TOKEN_FILE=<path>); never printed
#       DECK_ACCEPT_WRITE 1 = also run the lines that post to the box (wave 4 only)
#       DECK_SSH / DECK_RETIRE_CMD / DECK_PROBE_DESK / DECK_PEER_DESK  see the tests
#
# Exit 0 only when every line PASSES. A skipped line is not green: read-only mode
# skips the write lines and therefore exits 1 by design (ALLOW_SKIPS=1 accepts skips).
set -uo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${DECK_URL:-}" ]]; then
  echo "overhaul-acceptance: set DECK_URL to the deck under test (e.g. https://<your-deck>/v1)" >&2
  exit 2
fi
export DECK_URL
if [[ -z "${DECK_TOKEN:-}" && -z "${DECK_TOKEN_FILE:-}" ]]; then
  echo "overhaul-acceptance: set DECK_TOKEN (or DECK_TOKEN_FILE)" >&2
  exit 2
fi
PY="${PYTHON:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
out="$(mktemp)"; trap 'rm -f "$out"' EXIT

echo "deck: $DECK_URL   writes: ${DECK_ACCEPT_WRITE:-0}"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -o addopts= -m live -p no:cacheprovider -W ignore \
  --tb=short -q -rsfE tests/live/test_overhaul_always_on.py \
  tests/live/test_overhaul_voice_autonomy.py tests/live/test_overhaul_calls.py 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}

if grep -Eq '[0-9]+ skipped' "$out" && [[ "${ALLOW_SKIPS:-0}" != 1 ]]; then
  echo "overhaul-acceptance: RED - skipped lines are unproven, not passing" >&2
  rc=1
fi

cat <<'EOF'

MANUAL (a person, a mic, the installed app - not scriptable):
  C-1  first mic press shows both macOS prompts; on deny the composer says why, typing works
  C-2  hold mic, "what's the status of acme": bubble <1 s, "Asking Atlas..." <1 s, speech p50 <15 s (5 tries)
  B-4  tap a decision button in the real app; the card flips to answered
  B-3/B-6  score the samples with tests/live/OVERHAUL_RUBRIC.md (DECK_RUBRIC_OUT=<file> keeps them)
  D-1..D-3  YOS_SCREEN_IS_FREE=1 xcodebuild ... test  (macos/UITests/OverhaulScreenshotTests.swift)
EOF
exit "$rc"
