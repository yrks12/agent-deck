"""MB6: a desk's brief tells it the owner's Mac is there, and how to use it.

The `mac` MCP server was in every desk's `--mcp-config` (M2) and the deck now
serves the bridge, but nothing a desk reads said so -- the same gap the
computer had (tests/test_desk_drives_its_computer.py): a tool nobody is told
about is never used. The phrases are the plan's, pinned. They live in
the `mac_jobs` entry of `server/features.py`, which renders only while the deck
serves the bridge.
"""

from __future__ import annotations

from server import app as _app  # noqa: F401 - the bridge is live once the app is imported
from server import capabilities, hire, mac_mcp
from server.roster import Desk


def a_desk(name="acme"):
    return Desk(name=name, cwd="/tmp", engine="claude", mission="m",
                label="Ops", charter="You run ops.", reports_to=None)


def test_the_brief_says_the_owners_mac_is_there_and_how_to_use_it():
    text = hire.brief(a_desk(), inventory=capabilities.Inventory(), team=[])
    assert "His Mac" in text
    for phrase in ("mcp__mac__run", "mcp__mac__status",
                   "The box is your home; the Mac is his",
                   "If the Mac is offline, do not retry",
                   "change only what the task needs",
                   "never copy secrets off his Mac"):
        assert phrase in text, phrase


def test_every_mac_tool_the_brief_names_is_one_the_server_serves():
    text = hire.brief(a_desk(), inventory=capabilities.Inventory(), team=[])
    served = {t["name"] for t in mac_mcp.TOOLS}
    named = {word.split("mcp__mac__", 1)[1].rstrip(".,;:)")
             for word in text.split() if "mcp__mac__" in word} - {"*"}
    assert named and named <= served, named - served


def test_the_brief_says_the_grant_is_his_tap_on_the_mac():
    lowered = hire.brief(a_desk(), inventory=capabilities.Inventory(), team=[]).lower()
    assert "card" in lowered and "allow" in lowered
    assert "app store" in lowered, "the App Store build has no bridge; say so"
