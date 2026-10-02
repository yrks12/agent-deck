"""The deck creates the workspace, so the deck vouches for it.

**The defect these pin, measured on the real daemon.** `POST /v1/agents/interview`
with no `cwd` allocates `~/.claude/agent-bus/workspaces/<name>`, hires the desk
and opens a Terminal window. The window then sits on Claude Code's workspace
trust dialog -- "Quick safety check: Is this a project you created or one you
trust?" -- because a directory the deck made seconds ago is by definition one
the CLI has never seen. The opening prompt never runs, the hire never names
itself, and the default-highlighted option is *No, exit*. All 807 tests passed
through that, because every one of them stubs `spawn_terminal` and so no test
has ever observed what the CLI does once the window is open.

**Why this key.** The CLI names this mechanism itself. Its own error text, when
it refuses to load settings from an untrusted folder, reads: "Run Claude Code in
that folder once and accept the trust dialog, or set
projects[<path>].hasTrustDialogAccepted: true in <config>." There is no
`--trust` flag; the only other bypass is non-interactive mode (`-p`, or a
non-TTY stdout), which a Terminal desk is not.

**What these tests can and cannot prove.** They cannot see the dialog -- no
pytest can. What they prove is that the exact key the CLI reads is present for
the exact path the deck allocated, that nothing else in a 96-project config
moved, and that the write is serialised on the very lockfile Claude Code's own
writer takes. That is a *proxy*. The real proof is a live spawn.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from server import pretrust


# A config with the shape and the neighbours a real one has: other projects,
# other keys on the target's own entry, and top-level keys that are not
# `projects` at all. Every one of them is something the write must not touch.
def _config(**projects) -> dict:
    base = {
        "numStartups": 1547,
        "installMethod": "native",
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "projects": {
            "/Users/x/Projects/one": {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Bash"],
                "lastCost": 0.42,
                "history": [{"display": "a prompt"}],
            },
            "/Users/x/Projects/two": {
                "hasTrustDialogAccepted": False,
                "mcpServers": {},
            },
        },
    }
    base["projects"].update(projects)
    return base


@pytest.fixture
def bus(tmp_path: Path) -> Path:
    """An agent-bus directory: the roster's parent, so `workspaces/` and
    `events.jsonl` are both derived from it exactly as production derives them."""
    d = tmp_path / "agent-bus"
    (d / "workspaces").mkdir(parents=True)
    return d


@pytest.fixture
def config(tmp_path: Path) -> Path:
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(_config(), indent=2))
    return p


def _workspace(bus: Path, name: str = "new-hire-55b770") -> str:
    d = bus / "workspaces" / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _ledger(bus: Path) -> list[dict]:
    path = bus / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _call(bus: Path, config: Path, cwd: str, **kw):
    return pretrust.pretrust(cwd, roster_path=bus / "roster.json",
                             config_path=config, **kw)


# ── the fix ────────────────────────────────────────────────────────────────

def test_a_deck_workspace_gets_the_trust_key_the_cli_reads(bus, config):
    """GOOD signal, not "no error": the key is *present and true* at the exact
    absolute path the deck allocated. Measured against the key name and shape
    read out of the shipped CLI binary, not from memory."""
    cwd = _workspace(bus)

    result = _call(bus, config, cwd)

    assert result.ok, result.detail
    after = json.loads(config.read_text())
    assert after["projects"][cwd]["hasTrustDialogAccepted"] is True


def test_every_other_project_entry_survives_the_write(bus, config):
    """The file holds 96 projects on this machine. A write that fixes one desk
    and drops somebody's history is a worse bug than the dialog."""
    before = json.loads(config.read_text())
    cwd = _workspace(bus)

    _call(bus, config, cwd)

    after = json.loads(config.read_text())
    for key, value in before.items():
        if key != "projects":
            assert after[key] == value, f"top-level {key!r} changed"
    for path, entry in before["projects"].items():
        assert after["projects"][path] == entry, f"project {path!r} changed"


def test_the_targets_own_other_keys_are_kept(bus, config):
    """One key set on one entry -- the entry is edited, never replaced."""
    cwd = _workspace(bus)
    raw = json.loads(config.read_text())
    raw["projects"][cwd] = {"hasTrustDialogAccepted": False,
                            "history": [{"display": "typed here before"}]}
    config.write_text(json.dumps(raw, indent=2))

    _call(bus, config, cwd)

    entry = json.loads(config.read_text())["projects"][cwd]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["history"] == [{"display": "typed here before"}]


# ── only the deck's own workspaces ─────────────────────────────────────────

def test_a_folder_the_owner_named_is_trusted_too(bus, config, tmp_path):
    """OWNER RULING 2026-09-30, agents never dead-end. This once refused ("a
    worse bug than the one being fixed"); `harbor-lead` then sat on the dialog
    in the folder he named. A seat is his say-so that work happens there."""
    theirs = tmp_path / "Projects" / "something-real"
    theirs.mkdir(parents=True)

    result = _call(bus, config, str(theirs))

    assert result.ok, result.detail
    assert json.loads(config.read_text())["projects"][str(theirs.resolve())][
        "hasTrustDialogAccepted"] is True


@pytest.mark.parametrize("suffix", ["sub/dir", "../../elsewhere", ".."])
def test_only_a_direct_child_of_the_workspaces_root_is_a_deck_workspace(
        bus, suffix):
    """`is_deck_workspace` still decides what the deck itself allocated (and so
    what `seat` may substitute); a prefix match would claim
    `.../workspaces/../../Projects`. On the resolved path's parent."""
    target = Path(_workspace(bus)) / suffix
    target.mkdir(parents=True, exist_ok=True)

    assert not pretrust.is_deck_workspace(target, bus / "roster.json")


def test_the_config_keeps_the_permissions_it_had(bus, config):
    """`atomic.write_bytes` makes its temp file with `mkstemp`, which is 0600.
    Replacing a 0644 config with it silently retightens a file the owner did
    not ask us to touch -- caught only because the real `~/.claude.json` came
    back 0600 after a test run. Whatever mode it had, it keeps."""
    config.chmod(0o644)

    _call(bus, config, _workspace(bus))

    assert oct(config.stat().st_mode)[-3:] == "644"


# ── the hot shared file ────────────────────────────────────────────────────

def test_two_writers_racing_both_keep_their_key(bus, config):
    """A naive load-modify-write loses whichever edit landed first. Both keys
    must be there afterwards -- that is the whole point of the lock."""
    paths = [_workspace(bus, f"new-hire-{i}") for i in range(8)]
    barrier = threading.Barrier(len(paths))

    def go(cwd: str) -> None:
        barrier.wait()
        _call(bus, config, cwd)

    threads = [threading.Thread(target=go, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    projects = json.loads(config.read_text())["projects"]
    missing = [p for p in paths if projects.get(p, {}).get(
        "hasTrustDialogAccepted") is not True]
    assert not missing, f"lost {len(missing)} of {len(paths)} writes: {missing}"
    assert "/Users/x/Projects/one" in projects


def test_the_lock_is_the_one_claude_code_itself_takes(bus, config):
    """PROXY, and named as one. Read out of the shipped CLI: it locks the config
    with proper-lockfile at `${configPath}.lock`, which is a *directory* created
    by mkdir. Holding that directory ourselves is what stops Claude Code's own
    saveGlobalConfig from interleaving with ours. This asserts the lock we take
    is that path and that kind, which is as close as a unit test gets."""
    lock = Path(str(config) + ".lock")
    seen: list[bool] = []

    def watch(_):
        seen.append(lock.is_dir())

    _call(bus, config, _workspace(bus), _observe=watch)

    assert seen == [True], "the config lock was not held across the write"
    assert not lock.exists(), "the lock was not released"


def test_a_lock_another_process_holds_is_waited_for_not_ignored(bus, config):
    """Claude Code is mid-write. Barging in is how 96 projects get lost, so this
    waits, gives up, and reports -- it never writes over a held lock."""
    lock = Path(str(config) + ".lock")
    lock.mkdir()
    before = config.read_bytes()
    try:
        result = _call(bus, config, _workspace(bus), _timeout=0.3)
    finally:
        lock.rmdir()

    assert not result.ok
    assert result.reason == "lock_busy"
    assert config.read_bytes() == before


def test_a_lock_left_behind_by_a_dead_process_is_reclaimed(bus, config):
    """proper-lockfile treats a lock whose mtime has stopped being refreshed as
    stale. A crashed session must not wedge every future hire forever."""
    lock = Path(str(config) + ".lock")
    lock.mkdir()
    old = time.time() - (pretrust.LOCK_STALE + 5)
    os.utime(lock, (old, old))

    result = _call(bus, config, _workspace(bus), _timeout=2.0)

    assert result.ok, result.detail


# ── failure has to be visible ──────────────────────────────────────────────

def test_a_config_that_is_not_the_shape_we_expect_is_left_alone(bus, config):
    config.write_text("[]")

    result = _call(bus, config, _workspace(bus))

    assert result.reason == "unexpected_shape"
    assert config.read_text() == "[]"


def test_every_outcome_is_written_to_the_one_ledger(bus, config, tmp_path):
    """A silent OFFLINE is what hid this for a whole build cycle. Both the win
    and the refusal leave a line a human can read, in the same `events.jsonl`
    the hooks and `hire()` already append to."""
    _call(bus, config, _workspace(bus))
    _call(bus, config, str(tmp_path / "never-made"))

    lines = [e for e in _ledger(bus) if e.get("event") == "pretrust"]
    assert [e["ok"] for e in lines] == [True, False]
    assert lines[1]["reason"] == "no_such_directory"
    assert all(isinstance(e["ts"], float) and e["cwd"] for e in lines)


def test_an_engine_the_sweep_did_not_cover_is_reported_not_skipped(bus, config):
    """The class is "a CLI's first-run prompt a stubbed spawn test cannot see",
    so the other two engines were run for real in a directory neither had ever
    seen. `opencode` landed straight on its composer. `codex` did too -- its
    trust gate keys off a git repository root and its own
    `~/.codex/config.toml` `[projects."<path>"] trust_level`, neither of which
    a bare deck workspace trips -- but it *did* draw a blocking "Update
    available ... Press enter to continue" prompt, which is the same class of
    fault by a different door. Neither is handled here. Saying "not covered"
    out loud is the difference between a known hole and the silence that let
    this one live."""
    cwd = _workspace(bus)
    for engine in ("codex", "opencode"):
        result = _call(bus, config, cwd, engine=engine)
        assert result.reason == "engine_not_covered"
        assert engine in result.detail
    assert cwd not in json.loads(config.read_text())["projects"]


# ── the second gate: inherited MCP servers ─────────────────────────────────
#
# Closing the trust dialog revealed another modal of exactly the same shape:
#
#     New MCP server found in this project: docker-mcp
#       Use this MCP server
#       Use this and all future MCP servers in this project
#     > Continue without using this MCP server
#
# `~/.mcp.json` declares that server, `.mcp.json` is discovered by walking up
# from the cwd, and a project entry the CLI has never seen has no verdict for
# it -- so a brand-new deck workspace inherits the prompt from the owner's home
# directory. The desk stalls at OFFLINE exactly as it did before.
#
# Which key suppresses it was MEASURED, not inferred: five candidate configs,
# five fresh pre-trusted workspaces, one live `claude` each. A control with no
# config drew the dialog; `disabledMcpjsonServers` carrying the server's name
# silenced it from `.claude/settings.local.json`, from `.claude/settings.json`,
# AND from the `~/.claude.json` project entry. The last is the one used here:
# it is the same file and the same entry as the trust key, so both land in one
# lock acquisition and one write, and nothing is dropped inside the agent's own
# workspace for it to trip over or commit.


def _mcp(path: Path, *names: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"mcpServers": {n: {"command": "x"} for n in names}}))


def test_a_new_hire_is_not_offered_the_mcp_servers_it_merely_inherited(bus, config):
    """GOOD signal: the inherited server is named in `disabledMcpjsonServers`
    on this workspace's own entry. A deck workspace gets no project MCP servers
    unless the owner says so -- it must not silently pick up whatever
    `~/.mcp.json` happens to declare."""
    _mcp(bus.parent / ".mcp.json", "docker-mcp")
    cwd = _workspace(bus)

    result = _call(bus, config, cwd)

    assert result.ok, result.detail
    entry = json.loads(config.read_text())["projects"][cwd]
    assert entry["hasTrustDialogAccepted"] is True
    assert "docker-mcp" in entry["disabledMcpjsonServers"]
    assert result.muted == ("docker-mcp",)


def test_discovery_walks_up_from_the_workspace_the_way_the_cli_does(bus, config):
    """The server that stalled the real desk was declared four levels above it,
    in the owner's home directory. A check that only looked in the workspace
    would have found nothing and called it clean."""
    _mcp(bus.parent / ".mcp.json", "from-grandparent")
    _mcp(bus / ".mcp.json", "from-parent")
    cwd = _workspace(bus)
    _mcp(Path(cwd) / ".mcp.json", "from-workspace")

    result = _call(bus, config, cwd)

    assert set(result.muted) >= {"from-grandparent", "from-parent",
                                 "from-workspace"}


def test_the_trust_key_and_the_mute_land_in_one_lock_and_one_write(bus, config,
                                                                   monkeypatch):
    """Two acquisitions of a lock on a file every live session writes is twice
    the contention and twice the chance of losing to a racing writer, for no
    gain -- both keys live on the same entry."""
    _mcp(bus.parent / ".mcp.json", "docker-mcp")
    writes: list[Path] = []
    locks: list[Path] = []

    real_write = pretrust.atomic.write_text
    real_enter = pretrust._locked.__enter__

    def counted_write(path, text, **kw):
        writes.append(Path(path))
        return real_write(path, text, **kw)

    def counted_enter(self):
        locks.append(self.path)
        return real_enter(self)

    monkeypatch.setattr(pretrust.atomic, "write_text", counted_write)
    monkeypatch.setattr(pretrust._locked, "__enter__", counted_enter)

    result = _call(bus, config, _workspace(bus))

    assert result.ok
    assert len(locks) == 1, f"took the lock {len(locks)} times"
    assert len(writes) == 1, f"wrote the config {len(writes)} times"


def test_a_server_the_owner_already_enabled_here_is_not_taken_away(bus, config):
    """"Unless the owner says otherwise" cuts both ways. A server he turned on
    for this workspace stays on -- muting it would be the deck overriding a
    choice he made, which is the same fault as trusting a folder he named."""
    _mcp(bus.parent / ".mcp.json", "docker-mcp", "wanted")
    cwd = _workspace(bus)
    raw = json.loads(config.read_text())
    raw["projects"][cwd] = {"enabledMcpjsonServers": ["wanted"]}
    config.write_text(json.dumps(raw, indent=2))

    result = _call(bus, config, cwd)

    entry = json.loads(config.read_text())["projects"][cwd]
    assert entry["enabledMcpjsonServers"] == ["wanted"]
    assert "wanted" not in entry["disabledMcpjsonServers"]
    assert "docker-mcp" in entry["disabledMcpjsonServers"]
    assert "wanted" not in result.muted


def test_an_existing_disabled_list_is_added_to_never_replaced(bus, config):
    _mcp(bus.parent / ".mcp.json", "docker-mcp")
    cwd = _workspace(bus)
    raw = json.loads(config.read_text())
    raw["projects"][cwd] = {"disabledMcpjsonServers": ["something-else"]}
    config.write_text(json.dumps(raw, indent=2))

    _call(bus, config, cwd)

    disabled = json.loads(config.read_text())["projects"][cwd][
        "disabledMcpjsonServers"]
    assert set(disabled) == {"something-else", "docker-mcp"}


def test_no_mcp_json_anywhere_writes_no_mute_key(bus, config, monkeypatch):
    """Nothing to mute is not the same as "mute nothing". An empty list on
    every entry is noise in a file 96 projects share."""
    monkeypatch.setattr(pretrust, "mcp_json_servers", lambda cwd: [])
    cwd = _workspace(bus)

    result = _call(bus, config, cwd)

    assert result.ok
    assert result.muted == ()
    assert "disabledMcpjsonServers" not in json.loads(
        config.read_text())["projects"][cwd]


def test_the_mute_is_recorded_in_the_ledger_with_the_trust_line(bus, config):
    """What a new hire was silently denied is an audit fact, not an
    implementation detail."""
    _mcp(bus.parent / ".mcp.json", "docker-mcp")

    _call(bus, config, _workspace(bus))

    line = [e for e in _ledger(bus) if e.get("event") == "pretrust"][-1]
    assert line["ok"] is True
    assert line["muted"] == ["docker-mcp"]


def test_a_folder_the_owner_named_keeps_its_own_mcp_servers(bus, config, tmp_path):
    """His repo's own `.mcp.json` servers are his, so they are answered yes;
    one inherited from a folder above is muted, as for a deck workspace. Both
    are answered, because an unanswered one is the second dead-end modal."""
    _mcp(tmp_path / ".mcp.json", "docker-mcp")
    theirs = tmp_path / "his-repo"
    _mcp(theirs / ".mcp.json", "his-tools")

    result = _call(bus, config, str(theirs))

    assert result.ok, result.detail
    assert result.muted == ("docker-mcp",)
    entry = json.loads(config.read_text())["projects"][str(theirs.resolve())]
    assert entry["enabledMcpjsonServers"] == ["his-tools"]