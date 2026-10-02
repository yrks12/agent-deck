"""Vouch for a workspace the deck itself made, before a session opens in it.

**The fault.** Claude Code shows a workspace trust dialog for any directory it
has not seen before. A workspace the deck allocated seconds earlier is, by
definition, always one of those. So `interview_agent` opened a Terminal window
that sat on "Is this a project you created or one you trust?" with *No, exit*
pre-highlighted -- the seed prompt never ran, the hire never named itself, and
the desk read `OFFLINE`, identical to one that simply had not started yet.

**Why this key and not something cleaner.** There is no `--trust` flag. The only
bypass the CLI documents is non-interactive mode (`-p`, or a non-TTY stdout),
which a Terminal desk is not. The CLI does, however, name this mechanism itself:
when it refuses to load settings from an untrusted folder it prints "Run Claude
Code in that folder once and accept the trust dialog, or set
projects[<path>].hasTrustDialogAccepted: true in <config>." So this is the
supported escape hatch, not a poke at an internal.

Trust is resolved against the *exact* absolute path first, then by walking up to
the git root. `~/.claude/agent-bus/` is not a git repo and `/Users/<owner>` is
recorded `false`, so no ancestor rescues a workspace -- which is why the dialog
appeared even though the home directory has an entry. Measured, not assumed.

**Two things this deliberately will not do.**

*It will not trust a folder whose trust cascades.* SUPERSEDED in part by the
owner ruling of 2026-09-30 ("agents never dead-end"): this once refused every
folder the owner named, and `acme-lead` sat on the dialog for it. Now any
existing folder a desk is seated in is vouched for (`seat_verdict`), except
`/`, the home directory and its ancestors -- trusting those would trust every
folder below them, because the CLI resolves trust by walking up.

*It will not barge past a lock.* `~/.claude.json` is written by every live Claude
Code session on this machine and holds 96 projects' history, settings and MCP
servers. Claude Code locks it with proper-lockfile at `${path}.lock` -- a
directory, created by `mkdir`, treated as stale once its mtime stops being
refreshed. This takes that same lock, by that same protocol, re-reads the file
inside it, sets one key on one entry, and writes through `server.atomic` (which
uses `tempfile.mkstemp`, so two writers never share a temp name). If the lock is
held, this gives up and reports rather than writing.

**It clears the first gate, and there is a second.** Measured by spawning into
two sibling workspaces, one pre-trusted and one not. The control reproduced the
defect word for word -- "Quick safety check: Is this a project you created or
one you trust?". The treated one never drew it. What the treated one drew
instead was "New MCP server found in this project: docker-mcp ... Use this MCP
server?", because `~/.mcp.json` declares that server and a project entry the CLI
has never seen carries no `enabledMcpjsonServers`/`disabledMcpjsonServers`, so
it is asked per project. Same class of fault -- a modal a stubbed spawn test
cannot see -- and deliberately not fixed here: enabling or disabling an MCP
server on the owner's behalf is a decision about capability, and it deserves its
own change with its own test rather than being smuggled in behind this one.

**What it still cannot promise.** A session that loaded the config before this
write and saves afterwards can revert the key from its own in-memory copy; the
CLI has a re-basing path for exactly that (GH #3117) but it is theirs, not ours.
The window is small because the write happens immediately before the spawn, and
a reverted key fails visibly -- back to the dialog, with a ledger line already
recorded saying we set it.
"""
from __future__ import annotations

import errno
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import atomic
from .hire import events_path

#: proper-lockfile's default: a lock whose mtime is older than this belongs to a
#: process that died without releasing it.
LOCK_STALE = 10.0
#: How long to wait for a live writer. Long enough for a config save, short
#: enough that a hire never appears to hang.
LOCK_TIMEOUT = 3.0

#: The config file is `~/.claude.json` regardless of `CLAUDE_CONFIG_DIR` --
#: verified: setting that variable relocates `~/.claude/` and nothing else.
DEFAULT_CONFIG = Path.home() / ".claude.json"

#: The key the CLI reads. Named in its own error text; see the module docstring.
TRUST_KEY = "hasTrustDialogAccepted"
#: The key that answers "Continue without using this MCP server" ahead of time.
#: Measured, not inferred -- see the sweep quoted in tests/test_pretrust.py.
MUTE_KEY = "disabledMcpjsonServers"
#: What the CLI already wrote here if the owner said yes to a server.
ALLOW_KEY = "enabledMcpjsonServers"
MCP_FILE = ".mcp.json"


@dataclass(frozen=True)
class Trust:
    """The outcome. `reason` is a stable slug, in the shape `spawn.SpawnError`
    and `hire.HireError` already use, so every caller reads it the same way."""

    ok: bool
    reason: str
    detail: str = ""
    #: Names of `.mcp.json` servers this write denied the new hire. Empty when
    #: there were none to deny, or when nothing was written at all.
    muted: tuple[str, ...] = ()


def workspaces_root(roster_path: Path | str) -> Path:
    """The deck's own workspaces, found relative to the roster -- the same
    derivation `hire.events_path` uses, so production and a tmp-dir test both
    land on the directory `api.Surface._workspace` actually allocates into."""
    return Path(roster_path).parent / "workspaces"


def is_deck_workspace(cwd: str | Path, roster_path: Path | str) -> bool:
    """True only for a *direct child* of the workspaces root.

    On the resolved path, so a traversal (`.../workspaces/x/../../Projects`)
    resolves away before it is compared and cannot pass.
    """
    root = workspaces_root(roster_path)
    try:
        resolved = Path(cwd).resolve()
        return resolved.parent == root.resolve() and resolved != root.resolve()
    except OSError:
        return False


#: The owner's home. Trusting it -- or `/`, or anything above it -- would
#: cascade to every folder below, because the CLI resolves trust by walking up.
HOME = Path.home()


def seat_verdict(cwd: str | Path, roster_path: Path | str) -> tuple[bool, str, str]:
    """Whether a desk seated at `cwd` may be vouched for: `(ok, reason, detail)`.

    OWNER RULING 2026-09-30: agents never dead-end. A deck workspace always
    qualifies; so does any other existing folder a desk is seated in, because
    seating it there is the owner (or the boss desk he put in charge) saying
    work happens there -- and desks already run in bypassPermissions. MEASURED
    that day: `acme-lead` in `~/repos/acme-videos` sat on "Waiting for
    you: workspace trust was not pre-accepted" under the old rule.

    Refused, because its trust would cascade: `/`, the home directory and any
    ancestor of it. Refused, because there is nothing to trust: a folder that
    does not exist.
    """
    if is_deck_workspace(cwd, roster_path):
        return True, "deck_workspace", str(cwd)
    try:
        resolved = Path(cwd).resolve()
        home = Path(HOME).resolve()
    except OSError as exc:
        return False, "no_such_directory", f"{cwd}: {exc}"
    if resolved == resolved.parent or resolved == home or resolved in home.parents:
        return False, "too_broad", (
            f"{resolved} holds every folder below it; trusting it would trust "
            "them all")
    if not resolved.is_dir():
        return False, "no_such_directory", f"{resolved} is not a directory"
    return True, "seated_folder", str(resolved)


def is_trusted(cwd: str | Path, config_path: Path | None = None) -> bool:
    """Whether the CLI's config already trusts exactly `cwd`. Never raises."""
    config = _read(Path(config_path or DEFAULT_CONFIG)) or {}
    try:
        key = str(Path(cwd).resolve())
    except OSError:
        return False
    for candidate in {key, str(cwd)}:
        entry = (config.get("projects") or {}).get(candidate)
        if isinstance(entry, dict) and entry.get(TRUST_KEY) is True:
            return True
    return False


def mcp_json_servers(cwd: str | Path) -> list[str]:
    """Every `.mcp.json` server visible from `cwd`, walking up to the root.

    Up, because that is where the one that stalled the real desk lives: the
    workspace is `~/.claude/agent-bus/workspaces/<name>` and the declaration is
    in `~/.mcp.json`, four levels above it. A check that only looked in the
    workspace would find nothing and call it clean.

    Sorted and deduped so the write is deterministic. Unreadable or malformed
    files are skipped, not raised on: a broken `.mcp.json` somewhere up the
    tree is not a reason to refuse a hire.
    """
    names: set[str] = set()
    try:
        here = Path(cwd).resolve()
    except OSError:
        return []
    for folder in [here, *here.parents]:
        try:
            raw = json.loads((folder / MCP_FILE).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if isinstance(servers, dict):
            names.update(str(n) for n in servers)
    return sorted(names)


def _own_mcp_servers(cwd: str | Path) -> list[str]:
    """`.mcp.json` servers declared in `cwd` itself, not inherited from above."""
    try:
        raw = json.loads((Path(cwd) / MCP_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    return sorted(str(n) for n in servers) if isinstance(servers, dict) else []


def pretrust(cwd: str, *, roster_path: Path | str, engine: str = "claude",
             config_path: Path | None = None, _timeout: float = LOCK_TIMEOUT,
             _observe=None) -> Trust:
    """Mark `cwd` trusted so the session that opens there starts, not stalls.

    Never raises: a refusal is a `Trust` with a reason, because the caller's job
    on failure is to spawn anyway and *say so*, not to abandon the hire.
    """
    config_path = Path(config_path or DEFAULT_CONFIG)
    result = _pretrust(cwd, roster_path, engine, config_path, _timeout, _observe)
    _log(roster_path, cwd, result)
    return result


def _pretrust(cwd, roster_path, engine, config_path, timeout, observe) -> Trust:
    if engine != "claude":
        # Not silence: `codex` and `opencode` were both run for real in a
        # directory neither had seen, and neither gated on it. Codex does own a
        # trust gate (`~/.codex/config.toml`, `[projects."<p>"] trust_level`)
        # keyed off a git root, plus a blocking update prompt. Unhandled, and
        # said out loud rather than assumed away.
        return Trust(False, "engine_not_covered",
                     f"{engine} has no trust write here; see tests/test_pretrust.py")
    ok, reason, detail = seat_verdict(cwd, roster_path)
    if not ok:
        return Trust(False, reason, detail)

    key = str(Path(cwd).resolve())
    # A folder he named may declare its own `.mcp.json` servers. Those are his
    # and are answered yes; only servers inherited from above are muted.
    own = set() if reason == "deck_workspace" else set(_own_mcp_servers(key))
    try:
        with _locked(config_path, timeout):
            if observe is not None:
                observe(config_path)
            # Re-read INSIDE the lock and immediately before writing: anything
            # cached from before the lock was taken is already stale.
            config = _read(config_path)
            if config is None:
                return Trust(False, "unexpected_shape",
                             f"{config_path} is not a config object")
            projects = config["projects"]
            entry = projects.get(key) if isinstance(
                projects.get(key), dict) else {}

            # The second gate, decided in the same breath as the first: a deck
            # workspace inherits every `.mcp.json` server visible above it, and
            # is asked about each one before the session starts. Answer
            # "continue without" ahead of time -- a new hire gets no project MCP
            # servers unless the owner says otherwise.
            #
            # Except the ones he already said yes to *here*. Muting those would
            # be the deck overriding a choice he made, which is the same fault
            # as trusting a folder he named.
            allowed = {str(n) for n in entry.get(ALLOW_KEY) or ()}
            already = {str(n) for n in entry.get(MUTE_KEY) or ()}
            enable = sorted(own - allowed - already)
            muted = tuple(n for n in mcp_json_servers(key)
                          if n not in allowed and n not in already
                          and n not in own)

            if entry.get(TRUST_KEY) is True and not muted and not enable:
                return Trust(True, "already_trusted", key)

            # One entry, merged into whatever was there. Never a replacement,
            # never a prune -- and no empty list written when there is nothing
            # to mute, because noise in a file 96 projects share is not free.
            updated = {**entry, TRUST_KEY: True}
            if muted:
                updated[MUTE_KEY] = sorted(already | set(muted))
            if enable:
                updated[ALLOW_KEY] = sorted(allowed | set(enable))
            projects[key] = updated
            # Carry the mode across. `atomic.write_text` builds its temp file
            # with `mkstemp` (0600) and `os.replace` takes that mode with it,
            # so writing without this quietly retightens a 0644 config the
            # owner never asked us to touch.
            atomic.write_text(config_path,
                              json.dumps(config, indent=2, ensure_ascii=False),
                              mode=_mode_of(config_path))
    except _Busy as exc:
        return Trust(False, "lock_busy", str(exc))
    except (OSError, ValueError) as exc:
        return Trust(False, "write_failed", f"{type(exc).__name__}: {exc}")
    return Trust(True, "trusted", key, muted=muted)


BYPASS_KEY = "bypassPermissionsModeAccepted"


@dataclass(frozen=True)
class Accepted:
    """Outcome of `accept_bypass`. Its own type so its slugs are not pretrust
    reasons (those are a documented client contract, docs/client-api.md)."""

    ok: bool
    reason: str
    detail: str = ""


def accept_bypass(*, config_path: Path | None = None,
                  _timeout: float = LOCK_TIMEOUT) -> "Accepted":
    """Accept Claude Code's bypass-permissions disclaimer, once, for this user.

    OWNER RULING 2026-09-30 (see `server/deskperms.py`): desks run in
    bypassPermissions. MEASURED on claude 2.1.285: `claude --bg
    --permission-mode bypassPermissions` refuses with "requires accepting the
    disclaimer first" until `~/.claude.json` carries this key, and a `--bg`
    session that is not allowed it silently falls back to "default". Same lock
    and atomic write as `pretrust`; never raises; idempotent.
    """
    config_path = Path(config_path or DEFAULT_CONFIG)
    try:
        with _locked(config_path, _timeout):
            config = _read(config_path)
            if config is None:
                return Accepted(False, "unexpected_shape",
                             f"{config_path} is not a config object")
            if config.get(BYPASS_KEY) is True:
                return Accepted(True, "already_accepted", str(config_path))
            config[BYPASS_KEY] = True
            atomic.write_text(config_path,
                              json.dumps(config, indent=2, ensure_ascii=False),
                              mode=_mode_of(config_path))
    except _Busy as exc:
        return Accepted(False, "lock_busy", str(exc))
    except (OSError, ValueError) as exc:
        return Accepted(False, "write_failed", f"{type(exc).__name__}: {exc}")
    return Accepted(True, "accepted", str(config_path))


def _mode_of(path: Path) -> int | None:
    """The file's current permission bits, or None when it does not exist yet
    (in which case `atomic` leaving mkstemp's private 0600 is the right default
    for a file we are creating)."""
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _read(path: Path) -> dict | None:
    """The parsed config, or None if it is not the shape we are allowed to edit.
    A missing file is a config with no projects yet -- that is editable."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {"projects": {}}
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.setdefault("projects", {}), dict):
        return None
    return raw


class _Busy(Exception):
    """Another writer holds the config lock and did not let go in time."""


class _locked:
    """proper-lockfile's protocol, from this side.

    `mkdir` is the atomic primitive: it succeeds for exactly one process. A
    directory whose mtime has gone stale belonged to something that died, and is
    reclaimed. The hold is a read plus one `os.replace` -- milliseconds, far
    inside `LOCK_STALE` -- so no mtime refresh loop is needed.
    """

    def __init__(self, config_path: Path, timeout: float) -> None:
        self.path = Path(str(config_path) + ".lock")
        self.timeout = timeout

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if self._reclaim_if_stale():
                    continue
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            if time.monotonic() >= deadline:
                raise _Busy(f"{self.path} held for more than {self.timeout}s")
            time.sleep(0.02)

    def _reclaim_if_stale(self) -> bool:
        try:
            if time.time() - self.path.stat().st_mtime <= LOCK_STALE:
                return False
            os.rmdir(self.path)
            return True
        except OSError:
            return False

    def __exit__(self, *_exc) -> None:
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def _log(roster_path: Path | str, cwd: str, result: Trust) -> None:
    """One line in the one ledger `hooks/cc-bus.js` and `hire()` already write.

    A silent OFFLINE is what hid this defect for a whole build cycle, so the
    refusal is logged just as loudly as the success. Never raises: an unwritable
    ledger costs an audit line, not a hire.
    """
    line = {"ts": time.time(), "event": "pretrust", "cwd": str(cwd),
            "ok": result.ok, "reason": result.reason, "detail": result.detail,
            # What a new hire was silently denied is an audit fact.
            "muted": list(result.muted)}
    try:
        bus = events_path(roster_path)
        bus.parent.mkdir(parents=True, exist_ok=True)
        with bus.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
