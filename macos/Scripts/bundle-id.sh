# SCREEN: none — sets two variables, never starts anything.
# Sourced by the build scripts: the app's bundle id, one answer for all of them.
#
# The Keychain item, the UserDefaults domain, notification ids and the log
# subsystem are all scoped to this id, so a build must keep the id its installs
# already have or it strands them. Set it once per machine, outside the repo:
#
#   ~/.config/agent-deck/build.env      (or $DECK_BUILD_ENV)
#     DECK_BUNDLE_ID=com.example.agentdeck
#     DECK_COPYRIGHT="© 2026 Example Ltd"
#
# An exported DECK_BUNDLE_ID / DECK_COPYRIGHT wins over the file. With neither,
# a build is the neutral public one.
_deck_env_id="${DECK_BUNDLE_ID:-}"
_deck_env_copy="${DECK_COPYRIGHT:-}"
_deck_build_env="${DECK_BUILD_ENV:-$HOME/.config/agent-deck/build.env}"
if [ -f "$_deck_build_env" ]; then
  # shellcheck disable=SC1090
  . "$_deck_build_env"
fi
DECK_BUNDLE_ID="${_deck_env_id:-${DECK_BUNDLE_ID:-dev.agentdeck.app}}"
DECK_COPYRIGHT="${_deck_env_copy:-${DECK_COPYRIGHT:-© 2026 Agent Deck contributors}}"
export DECK_BUNDLE_ID DECK_COPYRIGHT
unset _deck_env_id _deck_env_copy _deck_build_env
