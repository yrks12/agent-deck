"""Anthropic's plugin directory (anthropics/claude-plugins-official) in the store.

The fixture is the directory's real `.claude-plugin/marketplace.json`, trimmed
to six entries covering every source kind measured on 2026-09-30: an in-repo
`./plugins/...` path (Anthropic's own), an in-repo `./external_plugins/...`
path (a partner's, kept by Anthropic), `git-subdir` and `url` (a partner's
repo, pinned to a sha).

Trust: `official` only for Anthropic's own (author Anthropic, source inside
`./plugins/`). Every other entry is `verified` -- listed in Anthropic's curated
directory, which is true and is all the badge says -- and never `official`.

Install runs the CLI exactly as measured in docs/skills.md: `claude plugin
install <name>@claude-plugins-official --json -s local` in the desk's folder,
never `-y`. The CLI's `plugin list --json` is what says where it is installed.
"""

from __future__ import annotations

import json

import pytest

from server.skills import Completed
from tests.test_connectors import Rig, connectors

MARKET = "claude-plugins-official"
SDK = f"plugin:agent-sdk-dev@{MARKET}"
ADOBE = f"plugin:adobe-for-creativity@{MARKET}"
ASANA = f"plugin:asana@{MARKET}"
ADLC = f"plugin:agentforce-adlc@{MARKET}"


class CLI:
    """`claude plugin ...` as measured, with a tiny state of its own."""

    def __init__(self) -> None:
        self.calls: list[tuple[list, str | None]] = []
        self.markets: list[str] = []
        self.installed: list[dict] = []
        self.fail = False

    def __call__(self, args, cwd, timeout):
        self.calls.append((list(args), cwd))
        if args[:3] == ["plugin", "marketplace", "list"]:
            return Completed(0, json.dumps([{"name": m} for m in self.markets]), "")
        if args[:3] == ["plugin", "marketplace", "add"]:
            self.markets.append(MARKET)
            return Completed(0, "", "")
        if args[:3] == ["plugin", "marketplace", "update"]:
            return Completed(0, "", "")
        if args[:2] == ["plugin", "list"]:
            return Completed(0, json.dumps({"installed": self.installed,
                                            "available": []}), "")
        if args[:2] == ["plugin", "install"]:
            if self.fail:
                return Completed(1, json.dumps({
                    "command": "install", "outcome": "failed", "failureCode": "not_found",
                    "message": "token=sk-ant-abc123 plugin not found"}),
                    "✘ Failed to install")
            self.installed.append({"id": args[2], "version": "ab024cdcfa7c", "scope": "local",
                                   "enabled": True, "projectPath": cwd,
                                   "installedAt": "2026-09-30T20:21:34.896Z"})
            return Completed(0, json.dumps({"command": "install", "outcome": "ok",
                                            "pluginId": args[2], "scope": "local"}), "")
        if args[:2] == ["plugin", "uninstall"]:
            self.installed = [r for r in self.installed
                              if not (r["id"] == args[2] and r["projectPath"] == cwd)]
            return Completed(0, json.dumps({"command": "uninstall", "outcome": "ok"}), "")
        raise AssertionError(f"unexpected claude {args}")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.cli = CLI()
    r.runner = r.cli
    r.store = r.make()
    return r


def test_the_directory_is_listed_as_plugins(rig):
    ids = {i["id"] for i in rig.store.catalog(kind="plugin", limit=200)["items"]}
    assert {SDK, ADOBE, ASANA, ADLC} <= ids
    sdk = rig.item(SDK)
    assert sdk["kind"] == "plugin" and sdk["source"] == "claude-plugins-official"
    assert sdk["repo"] == "https://github.com/anthropics/claude-plugins-official"
    assert sdk["stars"] == 37246 and sdk["installable"] is True
    adobe = rig.item(ADOBE)
    assert adobe["version"] == "acb6d76"          # the sha the directory pins
    assert adobe["repo"].startswith("https://github.com/adobe/skills")
    # Plugins are their own kind: the Skills tab does not change under them.
    assert all(i["kind"] == "skill" for i in rig.store.catalog(kind="skill")["items"])


@pytest.mark.parametrize("item_id,trust,publisher", [
    (SDK, "official", "Anthropic"),
    (ADOBE, "verified", "Adobe"),
    (ASANA, "verified", "asana"),
    (ADLC, "verified", "SalesforceAIResearch"),
])
def test_only_anthropics_own_plugins_are_official(rig, item_id, trust, publisher):
    item = rig.item(item_id)
    assert item["trust"] == trust
    assert item["publisher"] == publisher
    if trust == "verified":
        assert "Anthropic's plugin directory" in item["trust_note"]


def test_installing_a_plugin_runs_the_cli_in_each_desks_folder(rig):
    out = rig.store.install(ADOBE, ["atlas", "globex"])
    assert out["ok"] is True
    installs = [(a, c) for a, c in rig.cli.calls if a[:2] == ["plugin", "install"]]
    assert installs == [
        (["plugin", "install", f"adobe-for-creativity@{MARKET}", "--json", "-s", "local"],
         str(rig.tmp / "ws" / "atlas")),
        (["plugin", "install", f"adobe-for-creativity@{MARKET}", "--json", "-s", "local"],
         str(rig.tmp / "ws" / "globex")),
    ]
    # The directory is added once, before the first install, and never `-y`.
    adds = [a for a, _ in rig.cli.calls if a[:3] == ["plugin", "marketplace", "add"]]
    assert adds == [["plugin", "marketplace", "add", f"anthropics/{MARKET}"]]
    assert not any("-y" in a for a, _ in rig.cli.calls)
    assert sorted(d for d, _ in rig.reloads) == ["atlas", "globex"]
    # Folder-scoped, as measured: twin shares globex's folder, so it has it too.
    assert set(rig.item(ADOBE)["installed_on"]) == {"atlas", "globex", "twin"}
    (row,) = [i for i in rig.store.installed("atlas")["desks"][0]["items"]
              if i["id"] == ADOBE]
    assert row["kind"] == "plugin" and row["version"] == "ab024cdcfa7c"


def test_a_cli_failure_is_refused_with_its_secret_redacted(rig):
    rig.cli.fail = True
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(ADOBE, ["atlas"])
    assert exc.value.reason == "install_failed"
    assert "sk-ant-abc123" not in exc.value.detail
    assert rig.reloads == []


def test_uninstalling_a_plugin_runs_the_cli_and_reloads(rig):
    rig.store.install(SDK, ["atlas"])
    rig.reloads.clear()
    out = rig.store.uninstall(SDK, ["atlas"])
    assert out["removed"] == [{"desk": "atlas", "id": SDK}]
    assert (["plugin", "uninstall", f"agent-sdk-dev@{MARKET}", "--json", "-s", "local"],
            str(rig.tmp / "ws" / "atlas")) in rig.cli.calls
    assert [d for d, _ in rig.reloads] == ["atlas"]
    assert rig.item(SDK)["installed_on"] == []
