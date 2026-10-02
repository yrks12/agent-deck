"""Skills (C5, slice K1): explore and install Claude Code plugins on this server.

The owner calls them skills; the CLI calls them plugins (a plugin bundles
skills, commands, agents, hooks and MCP servers). The wire id is the CLI's
`name@marketplace`. Everything here shells out to `claude plugin …` as the
deck's own user with the deck's env, so it acts exactly as the desks would;
`CLAUDE_CONFIG_DIR` is honoured, which is how tests and measurements stay off
the real desks.

Measured on the box (Claude Code 2.1.286, isolated config; docs/skills.md):

* `plugin list --json --available` -> `{installed[], available[]}`; an
  installed plugin is **absent** from `available`, so its install count is
  only known from an earlier listing (remembered here, else null).
* Every mutating command with `--json` prints one result line on stdout
  (`outcome`, `failureCode`, …). A marketplace-declared command prints human
  text on stdout **before** that line, so the parser takes the last JSON line.
* `-s local` is keyed by the cwd: it writes `<cwd>/.claude/settings.local.json`
  (`enabledPlugins`) and records `projectPath` in `installed_plugins.json`; a
  session in any other cwd does not load the plugin.
* Without `-y`, and with stdin not a TTY, a command-sourced install is refused
  **before anything runs** (`failureCode: command_source_refused`) and reports
  `shownCommand.{command, sha256}`. `--accept-command <sha256>` accepts exactly
  that command. This module never passes `-y`.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import paths, roster

__all__ = ["Completed", "Skills", "SkillsError", "cli_runner", "parse_result", "redact"]

OFFICIAL = ("claude-plugins-official",)
CACHE_TTL = 600.0            # the listing, 10 min
UPDATE_EVERY = 6 * 3600.0    # `marketplace update`, at most once per 6 h
OP_TIMEOUT = 180.0           # install / uninstall / toggle
LIST_TIMEOUT = 60.0
README_MAX = 4000
APPLIES = "next_session"

DONE_TEXT = "Installed. Each agent picks it up on its next session."
RUNNING_TEXT = "Installing on your server…"

STATUS = {
    "bad_input": 400, "bad_scope": 400,
    "unknown_skill": 404, "unknown_desk": 404, "unknown_op": 404, "not_installed": 404,
    "already_installed": 409, "needs_confirmation": 409, "busy": 409,
    "install_failed": 502, "catalog_unavailable": 502,
    "cli_missing": 503, "timed_out": 504,
}

_SHA = re.compile(r"^[0-9a-f]{64}$")


class Completed(NamedTuple):
    rc: int
    stdout: str
    stderr: str


Runner = Callable[[list, "str | None", float], Completed]


class SkillsError(Exception):
    """A refusal in the deck's `{ok, reason, detail}` shape, plus `extra` fields."""

    def __init__(self, reason: str, detail: str = "", status: int | None = None,
                 extra: dict | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status = status or STATUS.get(reason, 400)
        self.extra = extra or {}

    def body(self) -> dict:
        return {"ok": False, "reason": self.reason, "detail": self.detail, **self.extra}


# ── the CLI ──────────────────────────────────────────────────────────────────


def claude_bin() -> str:
    """`claude` on PATH, else the installer's default `~/.local/bin/claude`."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    if local.exists():
        return str(local)
    raise FileNotFoundError("claude")


def cli_runner(args: list, cwd: str | None, timeout: float) -> Completed:
    """Run `claude <args>` non-interactively (stdin closed: nothing can prompt)."""
    proc = subprocess.run([claude_bin(), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL, env=os.environ.copy())
    return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


def parse_result(stdout: str) -> dict | None:
    """The last line of stdout that is a JSON object (human text may precede it)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


_REDACTIONS = (
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]+"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]+"), "***"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer ***"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2***"),
)


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="skills-op", daemon=True).start()


def _norm(path: str | None) -> str:
    return os.path.normpath(path) if path else ""


# ── plugin contents ──────────────────────────────────────────────────────────


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def _mcp_names(data: dict) -> list[str]:
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
    return [k for k, v in servers.items() if isinstance(v, dict)]


def components(root: Path) -> dict:
    """What a plugin directory holds, read from its files."""
    manifest = _load_json(root / ".claude-plugin" / "plugin.json")
    skills = []
    for skill_md in sorted((root / "skills").glob("*/SKILL.md")):
        meta = _frontmatter(skill_md)
        skills.append({"name": meta.get("name") or skill_md.parent.name,
                       "description": meta.get("description", "")})
    mcp = set(_mcp_names(_load_json(root / ".mcp.json")))
    if isinstance(manifest.get("mcpServers"), dict):
        mcp.update(_mcp_names({"mcpServers": manifest["mcpServers"]}))
    return {
        "skills": skills,
        "commands": sorted(p.stem for p in (root / "commands").glob("*.md")),
        "agents": sorted(p.stem for p in (root / "agents").glob("*.md")),
        "hooks": (root / "hooks" / "hooks.json").is_file() or bool(manifest.get("hooks")),
        "mcp_servers": sorted(mcp),
    }


def _readme(root: Path) -> str | None:
    for name in ("README.md", "readme.md", "Readme.md"):
        try:
            return (root / name).read_text(encoding="utf-8", errors="replace")[:README_MAX]
        except OSError:
            continue
    return None


# ── the service ──────────────────────────────────────────────────────────────


class _Listing(NamedTuple):
    fetched: float
    items: list          # Skill dicts, sorted
    rows: dict           # id -> [installed rows]
    plugin_dirs: dict    # id -> in-clone directory
    refreshed_at: str


class Skills:
    """Catalog, detail and the install/uninstall/toggle ops behind `/v1/skills`."""

    def __init__(self, *, runner: Runner = cli_runner, config_dir: Path | None = None,
                 desks: Callable[[], list] | None = None, lock_path: Path | None = None,
                 clock: Callable[[], float] = time.time,
                 start: Callable[[Callable[[], None]], None] | None = None,
                 ttl: float = CACHE_TTL, update_every: float = UPDATE_EVERY,
                 timeout: float = OP_TIMEOUT, official: tuple = OFFICIAL) -> None:
        self.runner = runner
        self.config_dir = Path(config_dir or paths.CLAUDE_HOME)
        self.desks = desks or (lambda: roster.load_roster(roster.DEFAULT_PATH))
        self.lock_path = Path(lock_path or paths.BUS_DIR / "skills.lock")
        self.clock = clock
        self.start = start or _thread
        self.ttl = ttl
        self.update_every = update_every
        self.timeout = timeout
        self.official = set(official)
        self._mu = threading.RLock()
        self._busy = threading.Lock()
        self._lock_fh = None
        self._listing: _Listing | None = None
        self._counts: dict[str, int] = {}
        self._last_update: float | None = None
        self._ops: dict[str, dict] = {}

    # ── running the CLI ──────────────────────────────────────────────────────
    def _run(self, args: list, cwd: str | None, timeout: float) -> Completed:
        try:
            return self.runner(args, cwd, timeout)
        except FileNotFoundError:
            raise SkillsError("cli_missing", "Claude Code isn't installed on this server.")
        except subprocess.TimeoutExpired:
            raise SkillsError("timed_out", f"claude plugin {args[1]} took longer than "
                                           f"{int(timeout)} seconds.")

    def _check(self, res: Completed, *, goal_ok: bool = False) -> dict:
        """The CLI's JSON result, or the refusal it amounts to."""
        parsed = parse_result(res.stdout) or {}
        if res.rc == 0 and parsed.get("outcome", "ok") == "ok":
            return parsed
        code = parsed.get("failureCode")
        if goal_ok and code == "already_in_goal_state":
            return parsed
        if code == "command_source_refused":
            shown = parsed.get("shownCommand") or {}
            command, sha = shown.get("command"), shown.get("sha256")
            raise SkillsError(
                "needs_confirmation",
                f"This skill installs by running a command on your server: {command}. "
                "Check it, then accept it to install.",
                extra={"command": command, "sha256": sha})
        if code == "not_found":
            raise SkillsError("unknown_skill", redact(parsed.get("message") or "not found"))
        if code in ("not_installed", "not_installed_at_scope"):
            raise SkillsError("not_installed", redact(parsed.get("message") or "not installed"))
        detail = _last_line(res.stderr) or parsed.get("message") or f"exit status {res.rc}"
        raise SkillsError("install_failed", redact(detail))

    # ── one op at a time ─────────────────────────────────────────────────────
    def _acquire(self) -> None:
        if not self._busy.acquire(blocking=False):
            raise SkillsError("busy", "Another skill change is still running.")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                raise SkillsError("busy", "Another skill change is still running.")
            self._lock_fh = fh
        except BaseException:
            self._busy.release()
            raise

    def _release(self) -> None:
        fh, self._lock_fh = self._lock_fh, None
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()
        self._busy.release()

    def invalidate(self) -> None:
        with self._mu:
            self._listing = None

    # ── the catalog ──────────────────────────────────────────────────────────
    def _maybe_update(self) -> None:
        now = self.clock()
        with self._mu:
            if self._last_update is not None and now - self._last_update < self.update_every:
                return
            self._last_update = now
        self.start(self._update)

    def _update(self) -> None:
        try:
            res = self._run(["plugin", "marketplace", "update"], None, self.timeout)
            if res.rc != 0:
                print(f"[agent-deck] skills: marketplace update failed: "
                      f"{redact(_last_line(res.stderr))}")
        except SkillsError as exc:
            print(f"[agent-deck] skills: marketplace update failed: {exc.detail}")
        except Exception as exc:  # a background refresh must never take the deck down
            print(f"[agent-deck] skills: marketplace update failed: {exc!r}")
        self.invalidate()

    def _marketplaces(self) -> dict[str, tuple[Path, dict]]:
        known = _load_json(self.config_dir / "plugins" / "known_marketplaces.json")
        out = {}
        for name, info in known.items():
            loc = info.get("installLocation") if isinstance(info, dict) else None
            root = Path(loc) if loc else self.config_dir / "plugins" / "marketplaces" / name
            manifest = _load_json(root / ".claude-plugin" / "marketplace.json")
            plugins = {p["name"]: p for p in manifest.get("plugins", [])
                       if isinstance(p, dict) and isinstance(p.get("name"), str)}
            out[name] = (root, plugins)
        return out

    @staticmethod
    def _source(src, entry: dict | None) -> dict:
        if isinstance(src, str) or src is None:
            home = (entry or {}).get("homepage")
            return {"kind": "path", "url": home if isinstance(home, str) else None, "ref": None}
        kind = str(src.get("source") or "url")
        url = src.get("url")
        if kind == "github" and src.get("repo"):
            url = f"https://github.com/{src['repo']}"
        elif kind == "npm" and src.get("package"):
            url = f"https://www.npmjs.com/package/{src['package']}"
        return {"kind": kind, "url": url if isinstance(url, str) else None,
                "ref": src.get("ref") if isinstance(src.get("ref"), str) else None}

    def _desk_names(self, project_paths: list[str]) -> list[str]:
        wanted = {_norm(p) for p in project_paths if p}
        try:
            desks = self.desks()
        except Exception:
            desks = []
        return sorted(d.name for d in desks if _norm(d.cwd) in wanted)

    @staticmethod
    def _local_enabled(pid: str, rows: list[dict]) -> bool | None:
        seen = []
        for row in rows:
            project = row.get("projectPath")
            if not project:
                continue
            settings = _load_json(Path(project) / ".claude" / "settings.local.json")
            value = (settings.get("enabledPlugins") or {}).get(pid)
            if isinstance(value, bool):
                seen.append(value)
        return any(seen) if seen else None

    def _build(self, raw: dict) -> _Listing:
        markets = self._marketplaces()
        rows: dict[str, list[dict]] = {}
        for row in raw.get("installed") or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                rows.setdefault(row["id"], []).append(row)
        base: dict[str, dict] = {}
        for entry in raw.get("available") or []:
            if isinstance(entry, dict) and isinstance(entry.get("pluginId"), str):
                base[entry["pluginId"]] = entry
                if isinstance(entry.get("installCount"), int):
                    self._counts[entry["pluginId"]] = entry["installCount"]
        items, dirs = [], {}
        for pid in list(base) + [i for i in rows if i not in base]:
            name, _, market = pid.rpartition("@")
            avail = base.get(pid, {})
            root, plugins = markets.get(market, (None, {}))
            m_entry = plugins.get(avail.get("name") or name)
            src = avail.get("source") if "source" in avail else (m_entry or {}).get("source")
            if root is not None and isinstance(src, str):
                candidate = (root / src).resolve()
                if candidate.is_dir() and root.resolve() in candidate.parents:
                    dirs[pid] = candidate
            desc = avail.get("description") or (m_entry or {}).get("description") or ""
            category = (m_entry or {}).get("category")
            item = {
                "id": pid, "name": avail.get("name") or name or pid,
                "description": desc if isinstance(desc, str) else "",
                "marketplace": avail.get("marketplaceName") or market,
                "official": (avail.get("marketplaceName") or market) in self.official,
                "category": category if isinstance(category, str) else None,
                "source": self._source(src, m_entry),
                "install_count": self._counts.get(pid),
                "installed": False, "enabled": None, "scope": None, "desks": [],
            }
            mine = rows.get(pid, [])
            if mine:
                user = [r for r in mine if r.get("scope") == "user"]
                local = [r for r in mine if r.get("scope") in ("local", "project")]
                item["installed"] = True
                item["desks"] = self._desk_names([r.get("projectPath") for r in local])
                path_hint = " ".join(str(r.get("installPath") or "") for r in mine)
                if market == "skills-dir":
                    item["scope"] = "skills-dir"
                elif "/plugins/synced/" in path_hint:
                    item["scope"] = "synced"
                elif user:
                    item["scope"] = "all"
                else:
                    item["scope"] = "desk"
                if user or item["scope"] in ("skills-dir", "synced"):
                    first = (user or mine)[0]
                    item["enabled"] = first.get("enabled") if isinstance(first.get("enabled"), bool) else None
                else:
                    item["enabled"] = self._local_enabled(pid, local)
            items.append(item)
        items.sort(key=lambda i: (not i["installed"], -(i["install_count"] or -1), i["id"]))
        now = self.clock()
        stamp = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(timespec="seconds")
        return _Listing(now, items, rows, dirs, stamp.replace("+00:00", "Z"))

    def _get(self) -> tuple[_Listing, bool]:
        with self._mu:
            current = self._listing
            if current is not None and self.clock() - current.fetched < self.ttl:
                return current, False
            try:
                res = self._run(["plugin", "list", "--json", "--available"], None, LIST_TIMEOUT)
                raw = json.loads(res.stdout) if res.rc == 0 else None
                if not isinstance(raw, dict):
                    raise SkillsError("catalog_unavailable", redact(
                        _last_line(res.stderr) or "claude plugin list returned no catalog"))
            except (SkillsError, ValueError) as exc:
                if current is not None:
                    return current, True
                if isinstance(exc, SkillsError):
                    raise
                raise SkillsError("catalog_unavailable", "claude plugin list returned no catalog")
            self._listing = self._build(raw)
            return self._listing, False

    def catalog(self, q: str = "", category: str | None = None, installed: bool | None = None,
                limit: int = 50, offset: int = 0) -> dict:
        self._maybe_update()
        listing, stale = self._get()
        needle = (q or "").strip().lower()
        hits = [i for i in listing.items
                if (not needle or needle in f"{i['name']} {i['description']}".lower())
                and (not category or i["category"] == category)
                and (installed is None or i["installed"] is installed)]
        return {"items": hits[offset:offset + limit], "total": len(hits),
                "refreshed_at": listing.refreshed_at, "stale": stale}

    def _item(self, pid: str) -> tuple[dict, _Listing]:
        listing, _ = self._get()
        item = next((i for i in listing.items if i["id"] == pid), None)
        if item is None:
            raise SkillsError("unknown_skill", f"No skill called {pid!r} on this server.")
        return item, listing

    def detail(self, pid: str) -> dict:
        item, listing = self._item(pid)
        root = None
        for row in listing.rows.get(pid, []):
            path = row.get("installPath")
            if path and Path(path).is_dir():
                root = Path(path)
                break
        if root is None:
            root = listing.plugin_dirs.get(pid)
        if root is None:
            return {**item, "readme": None, "components": None}
        return {**item, "readme": _readme(root), "components": components(root)}

    # ── targets ──────────────────────────────────────────────────────────────
    def _target(self, scope: str, desk: str | None) -> tuple[str | None, str]:
        """`(cwd, cli scope)` for C5's `all` / `desk`."""
        if scope == "all":
            return None, "user"
        if scope != "desk" or not desk:
            raise SkillsError("bad_scope", "Scope is 'all', or 'desk' with a desk name.")
        found = next((d for d in self.desks() if d.name == desk), None)
        if found is None:
            raise SkillsError("unknown_desk", f"No agent called {desk!r}.")
        if found.engine != "claude":
            raise SkillsError("unknown_desk", f"{desk} is not a Claude Code agent.")
        return found.cwd, "local"

    @staticmethod
    def _installed_at(listing: _Listing, pid: str, cli_scope: str, cwd: str | None) -> bool:
        for row in listing.rows.get(pid, []):
            if cli_scope == "user" and row.get("scope") == "user":
                return True
            if cli_scope == "local" and row.get("scope") == "local" \
                    and _norm(row.get("projectPath")) == _norm(cwd):
                return True
        return False

    # ── ops ──────────────────────────────────────────────────────────────────
    def install(self, pid: str, scope: str, desk: str | None = None,
                accept_command: str | None = None) -> dict:
        if accept_command is not None and not (isinstance(accept_command, str)
                                               and _SHA.match(accept_command)):
            raise SkillsError("bad_input", "accept_command is the 64-hex sha256 you were shown.")
        cwd, cli_scope = self._target(scope, desk)
        item, listing = self._item(pid)
        if self._installed_at(listing, pid, cli_scope, cwd):
            raise SkillsError("already_installed", f"{item['name']} is already installed there.")
        args = ["plugin", "install", pid, "--json", "-s", cli_scope]
        self._acquire()
        try:
            if item["source"]["kind"] == "command":
                # Without -y the CLI only *shows* the command; nothing runs.
                self._check(self._run(args, cwd, self.timeout))
                # Accepted before (or no command after all): it installed.
                return self._finished(pid, None)
        except SkillsError as exc:
            if exc.reason != "needs_confirmation":
                self._release()
                self.invalidate()
                raise
            if exc.extra.get("sha256") != accept_command:
                self._release()
                raise
        except BaseException:
            self._release()
            raise
        if accept_command is not None:
            args += ["--accept-command", accept_command]
        op_id = "sk_" + secrets.token_hex(6)
        with self._mu:
            self._ops[op_id] = {"state": "running", "reason": None, "detail": RUNNING_TEXT,
                                "applies": APPLIES}
        try:
            self.start(lambda: self._work(op_id, args, cwd))
        except BaseException:
            self._release()
            raise
        return {"op_id": op_id, "state": "running"}

    def _finished(self, pid: str, error: SkillsError | None) -> dict:
        """An install that completed inside the request (the command preflight)."""
        op_id = "sk_" + secrets.token_hex(6)
        self._record(op_id, error)
        self._release()
        self.invalidate()
        return {"op_id": op_id, "state": "running"}

    def _record(self, op_id: str, error: SkillsError | None) -> None:
        if error is None:
            op = {"state": "done", "reason": None, "detail": DONE_TEXT, "applies": APPLIES}
        else:
            op = {"state": "failed", "reason": error.reason, "detail": error.detail,
                  "applies": APPLIES, **error.extra}
        with self._mu:
            self._ops[op_id] = op

    def _work(self, op_id: str, args: list, cwd: str | None) -> None:
        error = None
        try:
            self._check(self._run(args, cwd, self.timeout))
        except SkillsError as exc:
            error = exc
        except Exception as exc:  # an op thread must always end in a state
            error = SkillsError("install_failed", redact(repr(exc)))
        finally:
            try:
                self._record(op_id, error)
            finally:
                self._release()
                self.invalidate()
        if error is not None:
            print(f"[agent-deck] skills: {args[1]} {args[2]} failed: {error.reason}: {error.detail}")

    def op(self, op_id: str) -> dict:
        with self._mu:
            op = self._ops.get(op_id)
            if op is None:
                raise SkillsError("unknown_op", f"No skill change {op_id!r}.")
            return dict(op)

    def _sync(self, verb: str, pid: str, scope: str, desk: str | None, *,
              goal_ok: bool = False) -> None:
        cwd, cli_scope = self._target(scope, desk)
        item, listing = self._item(pid)
        if not self._installed_at(listing, pid, cli_scope, cwd):
            raise SkillsError("not_installed", f"{item['name']} is not installed there.")
        self._acquire()
        try:
            self._check(self._run(["plugin", verb, pid, "--json", "-s", cli_scope], cwd,
                                  self.timeout), goal_ok=goal_ok)
        finally:
            self._release()
            self.invalidate()

    def uninstall(self, pid: str, scope: str = "all", desk: str | None = None) -> dict:
        self._sync("uninstall", pid, scope, desk)
        return {"ok": True, "applies": APPLIES}

    def set_enabled(self, pid: str, enabled: bool, scope: str = "all",
                    desk: str | None = None) -> dict:
        self._sync("enable" if enabled else "disable", pid, scope, desk, goal_ok=True)
        return {"ok": True, "enabled": enabled, "applies": APPLIES}
