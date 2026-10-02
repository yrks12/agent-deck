"""K1: the skills service (C5) against a fake `claude plugin` CLI.

The fake speaks the shapes measured on the box with Claude Code 2.1.286 under an
isolated CLAUDE_CONFIG_DIR (see docs/skills.md): `list --json --available`
returns `{installed[], available[]}` and an installed plugin is **absent** from
`available`; every mutating command prints one JSON result line on stdout, but a
marketplace-declared command prints human text on stdout *before* that line;
`-s local` is keyed by the cwd (`projectPath`); a second `disable` fails with
`failureCode: already_in_goal_state`.

What these pin:

* the catalog joins `available` + `installed` + each marketplace's
  `marketplace.json` (category), is cached 10 min, sorted installed-first then
  by install count, searched over name + description;
* `claude plugin marketplace update` runs at most once per 6 h, off the request;
* install is an async op: `-s user` for all agents, `-s local` in the desk's
  cwd for one agent;
* a marketplace-declared command is refused as `needs_confirmation` with the
  CLI's own sha256, and is only ever accepted with `--accept-command <sha>`:
  **no call the service makes ever carries `-y` / `--yes`**;
* one op at a time (thread lock + flock), 180 s timeout, secrets redacted from
  the stderr line a failure carries.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server.roster import Desk

try:  # absent, every test must FAIL on behaviour, not error at collection
    from server import skills
except ImportError:  # pragma: no cover - the RED state
    skills = None

FIXTURE = Path(__file__).parent / "fixtures" / "plugin_list_available.json"
OFFICIAL = "claude-plugins-official"
SDK = f"agent-sdk-dev@{OFFICIAL}"
CRUNCH = f"42crunch-api-security-testing@{OFFICIAL}"
ADLC = f"agentforce-adlc@{OFFICIAL}"
ASANA = f"asana@{OFFICIAL}"
CMD = "cmd-probe@probe-mkt"

#: The in-clone entry the real CLI omits from `available` once installed.
SDK_AVAILABLE = {
    "pluginId": SDK, "name": "agent-sdk-dev",
    "description": "Development kit for working with the Claude Agent SDK",
    "marketplaceName": OFFICIAL, "source": "./plugins/agent-sdk-dev",
    "installCount": 70287,
}


def _sha(plugin_id: str, command: str) -> str:
    return hashlib.sha256(f"{plugin_id}\0{command}".encode()).hexdigest()


class FakeCLI:
    """A stateful stand-in for `claude plugin …` that answers like 2.1.286."""

    def __init__(self) -> None:
        data = json.loads(FIXTURE.read_text())
        self.installed: list[dict] = copy.deepcopy(data["installed"])
        self.catalog: list[dict] = [SDK_AVAILABLE] + copy.deepcopy(data["available"])
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_next: tuple[int, str, str] | None = None
        self.raise_next: BaseException | None = None
        self.hold: threading.Event | None = None
        self.entered = threading.Event()

    # the runner signature the service uses
    def __call__(self, args: list[str], cwd: str | None, timeout: float):
        assert "-y" not in args and "--yes" not in args, f"auto-accepted: {args}"
        self.calls.append((list(args), cwd))
        self.entered.set()
        if self.hold is not None and args[:2] == ["plugin", "install"]:
            assert self.hold.wait(10)
        if self.raise_next is not None and args[1] != "marketplace":
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self.fail_next is not None and args[1] != "list":
            out, self.fail_next = self.fail_next, None
            return skills.Completed(*out)
        verb = args[1]
        if verb == "list":
            ids = {e["id"] for e in self.installed}
            return self._ok({"installed": self.installed,
                             "available": [a for a in self.catalog
                                           if a["pluginId"] not in ids]}, pretty=True)
        if verb == "marketplace":
            return skills.Completed(0, "Updating marketplaces...✔ Successfully updated 1 marketplaces\n", "")
        return getattr(self, "_" + verb)(args, cwd)

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _ok(payload: dict, pretty: bool = False):
        text = json.dumps(payload, indent=2 if pretty else None)
        return skills.Completed(0, text + "\n", "")

    @staticmethod
    def _failed(payload: dict, human: str = ""):
        return skills.Completed(1, human + json.dumps(payload) + "\n",
                                f"✘ {payload['message']}\n")

    @staticmethod
    def _scope(args: list[str], default: str | None = "user") -> str | None:
        return args[args.index("-s") + 1] if "-s" in args else default

    def _entries(self, pid: str, scope: str | None, cwd: str | None) -> list[dict]:
        out = []
        for e in self.installed:
            if e["id"] != pid:
                continue
            if scope == "user" and e["scope"] == "user":
                out.append(e)
            elif scope == "local" and e["scope"] == "local" and e.get("projectPath") == cwd:
                out.append(e)
            elif scope is None and (e["scope"] == "user" or e.get("projectPath") == cwd):
                out.append(e)
        return out

    def _install(self, args, cwd):
        pid, scope = args[2], self._scope(args)
        entry = next((a for a in self.catalog if a["pluginId"] == pid), None)
        base = {"command": "install", "plugin": pid, "scope": scope}
        if entry is None:
            name, _, market = pid.partition("@")
            return self._failed({**base, "outcome": "failed", "failureCode": "not_found",
                                 "message": f'Plugin "{name}" not found in marketplace "{market}"'})
        src = entry["source"]
        if isinstance(src, dict) and src.get("source") == "command":
            sha = _sha(pid, src["command"])
            accepted = args[args.index("--accept-command") + 1] if "--accept-command" in args else None
            if accepted != sha:
                shown = {"kind": "command_source", "pluginId": pid, "command": src["command"],
                         "mode": "copy", "catalogRevision": "sha256:c6ea", "sha256": sha}
                if accepted is not None:
                    shown["acceptCommandMatched"] = False
                human = (f'"{entry["name"]}" is installed by running a command from marketplace '
                         f'"{entry["marketplaceName"]}" on this machine:\n  {src["command"]}\n')
                return self._failed({**base, "outcome": "failed", "failureCode": "command_source_refused",
                                     "message": f"{pid} is installed by running a command on this machine "
                                                f"(`{src['command']}`) that has not been reviewed yet, so it was not run.",
                                     "shownCommand": shown}, human)
        if self._entries(pid, scope, cwd):
            return self._ok({**base, "outcome": "ok", "pluginId": pid,
                             "message": f'Plugin "{pid}" is already installed (scope: {scope})',
                             "installedVersion": "ab024cdcfa7c", "availableVersion": "ab024cdcfa7c"})
        row = {"id": pid, "version": "ab024cdcfa7c", "scope": scope, "enabled": True,
               "installPath": f"/nonexistent/cache/{pid}", "installedAt": "2026-09-30T20:21:22.025Z",
               "lastUpdated": "2026-09-30T20:21:22.025Z", "projectEnabled": False}
        if scope == "local":
            row["projectPath"] = cwd
        self.installed.append(row)
        return self._ok({**base, "outcome": "ok", "pluginId": pid,
                         "message": f"Successfully installed plugin: {pid} (scope: {scope})"})

    def _uninstall(self, args, cwd):
        pid, scope = args[2], self._scope(args)
        hits = self._entries(pid, scope, cwd)
        base = {"command": "uninstall", "plugin": pid, "scope": scope}
        if not hits:
            code = "not_installed" if not any(e["id"] == pid for e in self.installed) else "not_installed_at_scope"
            return self._failed({**base, "outcome": "failed", "failureCode": code,
                                 "message": f'Plugin "{pid}" not found in installed plugins'})
        for e in hits:
            self.installed.remove(e)
        return self._ok({**base, "outcome": "ok", "pluginId": pid, "keptData": False,
                         "message": f"Successfully uninstalled plugin: {pid} (scope: {scope})"})

    def _toggle(self, args, cwd, goal: bool):
        pid, scope = args[2], self._scope(args, None)
        verb = "enable" if goal else "disable"
        hits = self._entries(pid, scope, cwd)
        if not hits:
            return self._failed({"command": verb, "outcome": "failed", "plugin": pid,
                                 "failureCode": "not_installed", "message": f'Plugin "{pid}" is not installed'})
        if all(e["enabled"] == goal for e in hits):
            return self._failed({"command": verb, "outcome": "failed", "plugin": pid,
                                 "failureCode": "already_in_goal_state", "alreadyInGoalState": True,
                                 "message": f'Plugin "{pid}" is already {verb}d'})
        for e in hits:
            e["enabled"] = goal
        return self._ok({"command": verb, "outcome": "ok", "plugin": pid, "pluginId": pid,
                         "scope": hits[0]["scope"], "message": f"Successfully {verb}d plugin"})

    def _enable(self, args, cwd):
        return self._toggle(args, cwd, True)

    def _disable(self, args, cwd):
        return self._toggle(args, cwd, False)

    def verbs(self, verb: str) -> list[tuple[list[str], str | None]]:
        return [(a, c) for a, c in self.calls if a[1] == verb]


class Clock:
    def __init__(self) -> None:
        self.now = 1_790_800_000.0

    def __call__(self) -> float:
        return self.now


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_config(tmp: Path) -> Path:
    """An isolated CLAUDE_CONFIG_DIR with one marketplace clone, as `marketplace add` leaves it."""
    config = tmp / "config"
    clone = config / "plugins" / "marketplaces" / OFFICIAL
    _write(config / "plugins" / "known_marketplaces.json", json.dumps({
        OFFICIAL: {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
                   "installLocation": str(clone), "lastUpdated": "2026-09-30T20:11:07.409Z"}}))
    _write(clone / ".claude-plugin" / "marketplace.json", json.dumps({
        "name": OFFICIAL, "plugins": [
            {"name": "agent-sdk-dev", "description": "Development kit for working with the Claude Agent SDK",
             "source": "./plugins/agent-sdk-dev", "category": "development",
             "homepage": "https://github.com/anthropics/claude-plugins-public/tree/main/plugins/agent-sdk-dev"},
            {"name": "42crunch-api-security-testing", "description": "x", "category": "security",
             "source": {"source": "git-subdir", "url": "https://github.com/42Crunch-AI/claude-plugins.git"}},
            {"name": "agentforce-adlc", "description": "x", "category": "development",
             "source": {"source": "url", "url": "https://github.com/SalesforceAIResearch/agentforce-adlc.git"}},
            {"name": "asana", "description": "x", "category": "productivity",
             "source": "./external_plugins/asana"},
        ]}))
    sdk = clone / "plugins" / "agent-sdk-dev"
    _write(sdk / "README.md", "# Agent SDK dev\n" + "word " * 1200)
    _write(sdk / "agents" / "agent-sdk-verifier-py.md", "---\nname: agent-sdk-verifier-py\n---\n")
    _write(sdk / "agents" / "agent-sdk-verifier-ts.md", "---\nname: agent-sdk-verifier-ts\n---\n")
    _write(sdk / "commands" / "new-sdk-app.md", "Make an app\n")
    asana = clone / "external_plugins" / "asana"
    _write(asana / ".mcp.json", json.dumps({"asana": {"type": "http", "url": "https://mcp.asana.com/v2/mcp"}}))
    _write(asana / "skills" / "asana-setup" / "SKILL.md",
           "---\nname: asana-setup\ndescription: One-time Asana OAuth setup.\n---\nbody\n")
    _write(asana / "hooks" / "hooks.json", json.dumps({"hooks": {}}))
    return config


FIX = json.loads(FIXTURE.read_text())
SCRATCH_CWD = FIX["installed"][0]["projectPath"]


class Rig:
    def __init__(self, tmp: Path, start=None) -> None:
        assert skills is not None, "server/skills.py does not exist"
        self.cli = FakeCLI()
        self.clock = Clock()
        self.config = make_config(tmp)
        self.atlas = tmp / "atlas"
        self.atlas.mkdir()
        self.desks = [
            Desk(name="scratch", cwd=SCRATCH_CWD, engine="claude", mission=""),
            Desk(name="atlas", cwd=str(self.atlas), engine="claude", mission=""),
            Desk(name="coder", cwd=str(tmp), engine="opencode", mission=""),
        ]
        self.started: list = []
        self.lock_path = tmp / "skills.lock"
        self.svc = skills.Skills(
            runner=self.cli, config_dir=self.config, desks=lambda: self.desks,
            lock_path=self.lock_path, clock=self.clock,
            start=start if start is not None else (lambda fn: fn()))

    def item(self, pid: str) -> dict:
        return next(i for i in self.svc.catalog(limit=500)["items"] if i["id"] == pid)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _refusal(exc_info) -> tuple[int, str]:
    return exc_info.value.status, exc_info.value.reason


# ── parsing and redaction ────────────────────────────────────────────────────


def test_the_result_is_the_last_json_line_even_after_human_text():
    assert skills is not None, "server/skills.py does not exist"
    out = ('"cmd-probe" is installed by running a command from marketplace "probe-mkt":\n  true\n'
           'Not an interactive terminal, so the command was only displayed.\n'
           '{"command":"install","outcome":"failed","failureCode":"command_source_refused"}\n')
    assert skills.parse_result(out)["failureCode"] == "command_source_refused"
    assert skills.parse_result("no json here\n") is None


def test_secrets_are_redacted_from_a_failure_line():
    assert skills is not None, "server/skills.py does not exist"
    line = ("fatal: could not read https://bob:hunter2@github.com/x.git token=abc123 "
            "Authorization: Bearer sk-ant-oat01-SECRETSECRET ghp_ABCDEF0123456789")
    red = skills.redact(line)
    for secret in ("hunter2", "abc123", "sk-ant-oat01-" + "SECRETSECRET", "ghp_" + "ABCDEF0123456789"):
        assert secret not in red
    assert "github.com/x.git" in red


# ── catalog ──────────────────────────────────────────────────────────────────


def test_catalog_joins_available_installed_and_marketplace_category(rig):
    out = rig.svc.catalog()
    ids = [i["id"] for i in out["items"]]
    assert out["total"] == 5 and sorted(ids) == sorted([SDK, CRUNCH, ADLC, ASANA, CMD])
    assert ids[0] == SDK, "installed first"
    assert ids[1:] == [ASANA, CRUNCH, ADLC, CMD], "then install_count desc, unknown last"
    sdk = out["items"][0]
    assert sdk["installed"] is True and sdk["enabled"] is True
    assert sdk["scope"] == "all" and sdk["desks"] == ["scratch"]
    assert sdk["category"] == "development"
    assert sdk["description"] == "Development kit for working with the Claude Agent SDK"
    # measured: an installed plugin drops out of `available`, so its count is
    # only known if an earlier listing saw it
    assert sdk["install_count"] is None and sdk["official"] is True
    assert sdk["marketplace"] == OFFICIAL and sdk["name"] == "agent-sdk-dev"
    assert out["stale"] is False and isinstance(out["refreshed_at"], str)
    assert set(sdk) == {"id", "name", "description", "marketplace", "official", "category",
                        "source", "install_count", "installed", "enabled", "scope", "desks"}


def test_source_kinds_are_mapped(rig):
    crunch = rig.item(CRUNCH)
    assert crunch["source"] == {"kind": "git-subdir", "url": "https://github.com/42Crunch-AI/claude-plugins.git",
                                "ref": "v1.5.5"}
    assert rig.item(ADLC)["source"]["kind"] == "url"
    assert rig.item(ASANA)["source"] == {"kind": "path", "url": None, "ref": None}
    assert rig.item(SDK)["source"]["url"].startswith("https://github.com/anthropics/")
    cmd = rig.item(CMD)
    assert cmd["source"]["kind"] == "command" and cmd["official"] is False
    assert cmd["category"] is None and cmd["install_count"] is None
    assert cmd["installed"] is False and cmd["enabled"] is None and cmd["scope"] is None
    assert cmd["desks"] == []


def test_search_filters_and_paging(rig):
    assert [i["id"] for i in rig.svc.catalog(q="OWASP")["items"]] == [CRUNCH]
    assert rig.svc.catalog(q="agent")["total"] == 2  # agent-sdk-dev + agentforce-adlc (name)
    assert {i["id"] for i in rig.svc.catalog(category="development")["items"]} == {SDK, ADLC}
    assert [i["id"] for i in rig.svc.catalog(installed=True)["items"]] == [SDK]
    assert SDK not in [i["id"] for i in rig.svc.catalog(installed=False)["items"]]
    page = rig.svc.catalog(limit=2, offset=1)
    assert page["total"] == 5 and [i["id"] for i in page["items"]] == [ASANA, CRUNCH]


def test_the_listing_is_cached_for_ten_minutes(rig):
    rig.svc.catalog()
    rig.svc.catalog(q="x")
    assert len(rig.cli.verbs("list")) == 1
    rig.clock.now += 599
    rig.svc.catalog()
    assert len(rig.cli.verbs("list")) == 1
    rig.clock.now += 2
    rig.svc.catalog()
    assert len(rig.cli.verbs("list")) == 2
    assert rig.cli.verbs("list")[0][0] == ["plugin", "list", "--json", "--available"]


def test_marketplace_update_runs_at_most_every_six_hours_in_the_background(tmp_path):
    pending: list = []
    r = Rig(tmp_path, start=pending.append)
    r.svc.catalog()
    assert len(pending) == 1 and not r.cli.verbs("marketplace"), "queued, not run inline"
    pending.pop()()
    assert r.cli.verbs("marketplace")[0][0] == ["plugin", "marketplace", "update"]
    r.clock.now += 5 * 3600
    r.svc.catalog()
    assert not pending
    r.clock.now += 3601
    r.svc.catalog()
    assert len(pending) == 1


def test_a_failed_refresh_serves_the_old_listing_as_stale(rig):
    rig.svc.catalog()
    rig.clock.now += 700
    rig.cli.raise_next = FileNotFoundError("claude")
    out = rig.svc.catalog()
    assert out["stale"] is True and out["total"] == 5


def test_no_cli_and_no_cache_is_cli_missing(rig):
    rig.cli.raise_next = FileNotFoundError("claude")
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.catalog()
    assert _refusal(exc) == (503, "cli_missing")


def test_skills_dir_and_synced_plugins_carry_their_own_scope(rig):
    rig.cli.installed.append({"id": "mine@skills-dir", "scope": "user", "enabled": True,
                              "installPath": "/x/.claude/skills/mine"})
    rig.cli.installed.append({"id": "team@claude-ai", "scope": "user", "enabled": True,
                              "installPath": "/x/.claude/plugins/synced/b/team"})
    assert rig.item("mine@skills-dir")["scope"] == "skills-dir"
    assert rig.item("team@claude-ai")["scope"] == "synced"


# ── detail ───────────────────────────────────────────────────────────────────


def test_detail_reads_readme_and_components_from_the_clone(rig):
    sdk = rig.svc.detail(SDK)
    assert sdk["id"] == SDK and sdk["installed"] is True
    assert sdk["readme"].startswith("# Agent SDK dev") and len(sdk["readme"]) <= 4000
    assert sdk["components"] == {"skills": [], "commands": ["new-sdk-app"],
                                 "agents": ["agent-sdk-verifier-py", "agent-sdk-verifier-ts"],
                                 "hooks": False, "mcp_servers": []}
    asana = rig.svc.detail(ASANA)
    assert asana["components"]["skills"] == [{"name": "asana-setup",
                                              "description": "One-time Asana OAuth setup."}]
    assert asana["components"]["mcp_servers"] == ["asana"] and asana["components"]["hooks"] is True
    assert asana["readme"] is None


def test_detail_prefers_the_installed_copy(rig, tmp_path):
    inst = tmp_path / "cache" / "crunch"
    _write(inst / "README.md", "installed readme")
    _write(inst / ".mcp.json", json.dumps({"mcpServers": {"42c": {}}}))
    rig.cli.installed.append({"id": CRUNCH, "scope": "user", "enabled": False, "installPath": str(inst)})
    d = rig.svc.detail(CRUNCH)
    assert d["readme"] == "installed readme" and d["components"]["mcp_servers"] == ["42c"]
    assert d["enabled"] is False


def test_detail_of_an_external_uninstalled_plugin_has_no_contents(rig):
    d = rig.svc.detail(CRUNCH)
    assert d["readme"] is None and d["components"] is None


def test_detail_of_an_unknown_id_is_unknown_skill(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.detail("nope@nowhere")
    assert _refusal(exc) == (404, "unknown_skill")


# ── install ──────────────────────────────────────────────────────────────────


def test_install_for_all_agents_is_user_scope_and_an_async_op(rig):
    op = rig.svc.install(CRUNCH, "all")
    assert op["state"] == "running" and op["op_id"].startswith("sk_") and len(op["op_id"]) == 15
    assert rig.cli.verbs("install")[-1] == (
        ["plugin", "install", CRUNCH, "--json", "-s", "user"], None)
    status = rig.svc.op(op["op_id"])
    assert status["state"] == "done" and status["reason"] is None
    assert status["applies"] == "next_session"
    item = rig.item(CRUNCH)
    assert item["installed"] and item["scope"] == "all", "the cache was dropped after the op"
    assert item["install_count"] == 3327, "the count seen before install is remembered"


def test_install_for_one_agent_is_local_scope_in_its_cwd(rig):
    op = rig.svc.install(CRUNCH, "desk", desk="atlas")
    assert rig.cli.verbs("install")[-1] == (
        ["plugin", "install", CRUNCH, "--json", "-s", "local"], str(rig.atlas))
    assert rig.svc.op(op["op_id"])["state"] == "done"
    item = rig.item(CRUNCH)
    assert item["scope"] == "desk" and item["desks"] == ["atlas"]


@pytest.mark.parametrize("scope,desk,want", [
    ("everyone", None, (400, "bad_scope")),
    ("desk", None, (400, "bad_scope")),
    ("desk", "ghost", (404, "unknown_desk")),
    ("desk", "coder", (404, "unknown_desk")),  # not a Claude Code agent
])
def test_install_refuses_a_bad_target(rig, scope, desk, want):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(CRUNCH, scope, desk=desk)
    assert _refusal(exc) == want
    assert not rig.cli.verbs("install")


def test_install_refuses_unknown_and_already_installed(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install("nope@nowhere", "all")
    assert _refusal(exc) == (404, "unknown_skill")
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(SDK, "all")
    assert _refusal(exc) == (409, "already_installed")
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(SDK, "desk", desk="scratch")
    assert _refusal(exc) == (409, "already_installed")
    assert not rig.cli.verbs("install")
    rig.svc.install(SDK, "desk", desk="atlas")  # another desk is fine


def test_a_marketplace_command_needs_confirmation_by_its_sha(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(CMD, "all")
    assert _refusal(exc) == (409, "needs_confirmation")
    sha = _sha(CMD, "true")
    assert exc.value.extra == {"command": "true", "sha256": sha}
    assert "true" in exc.value.detail
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(CMD, "all", accept_command="0" * 64)
    assert _refusal(exc) == (409, "needs_confirmation"), "a stale sha is shown again"
    op = rig.svc.install(CMD, "all", accept_command=sha)
    assert rig.svc.op(op["op_id"])["state"] == "done"
    assert rig.cli.verbs("install")[-1][0] == [
        "plugin", "install", CMD, "--json", "-s", "user", "--accept-command", sha]
    assert all("-y" not in a and "--yes" not in a for a, _ in rig.cli.calls)


def test_an_accept_command_must_look_like_a_sha(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.install(CMD, "all", accept_command="yes; rm -rf /")
    assert _refusal(exc) == (400, "bad_input")


def test_a_command_surfacing_mid_install_fails_the_op_as_needs_confirmation(rig):
    rig.cli.fail_next = (1, json.dumps({
        "command": "install", "outcome": "failed", "failureCode": "command_source_refused",
        "message": "headersHelper not reviewed",
        "shownCommand": {"kind": "headers_helper", "command": "get-token.sh", "sha256": "ab" * 32}}) + "\n", "")
    op = rig.svc.install(CRUNCH, "all")
    st = rig.svc.op(op["op_id"])
    assert st["state"] == "failed" and st["reason"] == "needs_confirmation"
    assert st["command"] == "get-token.sh" and st["sha256"] == "ab" * 32


def test_a_failed_install_carries_the_last_stderr_line_redacted(rig):
    rig.cli.fail_next = (1, '{"command":"install","outcome":"failed","failureCode":"error_policy",'
                            '"message":"clone failed"}\n',
                         "Cloning…\nfatal: auth failed for https://bob:hunter2@github.com/x.git\n")
    st = rig.svc.op(rig.svc.install(CRUNCH, "all")["op_id"])
    assert st["state"] == "failed" and st["reason"] == "install_failed"
    assert st["detail"].startswith("fatal: auth failed") and "hunter2" not in st["detail"]


def test_timeout_and_missing_cli_fail_the_op_in_words(rig):
    rig.svc.catalog()
    rig.cli.raise_next = subprocess.TimeoutExpired(["claude"], 180)
    assert rig.svc.op(rig.svc.install(CRUNCH, "all")["op_id"])["reason"] == "timed_out"
    rig.svc.catalog()
    rig.cli.raise_next = FileNotFoundError("claude")
    assert rig.svc.op(rig.svc.install(ADLC, "all")["op_id"])["reason"] == "cli_missing"
    call = rig.cli.verbs("install")[0]
    assert call[0][2] == CRUNCH


def test_the_op_timeout_is_180_seconds(rig):
    seen = []
    orig = rig.cli.__call__

    def spy(args, cwd, timeout):
        seen.append((args[1], timeout))
        return orig(args, cwd, timeout)

    rig.svc.runner = spy
    rig.svc.install(CRUNCH, "all")
    assert ("install", 180) in seen


def test_unknown_op(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.op("sk_000000000000")
    assert _refusal(exc) == (404, "unknown_op")


def test_one_op_at_a_time(tmp_path):
    r = Rig(tmp_path, start=lambda fn: threading.Thread(target=fn, daemon=True).start())
    r.cli.hold = threading.Event()
    op = r.svc.install(CRUNCH, "all")
    assert r.cli.entered.wait(5)
    with pytest.raises(skills.SkillsError) as exc:
        r.svc.install(ADLC, "all")
    assert _refusal(exc) == (409, "busy")
    with pytest.raises(skills.SkillsError) as exc:
        r.svc.uninstall(SDK, "all")
    assert _refusal(exc) == (409, "busy")
    assert r.svc.op(op["op_id"])["state"] == "running"
    r.cli.hold.set()
    deadline = time.time() + 5
    while r.svc.op(op["op_id"])["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert r.svc.op(op["op_id"])["state"] == "done"
    r.svc.install(ADLC, "all")


def test_another_process_holding_the_lock_is_busy(rig):
    with open(rig.lock_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(skills.SkillsError) as exc:
            rig.svc.install(CRUNCH, "all")
        assert _refusal(exc) == (409, "busy")
    rig.svc.install(CRUNCH, "all")


# ── uninstall / enable / disable ─────────────────────────────────────────────


def test_uninstall_all_and_one_desk(rig):
    out = rig.svc.uninstall(SDK, "desk", desk="scratch")
    assert out == {"ok": True, "applies": "next_session"}
    assert rig.cli.verbs("uninstall")[-1] == (
        ["plugin", "uninstall", SDK, "--json", "-s", "local"], SCRATCH_CWD)
    assert rig.item(SDK)["desks"] == []
    rig.svc.uninstall(SDK, "all")
    assert rig.cli.verbs("uninstall")[-1] == (["plugin", "uninstall", SDK, "--json", "-s", "user"], None)
    assert rig.item(SDK)["installed"] is False
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.uninstall(SDK, "all")
    assert _refusal(exc) == (404, "not_installed")


def test_uninstall_of_an_unknown_id(rig):
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.uninstall("nope@nowhere", "all")
    assert _refusal(exc) == (404, "unknown_skill")


def test_enable_and_disable_are_scoped_and_idempotent(rig):
    out = rig.svc.set_enabled(SDK, False)
    assert out == {"ok": True, "enabled": False, "applies": "next_session"}
    assert rig.cli.verbs("disable")[-1] == (["plugin", "disable", SDK, "--json", "-s", "user"], None)
    assert rig.item(SDK)["enabled"] is False
    assert rig.svc.set_enabled(SDK, False)["ok"] is True, "already_in_goal_state is fine"
    rig.svc.set_enabled(SDK, True, scope="desk", desk="scratch")
    assert rig.cli.verbs("enable")[-1] == (["plugin", "enable", SDK, "--json", "-s", "local"], SCRATCH_CWD)
    with pytest.raises(skills.SkillsError) as exc:
        rig.svc.set_enabled(CRUNCH, True)
    assert _refusal(exc) == (404, "not_installed")


def test_a_desk_only_install_reads_enabled_from_its_local_settings(rig):
    rig.svc.install(CRUNCH, "desk", desk="atlas")
    _write(rig.atlas / ".claude" / "settings.local.json",
           json.dumps({"enabledPlugins": {CRUNCH: False}}))
    assert rig.item(CRUNCH)["enabled"] is False
