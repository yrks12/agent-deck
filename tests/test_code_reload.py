"""A deploy that changes the CODE of a desk's MCP servers reloads that desk.

MEASURED 2026-10-02 on the box: PR #319 made every desk's `make_room` read
the daemon's published browser cap (6). Deployed at 04:40:58; every desk's
`computer_mcp`, `deck_mcp` and `mac_mcp` process had started 03:55-04:24, so
each still ran the old `browser_reaper` holding the old cap. One desk was
refused "browsers_full: 3 desk browsers are live (cap 3)". The tool-reload
sweep only compared TOOL SCHEMAS, and #319 changed none, so no desk reloaded.

Now each of those processes also stamps a fingerprint of the server code it
started with, and the sweep reloads a live desk whose stamp is missing or
differs from the code the deck runs now.
"""

from __future__ import annotations

import io

import pytest

from server import code_stamp, computer_mcp, tool_reload


@pytest.fixture
def stamps(tmp_path, monkeypatch):
    monkeypatch.setattr(code_stamp, "STAMP_ROOT", tmp_path)
    return tmp_path


def test_the_fingerprint_moves_when_any_server_module_changes(tmp_path):
    pkg = tmp_path / "server"
    pkg.mkdir()
    (pkg / "browser_reaper.py").write_text("MAX_LIVE = 3\n")
    (pkg / "notes.txt").write_text("not code")
    before = code_stamp.fingerprint(pkg)
    (pkg / "notes.txt").write_text("still not code")
    assert code_stamp.fingerprint(pkg) == before
    (pkg / "browser_reaper.py").write_text("MAX_LIVE = 6\n")
    assert code_stamp.fingerprint(pkg) != before


def test_a_process_from_older_code_is_stale(stamps):
    for server in code_stamp.SERVERS:
        code_stamp.write(server, "scout", sig="old")
    assert tool_reload.code_check("scout") == f"code:{code_stamp.CODE_SIG}"


def test_a_process_from_before_code_stamps_is_stale(stamps):
    assert tool_reload.code_check("scout") is not None


def test_a_desk_on_current_code_is_left_alone(stamps):
    for server in code_stamp.SERVERS:
        code_stamp.write(server, "scout")
    assert tool_reload.code_check("scout") is None


def test_one_stale_server_is_enough(stamps):
    for server in code_stamp.SERVERS:
        code_stamp.write(server, "scout")
    code_stamp.write("computer", "scout", sig="old")
    assert tool_reload.code_check("scout") is not None


def test_the_sweep_checks_code_not_only_tool_schemas():
    assert tool_reload.code_check in tool_reload.default_checks()


def test_the_computer_server_stamps_its_code_when_it_starts(stamps, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    computer_mcp.main(["--desk", "scout"])
    assert not code_stamp.stale("computer", "scout")
