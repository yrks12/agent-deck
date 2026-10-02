"""A desk's computer tools must outlive anything one call or one deploy does.

MEASURED on the box, 2026-10-01, desk atlas: a deploy at 22:24 changed
`desk_computer.ensure` to call `browser_reaper.admit`. The computer server
that atlas's session had started at 20:53 reloaded `desk_computer` (it is in
the guard list) but NOT `browser_reaper` (it is not), so at 22:29 the first
`navigate` against a stopped browser raised AttributeError. Only the refusal
types were caught; the error left `main`, the process exited, and Claude
showed "Connection closed". Claude reconnected a fresh server, but dropped
every `mcp__computer__*` tool from the desk's list -- and for the next hour
the desk told the owner its browser was gone until he restarted it, while he
was looking at that browser.

So: one bad call answers as an error and the server keeps reading, and a
deploy reloads every module the tools reach, not a hand-kept list.
"""

import io
import json
import sys

from server import computer_mcp, desk_computer, hire

DESK = "acme"


def _run(monkeypatch, *msgs):
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "".join(json.dumps(m) + "\n" for m in msgs)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    computer_mcp.main(["--desk", DESK])
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_an_unexpected_error_in_a_tool_is_an_answer_not_the_end_of_the_server(
        monkeypatch):
    def broken(desk):
        raise AttributeError("module 'server.browser_reaper' has no "
                             "attribute 'admit'")
    monkeypatch.setattr(desk_computer, "read_page", broken)
    monkeypatch.setattr(computer_mcp, "_fresh", lambda: None)
    replies = _run(
        monkeypatch,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "read_page", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [r["id"] for r in replies] == [1, 2]          # still reading
    first = replies[0]["result"]
    assert first["isError"] is True
    text = first["content"][0]["text"]
    assert "try" in text.lower()                         # it says what next
    assert "restart" not in text.lower()
    assert replies[1]["result"]["tools"]


def test_a_deploy_reloads_every_module_the_tools_reach(monkeypatch):
    reloaded = []
    stamps = iter([1.0, 2.0])
    monkeypatch.setattr(computer_mcp, "_stamp", lambda: next(stamps))
    monkeypatch.setattr(computer_mcp.importlib, "reload",
                        lambda mod: reloaded.append(mod.__name__) or mod)
    monkeypatch.setattr(computer_mcp, "_loaded_at", None)
    computer_mcp._fresh()
    computer_mcp._fresh()
    # The one that killed atlas's tools: desk_computer calls into it.
    assert "server.browser_reaper" in reloaded
    assert reloaded.index("server.browser_reaper") < \
        reloaded.index("server.desk_computer")
    # Never itself: its loop is running.
    assert "server.computer_mcp" not in reloaded


def test_the_brief_never_has_the_desk_send_the_owner_to_restart_it():
    text = hire.COMPUTER.lower()
    assert "never tell him to restart you" in text
    assert "toolsearch" in text
