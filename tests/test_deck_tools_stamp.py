"""Each deck MCP process says which tool set it serves; the brief says what to
do when a deck tool is missing.

MEASURED 2026-10-02 on the box: `call_owner` shipped, and Atlas -- whose
`deck` process predated it -- told the owner "the calling tool isn't loaded in
my session... It'll probably appear after the next restart of my desk".

* Each `deck` MCP process stamps a hash of the whole tool list it serves. A
  desk with no stamp, or an older hash, is stale.
* The brief tells a desk to ToolSearch a missing deck tool and never to ask
  the owner to restart it.
"""

from __future__ import annotations

import io
import sys

import pytest

from server import deck_mcp, features, tool_stamp


# ── the stamp ────────────────────────────────────────────────────────────────


def test_starting_the_deck_process_stamps_the_tool_set_it_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_mcp, "STAMP_ROOT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert deck_mcp.stale("atlas") is True
    assert deck_mcp.main(["--desk", "atlas"]) == 0
    assert deck_mcp.stale("atlas") is False
    row = tool_stamp.read("deck", "atlas", root=tmp_path)
    assert "call_owner" in row["names"]


def test_the_signature_changes_with_any_tool_or_description():
    base = tool_stamp.signature(deck_mcp.TOOLS)
    assert base == deck_mcp.TOOLS_SIG
    fewer = [t for t in deck_mcp.TOOLS if t["name"] != "call_owner"]
    assert tool_stamp.signature(fewer) != base
    reworded = [dict(t) for t in deck_mcp.TOOLS]
    reworded[0]["description"] += " (new)"
    assert tool_stamp.signature(reworded) != base


def test_a_stamp_from_the_tool_set_before_call_owner_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_mcp, "STAMP_ROOT", tmp_path)
    old = [t for t in deck_mcp.TOOLS if t["name"] != "call_owner"]
    tool_stamp.write("deck", "atlas", tool_stamp.signature(old),
                     names=[t["name"] for t in old], root=tmp_path)
    assert deck_mcp.stale("atlas") is True


# ── the brief ────────────────────────────────────────────────────────────────


def test_the_brief_says_toolsearch_first_and_never_ask_for_a_restart():
    text = features.section()
    assert "ToolSearch" in text
    assert "select:mcp__deck__" in text
    assert "reloads you" in text
    assert "never ask" in text.lower() and "restart" in text.lower()


@pytest.mark.parametrize("name", ["call_owner", "send_file", "history"])
def test_every_deck_tool_is_in_the_signature(name):
    assert name in {t["name"] for t in deck_mcp.TOOLS}
