#!/bin/bash
# SCREEN: takes
#
# **The reproduction, as a script — because the app pegs on its own.**
#
# The defect that took a day was believed to need scrolling, so the automated
# repro was an XCUITest that drove the transcript. That was wrong twice over:
# it ran on the fixture, whose longest message is one line, so it measured a
# pane that never spins. Against the owner's real deck the app takes a whole
# core **by itself, within 15 seconds, with nobody touching it** — which means
# the measurement is a shell script and not a UI test.
#
#   40s to settle (the ramp is 30-40s; every shorter reading has been a false
#   pass), then three readings six seconds apart, attributed to the app's own
#   pid rather than to a test runner.
#
# The four runs that named `Label` as the driver were taken exactly this way:
#
#   1  baseline                99.1%  100.5%  100.0%
#   2  DECK_X_NO_SELECTION     100.7%  99.5%  100.0%   <- not it
#   3  DECK_X_NO_CHROME         19.1%   3.4%   77.0%   <- half the loop
#   4  DECK_X_PLAIN_LABELS       0.1%   0.3%    0.0%   <- IT
#
# **THIS TAKES THE SCREEN.** It launches the real app with a Dock icon and a
# window. Default deny, same switch as the UI tests: set YOS_SCREEN_IS_FREE=1
# only when the owner has said he does not need his Mac.
#
#   DECK_URL=https://<your-deck> YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh
#   DECK_URL=https://<your-deck> YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh DECK_X_NO_CHROME=1
#
# Anything after the URL is passed to the app as an environment variable, so a
# variant run is one command and the report can say which build a number came
# from.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/dist/Agent Deck.app"
# The bundle renames the executable; see Scripts/make-app-bundle.sh.
BINARY="$BUNDLE/Contents/MacOS/Agent Deck"
SETTLE="${SETTLE_SECONDS:-40}"
GAP="${READING_GAP_SECONDS:-6}"

if [ "${YOS_SCREEN_IS_FREE:-}" != "1" ]; then
    echo "Refusing to run: this launches the real Agent Deck, brings it to the" >&2
    echo "front and holds the screen for about $((SETTLE + GAP * 3)) seconds." >&2
    echo "Re-run with YOS_SCREEN_IS_FREE=1 when the owner has said it is free." >&2
    exit 78
fi

if [ -z "${DECK_URL:-}" ]; then
    echo "usage: DECK_URL=<the deck under test> YOS_SCREEN_IS_FREE=1 $0 [KEY=VALUE ...]" >&2
    echo "DECK_URL is required; there is no default deck." >&2
    echo "A reading against the fixture proves nothing: its longest message is" >&2
    echo "one line, so the pane under test never spins." >&2
    exit 64
fi

if [ ! -x "$BINARY" ]; then
    echo "No app bundle at $BUNDLE — build one first:" >&2
    echo "  swift build -c release && Scripts/make-app-bundle.sh release" >&2
    exit 66
fi

echo "deck:  $DECK_URL"
echo "build: ${*:-baseline, no switches}"

env DECK_URL="$DECK_URL" "$@" "$BINARY" >/dev/null 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT

sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
    echo "The app exited before it could be measured. Any number here would be of a corpse." >&2
    exit 70
fi

echo "pid $PID — settling for ${SETTLE}s before the first reading"
sleep "$SETTLE"

READINGS=()
for i in 1 2 3; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "The app died after reading $((i - 1)). Nothing below is trustworthy." >&2
        exit 70
    fi
    CPU="$(ps -o pcpu= -p "$PID" | tr -d ' ')"
    READINGS+=("$CPU")
    echo "  t=$((SETTLE + GAP * (i - 1)))s  ${CPU}%"
    [ "$i" -lt 3 ] && sleep "$GAP"
done

echo
echo "readings: ${READINGS[*]}"
echo "A settled pane is a fraction of one percent. Anything in the tens is the"
echo "layout loop still running; anything near 100 is the whole defect."
echo "For the frames themselves, while it is still up: sample $PID 5 -f /tmp/deck.sample"
