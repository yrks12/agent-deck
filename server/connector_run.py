"""Start an installed connector with its key, or print its auth headers.

The ONLY place a connector's secret leaves the vault, and only into the
environment of the process it is for (or onto stdout for Claude Code's
`headersHelper`, which reads it into the request header and nowhere else).
The desk's `--mcp-config` names this module and the connector's id; it never
carries the value. See docs/connectors.md.

    python -m server.connector_run --desk atlas --id mcp:io.github.brave/...
        -> exec `npx -y @brave/brave-search-mcp-server@2.1.3` with BRAVE_API_KEY
    python -m server.connector_run --desk atlas --id mcp:io.github.upstash/... --headers
        -> {"Authorization": "Bearer ..."} on stdout

A desk can only launch what is in ITS ledger rows with keys granted to IT:
`vault.env_for(desk)` is the grant check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import connectors, vault

SCHEMES = ("bearer ", "basic ", "token ")


class LaunchError(Exception):
    pass


def _entry(desk: str, item_id: str, ledger_path: Path | None = None) -> dict:
    path = Path(ledger_path or connectors.INSTALLED_PATH)
    try:
        ledger = json.loads(path.read_text())
        entry = ((ledger.get("desks") or {}).get(desk) or {}).get(item_id)
    except (OSError, ValueError, AttributeError):
        entry = None
    if not isinstance(entry, dict):
        raise LaunchError(f"{item_id} is not installed on {desk}")
    return entry


def _values(desk: str, entry: dict, vault_path: Path | None) -> dict[str, str]:
    """{field: value} for this connector, from what `desk` was granted."""
    granted = vault.env_for(Path(vault_path or vault.DEFAULT_PATH), desk)
    out = {}
    for field, vname in (entry.get("vault") or {}).items():
        if vname in granted:
            out[field] = granted[vname]
    return out


def resolve(desk: str, item_id: str, *, vault_path: Path | None = None,
            ledger_path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """`(argv, extra env)` for a stdio connector. Raises LaunchError."""
    entry = _entry(desk, item_id, ledger_path)
    recipe = entry.get("recipe") or {}
    if recipe.get("type") != "stdio":
        raise LaunchError(f"{item_id} is not a local connector")
    values = _values(desk, entry, vault_path)
    env = {}
    for var, field in (recipe.get("env") or {}).items():
        if field in values:
            env[var] = values[field]
    return [recipe["command"], *recipe.get("args", [])], env


def headers(desk: str, item_id: str, *, vault_path: Path | None = None,
            ledger_path: Path | None = None, post=None,
            now: float | None = None) -> dict[str, str]:
    """The auth headers for a remote connector. Raises LaunchError.

    An OAuth connector's access token is refreshed first when it is within
    five minutes of expiring (`connectors.fresh_access`, under a lock, so two
    desks never spend the same rotating refresh token)."""
    entry = _entry(desk, item_id, ledger_path)
    recipe = entry.get("recipe") or {}
    if recipe.get("oauth"):
        store_dir = Path(ledger_path or connectors.INSTALLED_PATH).parent
        try:
            token = connectors.fresh_access(
                item_id, desk, store_dir=store_dir,
                vault_path=Path(vault_path or vault.DEFAULT_PATH), post=post, now=now)
        except connectors.mcp_oauth.OAuthError as exc:
            raise LaunchError(f"{item_id}: {exc.reason}") from None
        return {"Authorization": f"Bearer {token}"}
    values = _values(desk, entry, vault_path)
    out = {}
    for header, spec in (recipe.get("headers") or {}).items():
        value = values.get(spec.get("field"))
        if value is None:
            continue
        template = spec.get("template")
        if template and "{" in template and "}" in template:
            start, end = template.index("{"), template.index("}")
            value = template[:start] + value + template[end + 1:]
        elif header.lower() == "authorization" and not value.lower().startswith(SCHEMES):
            value = f"Bearer {value}"
        out[header] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connector_run")
    parser.add_argument("--desk", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--headers", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.headers:
            sys.stdout.write(json.dumps(headers(args.desk, args.id)) + "\n")
            return 0
        cmd, env = resolve(args.desk, args.id)
    except LaunchError as exc:
        # The id and the desk only: nothing here can name a value.
        sys.stderr.write(f"connector_run: {exc}\n")
        return 2
    os.execvpe(cmd[0], cmd, {**os.environ, **env})
    return 0  # pragma: no cover - exec does not return


if __name__ == "__main__":
    sys.exit(main())
