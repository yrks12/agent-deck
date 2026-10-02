"""The iPhone's "Your Mac" entry must be there whenever a Mac node is online.

Owner, 2026-10-01 night: the entry that opens his Mac's live view was on his
iPhone earlier, then GONE after a fresh install. Measured cause: the phone
listed his Macs (`GET /v1/nodes`) only from `refreshAll()` -- pull-to-refresh
or coming back from the background -- never on the first load after launch,
and never again while the app stayed open. A cold launch (every install) drew
the roster with no Mac row at all.

The phone app has no unit-test seam for its store's network lifecycle (it
builds its own HTTPDeckClient), so this pins the three facts in its source:

1. the first load lists his Macs, as it lists attention and usage;
2. the periodic poll lists them again (a Mac that wakes appears on its own);
3. the roster draws a row for every listed Mac, with no grant condition --
   watching his own Mac needs no agent control grant.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = (ROOT / "ios/AgentDeckPhone/PhoneStore.swift").read_text()
ROSTER = (ROOT / "ios/AgentDeckPhone/RosterView.swift").read_text()


def _body(source: str, signature: str) -> str:
    """The braces-balanced body of the first function matching `signature`."""
    start = source.index(signature)
    i = source.index("{", start)
    depth = 0
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[i:j + 1]
    raise AssertionError(f"unbalanced body for {signature}")


def test_the_first_load_lists_his_macs():
    body = _body(STORE, "private func beginFirstLoad()")
    after_loaded = body[body.index("hasLoaded = true"):]
    assert "refreshMacs()" in after_loaded, (
        "a cold launch (every install) never asks /v1/nodes, so 'Your Mac' "
        "is missing until he pulls to refresh")


def test_the_poll_keeps_his_macs_current():
    body = _body(STORE, "private func pollAttention()")
    assert "refreshMacs()" in body, (
        "a Mac that wakes while the app is open never gets its row")


def test_every_listed_mac_gets_a_row_with_no_grant_condition():
    loop = _body(ROSTER, "ForEach(store.macs)")
    assert "YourMacRow(" in loop
    assert "controlLive" not in loop, "the row must not hide behind a grant"
    row = _body(ROSTER, "private struct YourMacRow")
    assert "Turn on Mac control" not in row, (
        "watching his own Mac needs no agent grant; the row must not say so")


def test_the_mac_apps_own_live_view_button_needs_no_grant():
    """Sweep, same defect on the Mac: Settings -> Mac's "Live view" button
    was disabled until an agent grant was on."""
    view = (ROOT / "macos/Sources/DeckUI/MacAccessSettingsView.swift").read_text()
    line = next(l for l in view.splitlines() if 'Button("Live view"' in l)
    assert "grant" not in line, line
