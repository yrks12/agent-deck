# Repository Guidelines

## Product and Structure

Shaliach is one product with two runtime halves. `server/` contains the
Python/FastAPI backend and agent runtime; `macos/` contains the native SwiftUI
client. Keep their shared wire format in `docs/client-api.md`. Claude hooks live
in `hooks/`, the browser fallback in `web/`, VPS assets in `deploy/`, and the
per-desk Linux environment in `docker/`. Backend tests are in `tests/`; Apple
unit and UI tests are under `macos/Tests/` and `macos/UITests/`.

## Commands

- `.venv/bin/python -m pytest -q` runs hermetic backend tests; default markers
  exclude `live` and `ui`.
- `.venv/bin/python -m pytest -q tests/test_client_api.py` runs one backend
  contract area.
- `swift build --package-path macos` builds the Apple client.
- `swift test --package-path macos` runs the XCTest suite.
- `macos/Scripts/make-app-bundle.sh release` creates the distributable app.

Run live/UI tests deliberately: they may launch agents, spend tokens, or drive
the visible Mac. Never point ordinary tests at the real agent bus.

## Style and Naming

Python uses four spaces, `snake_case`, typed public boundaries, and injected
filesystem/runtime dependencies. Swift follows standard Swift naming:
`PascalCase` types and `camelCase` members. Keep `DeckKit` decisions independent
of SwiftUI; `DeckUI` should render those decisions. Preserve explicit refusal
reasons across the API instead of reducing them to HTTP status codes.

## Detector-First Changes

Commit the failing test before production code. The test must fail if only the
implementation commit is reverted. Sweep the whole failure class: all routes,
state transitions, clients, or render sites sharing the shape. State whether
each premise is measured or assumed. Run the focused detector, then both full
suites for cross-contract changes.

## Commits and Reviews

Use narrow subjects such as `test(thread): ...`, `feat(api): ...`, and
`fix(screen): ...`. Pull requests must name the detector, RED witness, class
sweep, test results, live gaps, and deployment impact. Include screenshots or
XCUITest evidence for visible UI changes. Never commit tokens, transcripts,
Keychain material, `.env` files, or real agent-bus state.

## Deploying to the Box

Always deploy with `bin/deploy-box`. Never rsync or scp code to the box by hand:
several sessions doing that at once overwrote a fresh deploy with older code
(measured 2026-09-30).

```sh
git fetch origin && git checkout --detach origin/main   # a clean tree at the commit you mean
DECK_BOX=<user>@<box> bin/deploy-box                  # deploys HEAD
```

It holds a lock on the box for the whole run, refuses a dirty tree, and refuses
any commit that is not a descendant of the one in `/opt/agent-deck/DEPLOYED`.
It backs up the code, restarts the deck, and restores the backup if `/healthz`
does not answer. If it refuses because another deploy holds the lock, wait. If it
refuses as "not a descendant", merge or rebase onto main first. Use
`--force-rollback` only when a rollback is really what you want, and say so in
your report. A tree changed by hand shows up as a `DEPLOY GUARD` line in
`journalctl -u agentdeck` and as a red `code_tree` alert from deckdoctor.

