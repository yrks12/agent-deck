"""No desk dead-ends on Claude Code's workspace trust dialog.

OWNER RULING 2026-09-30: agents never dead-end. MEASURED on the box that day:
`harbor-lead` (seated in `/home/deckop/repos/videos-for-harbor`, a folder the
owner named) read "Waiting for you: workspace trust was not pre-accepted" on
his screen, because `pretrust` refused every folder outside the deck's own
`workspaces/` with `not_deck_workspace`. `yye-ops` and `yye-growth`, hired the
same evening into `/home/deckop/repos/dana-sam-edits`, had no trust entry at
all -- the next two dead-ends, queued. And the resume path (`wake`) never
vouched for anything, so a woken desk got no trust write either.

The old rule ("never trust a folder the owner named") predates the ruling and
the bypassPermissions ruling that desks already run under: seating a desk in a
folder is the owner (or the boss desk he put in charge) saying work happens
there. What stays refused is a folder whose trust would cascade to everything
below it -- the filesystem root, the home directory, any ancestor of home --
and a folder that does not exist.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import api as api_mod
from server import pretrust, spawn


@pytest.fixture
def bus(tmp_path: Path) -> Path:
    d = tmp_path / "agent-bus"
    (d / "workspaces").mkdir(parents=True)
    return d


@pytest.fixture
def config(tmp_path: Path) -> Path:
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"numStartups": 3, "projects": {
        "/elsewhere": {"hasTrustDialogAccepted": True, "history": [1]}}}))
    return p


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home" / "deckop"
    h.mkdir(parents=True)
    monkeypatch.setattr(pretrust, "HOME", h, raising=False)
    return h


def _call(bus, config, cwd):
    return pretrust.pretrust(str(cwd), roster_path=bus / "roster.json",
                             config_path=config)


def test_a_folder_the_owner_named_is_trusted_so_the_desk_starts(bus, config, home):
    repo = home / "repos" / "videos-for-harbor"
    repo.mkdir(parents=True)

    result = _call(bus, config, repo)

    assert result.ok, result.detail
    after = json.loads(config.read_text())
    assert after["projects"][str(repo.resolve())]["hasTrustDialogAccepted"] is True
    assert after["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": True,
                                               "history": [1]}


@pytest.mark.parametrize("which", ["home", "root", "ancestor"])
def test_a_folder_whose_trust_would_cascade_is_still_refused(bus, config, home,
                                                              which):
    target = {"home": home, "root": Path("/"), "ancestor": home.parent}[which]
    before = config.read_bytes()

    result = _call(bus, config, target)

    assert not result.ok
    assert result.reason == "too_broad"
    assert config.read_bytes() == before


def test_a_folder_that_is_not_there_is_refused(bus, config, home):
    result = _call(bus, config, home / "repos" / "never-made")
    assert result.reason == "no_such_directory"


def test_the_repos_own_mcp_servers_are_enabled_and_inherited_ones_muted(
        bus, config, home):
    """The second modal ("New MCP server found in this project") is a dead-end
    too. His repo's own servers are his: answered yes. Ones inherited from a
    folder above (the box's `~/.mcp.json`): answered "continue without", as for
    every deck workspace."""
    (home / ".mcp.json").write_text(json.dumps({"mcpServers": {"docker-mcp": {}}}))
    repo = home / "repos" / "yye"
    repo.mkdir(parents=True)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"yye-tools": {}}}))

    result = _call(bus, config, repo)

    assert result.ok, result.detail
    entry = json.loads(config.read_text())["projects"][str(repo.resolve())]
    assert entry.get("enabledMcpjsonServers") == ["yye-tools"]
    assert entry.get("disabledMcpjsonServers") == ["docker-mcp"]


def test_hiring_into_a_real_repo_no_longer_warns_of_a_dead_end(tmp_path, home):
    repo = home / "repos" / "acme"
    repo.mkdir(parents=True)
    surface = api_mod.Surface(roster_path=tmp_path / "roster.json")

    assert surface.seat_warning(str(repo)).get("ok") is True


def test_a_woken_desk_is_vouched_for_before_it_resumes(tmp_path, monkeypatch):
    """The sweep: `wake` resumes through `spawn.resume_background`, which never
    wrote a trust key. Every door that opens a session vouches."""
    seen: list[str] = []
    monkeypatch.setattr(spawn.pretrust, "pretrust",
                        lambda cwd, **kw: seen.append(cwd) or pretrust.Trust(True, "trusted"))

    class Ran:
        returncode, stdout, stderr = 0, "woke session abcd1234\n", ""

    monkeypatch.setattr(spawn.subprocess, "run", lambda *a, **k: Ran())
    monkeypatch.setattr(spawn, "_agent_id", lambda out: "abcd1234")

    spawn.resume_background("abcd1234-0000", cwd=str(tmp_path), seed="hi")

    assert seen == [str(tmp_path)]


def test_a_stale_trust_block_clears_once_the_folder_is_trusted(tmp_path, config,
                                                               monkeypatch):
    """harbor-lead's card kept "Waiting for you" after its folder was trusted:
    the stored reason is only cleared by a start, and a desk woken by `wake`
    never passes through one."""
    repo = tmp_path / "harbor"
    repo.mkdir()
    data = json.loads(config.read_text())
    data["projects"][str(repo)] = {"hasTrustDialogAccepted": True}
    config.write_text(json.dumps(data))
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", config)
    surface = api_mod.Surface(roster_path=tmp_path / "roster.json")
    stale = {"what": "workspace trust was not pre-accepted", "reason":
             "not_deck_workspace", "detail": str(repo), "at": 1.0}

    got = surface._blocked({"state": "OFFLINE", "cwd": str(repo)},
                           {"blocked": stale}, None)

    assert got is None, "a desk whose folder is trusted still reads as dead-ended"


def test_a_trust_block_that_is_still_true_is_still_shown(tmp_path, config,
                                                          monkeypatch):
    repo = tmp_path / "harbor"
    repo.mkdir()
    monkeypatch.setattr(pretrust, "DEFAULT_CONFIG", config)
    surface = api_mod.Surface(roster_path=tmp_path / "roster.json")
    stale = {"what": "workspace trust was not pre-accepted", "reason":
             "not_deck_workspace", "detail": str(repo), "at": 1.0}

    got = surface._blocked({"state": "OFFLINE", "cwd": str(repo)},
                           {"blocked": stale}, None)

    assert got == stale
