"""Which Claude account a desk runs under, and where that account keeps its state.

An ACCOUNT is one Claude Code config directory. MEASURED on the box (claude
2.1.286, probe P1 of docs/plans/2026-10-01-two-accounts.md): `CLAUDE_CONFIG_DIR`
relocates `.credentials.json`, `projects/`, `sessions/`, `jobs/`, the daemon and
`.claude.json` (trust, onboarding, user MCP) into that directory. So a second
subscription is a second directory, with its own daemon and its own login.

`main` is implicit. It is `paths.CLAUDE_HOME`, it is never written to the
registry, and `env_for(main)` is None -- `env_kw(main)` is `{}` -- so every
subprocess call the single-account deck makes stays exactly the call it was.
That is the regression this module exists not to cause.

The registry (`BUS_DIR/accounts.json`, 0600) holds the other accounts:
`[{id, label, kind: subscription|api, config_dir, added_at}]`. It holds no
credential. The CLI owns `.credentials.json` in each config dir and is the only
thing that refreshes it (refresh tokens rotate; two refreshers lose the login).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import paths

DEFAULT_ID = "main"
KINDS = ("subscription", "api")
REGISTRY: Path = paths.BUS_DIR / "accounts.json"
#: Where `deckctl login --account <id>` makes a new account's config dir.
ACCOUNTS_ROOT_NAME = ".claude-accounts"

#: The macOS keychain item the CLI keeps the default login in. Only `main`'s is
#: known; a relocated config dir's item name is UNMEASURED, so it is left empty
#: rather than guessed (the box, which is where accounts run, has no keychain).
MAIN_KEYCHAIN = "Claude Code-credentials"

#: Never handed to a non-default account's process: either one would sign its
#: desks in as somebody else. MEASURED: the box's unit loads
#: CLAUDE_CODE_OAUTH_TOKEN from oauth.env into the deck's own environment.
_DROP = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    kind: str
    config_dir: Path
    added_at: float = 0.0

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_ID


@dataclass(frozen=True)
class AccountDirs:
    config_dir: Path
    sessions: Path
    projects: Path
    jobs: Path
    credentials: Path
    global_config: Path
    keychain_service: str


def valid_id(value: str) -> bool:
    return bool(_ID.match(value or "")) and value != DEFAULT_ID


def main_account() -> Account:
    return Account(DEFAULT_ID, "Main", "subscription", paths.CLAUDE_HOME, 0.0)


def _from_row(row) -> Account | None:
    if not isinstance(row, dict):
        return None
    ident = str(row.get("id") or "")
    kind = str(row.get("kind") or "subscription")
    raw_dir = str(row.get("config_dir") or "")
    if not valid_id(ident) or kind not in KINDS or not raw_dir:
        return None
    config_dir = Path(raw_dir)
    if not config_dir.is_absolute():
        return None
    try:
        added = float(row.get("added_at") or 0.0)
    except (TypeError, ValueError):
        added = 0.0
    return Account(ident, str(row.get("label") or ident), kind, config_dir, added)


def registry(path: Path | None = None) -> list[Account]:
    """Every account: `main` first, then the registry in file order. A missing,
    unreadable or malformed file is just `[main]`; a bad row is skipped."""
    found = [main_account()]
    try:
        rows = json.loads(Path(path or REGISTRY).read_text())
    except (OSError, ValueError):
        return found
    seen = {DEFAULT_ID}
    for row in rows if isinstance(rows, list) else []:
        acct = _from_row(row)
        if acct is not None and acct.id not in seen:
            seen.add(acct.id)
            found.append(acct)
    return found


def get(ident: str, path: Path | None = None) -> Account | None:
    ident = ident or DEFAULT_ID
    return next((a for a in registry(path) if a.id == ident), None)


def for_desk(desk, path: Path | None = None) -> Account:
    """The account `desk` runs under. Empty, or naming an account that is no
    longer registered, is `main` -- a desk is never made unstartable by it."""
    return get(str(getattr(desk, "account", "") or ""), path) or main_account()


def dirs(acct: Account) -> AccountDirs:
    if acct.is_default:
        # Read at call time: the suite redirects these, and so may a caller.
        from . import pretrust

        return AccountDirs(paths.CLAUDE_HOME, paths.SESSIONS_DIR, paths.PROJECTS_DIR,
                           paths.CLAUDE_HOME / "jobs",
                           paths.CLAUDE_HOME / ".credentials.json",
                           pretrust.DEFAULT_CONFIG, MAIN_KEYCHAIN)
    base = acct.config_dir
    return AccountDirs(base, base / "sessions", base / "projects", base / "jobs",
                       base / ".credentials.json", base / ".claude.json", "")


def other_dirs(kind: str) -> list[tuple[str, Path]]:
    """`(account id, dir)` for every NON-default account; `kind` is an
    `AccountDirs` field (`sessions`, `jobs`, `projects`). Readers put their own
    main dir first -- the module constant the suite redirects -- and append
    these. `[]` on a single-account deck, so the reader is what it was."""
    return [(a.id, getattr(dirs(a), kind)) for a in registry()[1:]]


def env_for(acct: Account) -> dict | None:
    """The environment a claude subprocess for `acct` runs in. None for `main`:
    inherit, exactly as before accounts existed."""
    if acct.is_default:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _DROP}
    env["CLAUDE_CONFIG_DIR"] = str(acct.config_dir)
    # The hooks look for the bus under CLAUDE_CONFIG_DIR; this pins it to the
    # deck's one bus (MEASURED, P2: it reaches the --bg worker's environ).
    env["DECK_BUS_DIR"] = str(paths.BUS_DIR)
    return env


def env_kw(acct: Account) -> dict:
    """`**env_kw(acct)` for subprocess calls: `{}` for main, so the call is
    byte-identical to the one the single-account deck makes."""
    env = env_for(acct)
    return {} if env is None else {"env": env}
