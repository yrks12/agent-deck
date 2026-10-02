"""Connectors & Skills (the store): catalog, trust, install, uninstall, secrets.

Every source is parsed from a REAL-FORMAT fixture saved on 2026-09-30:

* `anthropic_skills_marketplace.json` -- anthropics/skills
  `.claude-plugin/marketplace.json`, verbatim.
* `github_tree.json` -- `GET /repos/anthropics/skills/git/trees/<sha>?recursive=1`,
  trimmed to two skill folders.
* `frontend_design_SKILL.md` -- that skill's SKILL.md at the same commit.
* `registry_servers.json` -- `GET registry.modelcontextprotocol.io/v0/servers`,
  eight real entries: five vendor-official, and three that LOOK official and
  are not (`com.microsoft/esrp-oss-mcp-test` points at a personal repo,
  `app.vercel.auditflag-lac` is anyone's Vercel app, a smithery re-host).
* `github_repo.json`, `github_search.json` -- api.github.com shapes.

No network: every URL goes through `Fake`, which fails the test on any URL it
was not told about.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path

import pytest

from server import deck_mcp, roster, vault

try:  # absent, every test must FAIL on behaviour, not error at collection
    from server import connectors
except ImportError:  # pragma: no cover - the RED state
    connectors = None

FX = Path(__file__).parent / "fixtures" / "connectors"
SHA = "8a1541c4a3ffa5a20a5a91de0dcf3f0bab1d1ef4"
NEW_SHA = "b" * 40
PLUGINS_SHA = "c" * 40
KEY = "ctx7sk-live-0123456789abcdef-not-real"

MS_LEARN = "mcp:com.microsoft/microsoft-learn-mcp"
CONTEXT7 = "mcp:io.github.upstash/context7"
NOTION = "mcp:com.notion/mcp"
BRAVE = "mcp:io.github.brave/brave-search-mcp-server"
GITHUB = "mcp:io.github.github/github-mcp-server"
ESRP = "mcp:com.microsoft/esrp-oss-mcp-test"
VERCEL_APP = "mcp:app.vercel.auditflag-lac/auditflag"
SMITHERY = "mcp:ai.smithery/smithery-ai-slack"
PDF = "skill:anthropics/skills:pdf"
FRONTEND = "skill:anthropics/skills:frontend-design"


def _refs(sha: str) -> bytes:
    """`git-upload-pack` ref advertisement, as github.com serves it."""
    return (b"001e# service=git-upload-pack\n0000015" + sha.encode() +
            b" HEAD\x00multi_ack thin-pack symref=HEAD:refs/heads/main\n003f" +
            sha.encode() + b" refs/heads/main\n0000")


def _registry_page(query: dict) -> bytes:
    servers = json.loads((FX / "registry_servers.json").read_text())["servers"]
    want = (query.get("search") or [""])[0]
    found = [s for s in servers if want in s["server"]["name"]]
    return json.dumps({"servers": found,
                       "metadata": {"count": len(found)}}).encode()


class Fake:
    """Every URL the store may fetch, answered from fixtures. Records calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.sha = SHA
        self.down = False
        #: GitHub's REST API out of its 60-an-hour anonymous budget (403).
        self.api_limited = False

    def __call__(self, url: str, headers: dict | None = None,
                 timeout: float = 20.0) -> tuple[int, bytes]:
        self.calls.append(url)
        if self.down:
            raise OSError("network down")
        if self.api_limited and url.startswith("https://api.github.com/repos/") \
                and "/git/trees/" not in url:
            return 403, b'{"message":"API rate limit exceeded"}'
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.netloc == "registry.modelcontextprotocol.io":
            return 200, _registry_page(query)
        if url == "https://api.github.com/repos/anthropics/skills":
            return 200, json.dumps({"full_name": "anthropics/skills",
                                    "html_url": "https://github.com/anthropics/skills",
                                    "stargazers_count": 179182,
                                    "pushed_at": "2026-09-29T02:20:07Z"}).encode()
        if url == ("https://github.com/anthropics/skills.git/info/refs"
                   "?service=git-upload-pack"):
            return 200, _refs(self.sha)
        if url.startswith("https://api.github.com/repos/anthropics/skills/git/trees/"):
            return 200, (FX / "github_tree.json").read_bytes()
        if url == "https://api.github.com/repos/anthropics/claude-plugins-official":
            return 200, json.dumps({"full_name": "anthropics/claude-plugins-official",
                                    "html_url": "https://github.com/anthropics/claude-plugins-official",
                                    "stargazers_count": 37246,
                                    "pushed_at": "2026-09-30T22:40:08Z"}).encode()
        if url == ("https://github.com/anthropics/claude-plugins-official.git/info/refs"
                   "?service=git-upload-pack"):
            return 200, _refs(PLUGINS_SHA)
        if url == (f"https://raw.githubusercontent.com/anthropics/claude-plugins-official/"
                   f"{PLUGINS_SHA}/.claude-plugin/marketplace.json"):
            return 200, (FX / "claude_plugins_official_marketplace.json").read_bytes()
        if url == "https://api.github.com/repos/MicrosoftDocs/mcp":
            return 200, (FX / "github_repo.json").read_bytes()
        if url.startswith("https://api.github.com/search/repositories"):
            return 200, (FX / "github_search.json").read_bytes()
        if url.startswith("https://api.github.com/repos/"):
            return 404, b'{"message":"Not Found"}'
        prefix = "https://raw.githubusercontent.com/anthropics/skills/"
        if url.startswith(prefix):
            rest = url[len(prefix):]
            sha, _, path = rest.partition("/")
            if path == ".claude-plugin/marketplace.json":
                return 200, (FX / "anthropic_skills_marketplace.json").read_bytes()
            if path == "skills/frontend-design/SKILL.md":
                return 200, (FX / "frontend_design_SKILL.md").read_bytes()
            if path == "skills/pdf/SKILL.md":
                return 200, (b"---\nname: pdf\ndescription: Use this skill whenever "
                             b"the user wants to do anything with PDF files.\n---\n"
                             b"# PDF Processing Guide\n")
            if path.startswith("skills/"):
                return 200, f"{sha}:{path}\n".encode()
        raise AssertionError(f"the store fetched a URL no fixture answers: {url}")


def fake_probe(url: str) -> str:
    """The MCP `initialize` probe: MS Learn answers without a key, Notion 401s."""
    if "learn.microsoft.com" in url:
        return "none"
    if "notion.com" in url:
        return "oauth"
    return "unknown"


def no_post(url, data=b"", headers=None, timeout=20.0):
    raise AssertionError(f"the store POSTed to a URL no test answers: {url}")


def no_runner(args, cwd, timeout):
    raise AssertionError(f"the store ran `claude {' '.join(args)}` in a test")


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch=None) -> None:
        assert connectors is not None, "server/connectors.py does not exist"
        self.tmp = tmp_path
        self.fetch = Fake()
        self.reloads: list[tuple[str, str]] = []
        self.now = [1_790_000_000.0]
        for name in ("atlas", "globex"):
            (tmp_path / "ws" / name).mkdir(parents=True)
        self.roster_path = tmp_path / "roster.json"
        roster.save_roster(self.roster_path, [
            roster.Desk(name="atlas", cwd=str(tmp_path / "ws" / "atlas"),
                        engine="claude", mission="m"),
            roster.Desk(name="globex", cwd=str(tmp_path / "ws" / "globex"),
                        engine="claude", mission="m", reports_to="atlas"),
            # Shares globex's folder, as an ops and a growth desk can.
            roster.Desk(name="twin", cwd=str(tmp_path / "ws" / "globex"),
                        engine="claude", mission="m", reports_to="atlas"),
            roster.Desk(name="oc", cwd=str(tmp_path / "ws" / "atlas"),
                        engine="opencode", mission="m", reports_to="atlas"),
        ])
        self.root = tmp_path / "store"
        self.vault_path = tmp_path / "vault.json"
        self.post = no_post
        self.runner = no_runner
        if monkeypatch is not None:
            # deck_mcp.config reads the installed ledger from the module default.
            monkeypatch.setattr(connectors, "INSTALLED_PATH",
                                self.root / "installed.json")
        self.store = self.make()

    def make(self):
        return connectors.Store(
            fetch=self.fetch, probe=fake_probe, roster_path=self.roster_path,
            post=self.post, runner=self.runner,
            root=self.root, vault_path=self.vault_path,
            reloader=lambda desk, note: self.reloads.append((desk, note)) or {
                "desk": desk, "action": "after_turn", "detail": "test"},
            clock=lambda: self.now[0], background=False)

    def item(self, item_id: str) -> dict:
        return self.store.item(item_id)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# ── the catalog: parsing the real formats ───────────────────────────────────


def test_skills_are_one_item_per_folder_in_the_anthropic_marketplace(rig):
    page = rig.store.catalog(kind="skill")
    ids = {i["id"] for i in page["items"]}
    # The marketplace lists 19 skill paths; the tree fixture holds two of them,
    # and a path with no SKILL.md at the pinned commit is not an item.
    assert ids == {PDF, FRONTEND}
    fd = rig.item(FRONTEND)
    assert fd["kind"] == "skill" and fd["name"] == "frontend-design"
    assert fd["description"].startswith("Guidance for distinctive")
    assert fd["publisher"] == "Anthropic"
    assert fd["source"] == "anthropic-skills"
    assert fd["repo"] == "https://github.com/anthropics/skills"
    assert fd["stars"] == 179182 and fd["updated_at"] == "2026-09-29T02:20:07Z"
    assert fd["trust"] == "official"
    assert fd["version"] == SHA[:7]
    assert fd["installable"] is True and fd["auth"] == "none"


def test_a_no_key_remote_connector_from_the_registry(rig):
    ms = rig.item(MS_LEARN)
    assert ms["kind"] == "connector" and ms["source"] == "mcp-registry"
    assert ms["title"] == "Microsoft Learn MCP"
    assert ms["publisher"] == "Microsoft"
    assert ms["repo"] == "https://github.com/MicrosoftDocs/mcp"
    assert ms["version"] == "1.0.0"
    assert ms["auth"] == "none" and ms["secrets"] == []
    assert ms["installable"] is True and ms["why_not"] is None
    repo = json.loads((FX / "github_repo.json").read_text())
    assert ms["stars"] == repo["stargazers_count"]
    assert ms["updated_at"] == repo["pushed_at"]


def test_a_remote_with_a_secret_header_asks_for_that_key(rig):
    c7 = rig.item(CONTEXT7)
    assert c7["auth"] == "api_key"
    assert [s["name"] for s in c7["secrets"]] == ["AUTHORIZATION"]
    assert c7["secrets"][0]["required"] is True
    assert "API key" in c7["secrets"][0]["description"]


def test_a_package_with_a_secret_env_var_asks_for_it(rig):
    brave = rig.item(BRAVE)
    assert brave["auth"] == "api_key"
    assert [s["name"] for s in brave["secrets"]] == ["BRAVE_API_KEY"]
    assert brave["installable"] is True


def test_an_oauth_remote_is_installable_through_connect(rig):
    notion = rig.item(NOTION)
    assert notion["trust"] == "official" and notion["publisher"] == "Notion"
    assert notion["auth"] == "oauth"
    # Installable -- but only through Connect (tests/test_connectors_oauth.py):
    # a plain install without a sign-in is refused with `needs_oauth`.
    assert notion["installable"] is True and notion["why_not"] is None


def test_github_search_adds_official_repos_the_registry_lacks(rig):
    # The one search hit is github/github-mcp-server, which IS in the registry:
    # it must not appear twice.
    ids = [i["id"] for i in rig.store.catalog(kind="connector", limit=200)["items"]]
    assert ids.count(GITHUB) == 1
    assert "gh:github/github-mcp-server" not in ids


# ── trust ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("item_id,publisher", [
    (MS_LEARN, "Microsoft"), (CONTEXT7, "Upstash"), (NOTION, "Notion"),
    (BRAVE, "Brave"), (GITHUB, "GitHub"), (PDF, "Anthropic")])
def test_vendor_items_are_official(rig, item_id, publisher):
    item = rig.item(item_id)
    assert item["trust"] == "official"
    assert item["publisher"] == publisher
    assert publisher in item["trust_note"]


@pytest.mark.parametrize("name,repo,why", [
    # A vendor namespace pointing at a stranger's repo.
    ("com.microsoft/esrp-oss-mcp-test",
     "https://github.com/sravanism/esrp-oss-mcp-server", "repo"),
    # `app.vercel` is Vercel's hosting domain: anyone's app lives under it.
    ("app.vercel.auditflag-lac/auditflag", None, "namespace"),
    ("ai.smithery/smithery-ai-slack", "https://github.com/smithery-ai/mcp-servers",
     "namespace"),
    ("io.github.someone/github-mcp-server", "https://github.com/someone/x", "namespace"),
    # Prefix is not ownership.
    ("com.microsoftx/tool", None, "namespace"),
])
def test_look_alikes_are_unverified(name, repo, why):
    assert connectors is not None
    trust, _publisher, _note = connectors.classify_registry(name, repo)
    assert trust == "unverified", why


def test_nothing_is_verified_without_proof():
    assert connectors is not None
    seen = {connectors.classify_registry(n, r)[0] for n, r in [
        ("com.microsoft/microsoft-learn-mcp", "https://github.com/MicrosoftDocs/mcp"),
        ("ac.inference.sh/mcp", None)]}
    assert seen == {"official", "unverified"}


def test_the_trusted_catalog_hides_unverified_items(rig):
    ids = {i["id"] for i in rig.store.catalog(kind="connector", limit=200)["items"]}
    assert {MS_LEARN, CONTEXT7, NOTION, BRAVE, GITHUB} <= ids
    assert not ids & {ESRP, VERCEL_APP, SMITHERY}


def test_other_sources_are_searched_live_and_marked_unverified(rig):
    page = rig.store.catalog(kind="connector", q="auditflag", trust="all")
    got = {i["id"]: i for i in page["items"]}
    assert VERCEL_APP in got
    assert got[VERCEL_APP]["trust"] == "unverified"
    assert got[VERCEL_APP]["publisher"]  # the namespace, said as it is


# ── the cache ────────────────────────────────────────────────────────────────


def test_the_catalog_is_cached_on_disk_and_refreshed_daily(rig):
    rig.store.catalog()
    first = len(rig.fetch.calls)
    assert first > 0
    again = rig.make()               # a new process: reads the disk cache
    again.catalog()
    assert len(rig.fetch.calls) == first
    rig.now[0] += 25 * 3600          # a day later
    again.catalog()
    assert len(rig.fetch.calls) > first


def test_a_failed_refresh_serves_the_last_good_copy_as_stale(rig):
    rig.store.catalog()
    rig.fetch.down = True
    rig.now[0] += 25 * 3600
    page = rig.make().catalog(kind="connector")
    assert page["stale"] is True
    assert page["items"]


def test_search_filters_by_name_and_description(rig):
    page = rig.store.catalog(q="docs & code samples")
    assert [i["id"] for i in page["items"]] == [MS_LEARN]


# ── install: skills ──────────────────────────────────────────────────────────


def test_a_skill_installs_into_each_desks_skills_dir_at_the_pinned_commit(rig):
    out = rig.store.install(PDF, ["atlas", "globex"])
    assert out["ok"] is True
    for desk in ("atlas", "globex"):
        folder = rig.tmp / "ws" / desk / ".claude" / "skills" / "pdf"
        assert (folder / "SKILL.md").read_text().startswith("---\nname: pdf")
        # Every file came from the pinned commit, never from `main`.
        assert (folder / "scripts" / "fill_fillable_fields.py").read_text() == \
            f"{SHA}:skills/pdf/scripts/fill_fillable_fields.py\n"
        marker = json.loads((folder / ".deck-install.json").read_text())
        assert marker["id"] == PDF and marker["commit"] == SHA
    assert all(SHA in u for u in rig.fetch.calls
               if u.startswith("https://raw.githubusercontent.com/anthropics/skills/"))
    assert sorted(d for d, _ in rig.reloads) == ["atlas", "globex"]
    assert {r["desk"] for r in out["reload"]} == {"atlas", "globex"}
    assert {(i["desk"], i["commit"]) for i in out["installed"]} == {
        ("atlas", SHA), ("globex", SHA)}


def test_desks_that_share_a_folder_share_its_skills(rig):
    rig.store.install(PDF, ["globex"])
    twin = rig.store.installed("twin")["desks"]
    assert [i["id"] for i in twin[0]["items"]] == [PDF]
    assert set(rig.item(PDF)["installed_on"]) == {"globex", "twin"}


def test_all_desks_means_every_claude_desk(rig):
    rig.store.install(MS_LEARN, "all")
    assert set(rig.item(MS_LEARN)["installed_on"]) == {"atlas", "globex", "twin"}


def test_a_skill_the_deck_did_not_install_is_never_overwritten_or_removed(rig):
    mine = rig.tmp / "ws" / "atlas" / ".claude" / "skills" / "pdf"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("his own")
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(PDF, ["atlas"])
    assert exc.value.reason == "conflict"
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.uninstall(PDF, ["atlas"])
    assert exc.value.reason == "not_installed"
    assert (mine / "SKILL.md").read_text() == "his own"


def test_uninstalling_a_skill_removes_its_folder_and_reloads(rig):
    rig.store.install(PDF, ["atlas"])
    rig.reloads.clear()
    out = rig.store.uninstall(PDF, ["atlas"])
    assert out["removed"] == [{"desk": "atlas", "id": PDF}]
    assert not (rig.tmp / "ws" / "atlas" / ".claude" / "skills" / "pdf").exists()
    assert [d for d, _ in rig.reloads] == ["atlas"]
    assert rig.store.installed("atlas")["desks"][0]["items"] == []


def test_a_new_commit_offers_an_update_and_update_pins_it(rig):
    rig.store.install(PDF, ["atlas"])
    rig.fetch.sha = NEW_SHA
    rig.now[0] += 25 * 3600
    rig.store.refresh(force=True)
    (row,) = rig.store.installed("atlas")["desks"][0]["items"]
    assert row["update_available"] is True and row["commit"] == SHA
    rig.store.update(PDF, ["atlas"])
    (row,) = rig.store.installed("atlas")["desks"][0]["items"]
    assert row["commit"] == NEW_SHA and row["update_available"] is False


# ── install: connectors ──────────────────────────────────────────────────────


def _servers(desk: str) -> dict:
    return json.loads(deck_mcp.config(desk))["mcpServers"]


def test_a_connector_lands_in_that_desks_mcp_config_only(rig):
    out = rig.store.install(MS_LEARN, ["atlas"])
    assert out["ok"] is True
    servers = _servers("atlas")
    assert {"computer", "deck", "mac"} <= set(servers)
    (name,) = set(servers) - {"computer", "deck", "mac"}
    assert servers[name] == {"type": "http", "url": "https://learn.microsoft.com/api/mcp"}
    assert set(_servers("globex")) <= {"computer", "deck", "mac"}
    (row,) = rig.store.installed("atlas")["desks"][0]["items"]
    assert row["id"] == MS_LEARN and row["version"] == "1.0.0"
    assert [d for d, _ in rig.reloads] == ["atlas"]
    assert name in rig.reloads[0][1]  # the reload note names the server


def test_a_connector_key_goes_to_the_vault_granted_to_those_desks_only(rig, caplog):
    caplog.set_level(logging.DEBUG)
    out = rig.store.install(CONTEXT7, ["atlas"], secrets={"AUTHORIZATION": KEY})
    assert KEY not in json.dumps(out)
    assert KEY not in json.dumps(rig.item(CONTEXT7))
    assert KEY not in json.dumps(rig.store.installed())
    assert KEY not in (rig.root / "installed.json").read_text()
    assert KEY not in deck_mcp.config("atlas")
    assert KEY not in caplog.text
    names = [m.name for m in vault.meta(rig.vault_path)]
    assert len(names) == 1 and names[0].startswith("CONNECTOR_")
    assert vault.env_for(rig.vault_path, "atlas") == {names[0]: KEY}
    assert vault.env_for(rig.vault_path, "globex") == {}
    # The header reaches the server through a helper that reads the vault at
    # run time -- never as a literal in the config.
    (server,) = [v for k, v in _servers("atlas").items()
                 if k not in ("computer", "deck", "mac")]
    assert "headersHelper" in server and "headers" not in server
    assert "server.connector_run" in server["headersHelper"]


def test_a_second_desk_reuses_the_stored_key(rig):
    rig.store.install(CONTEXT7, ["atlas"], secrets={"AUTHORIZATION": KEY})
    rig.store.install(CONTEXT7, ["globex"])  # no key sent: the vault has it
    (meta,) = vault.meta(rig.vault_path)
    assert set(meta.grants) == {"atlas", "globex"}


def test_a_missing_key_is_refused_by_field_name(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(BRAVE, ["atlas"])
    assert exc.value.reason == "missing_secret"
    assert "BRAVE_API_KEY" in exc.value.detail
    assert rig.reloads == []


def test_an_oauth_connector_is_refused_honestly(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(NOTION, ["atlas"])
    assert exc.value.reason == "needs_oauth"


def test_an_unverified_item_needs_an_explicit_accept(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(VERCEL_APP, ["atlas"])
    assert exc.value.reason == "unverified"
    assert rig.reloads == []
    out = rig.store.install(VERCEL_APP, ["atlas"], accept_unverified=True)
    assert out["ok"] is True


def test_an_unknown_desk_or_item_is_refused(rig):
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install(MS_LEARN, ["nobody"])
    assert exc.value.reason == "unknown_desk"
    with pytest.raises(connectors.StoreError) as exc:
        rig.store.install("mcp:com.example/none", ["atlas"])
    assert exc.value.reason == "unknown_item"


def test_uninstalling_a_connector_is_clean(rig):
    rig.store.install(CONTEXT7, ["atlas", "globex"], secrets={"AUTHORIZATION": KEY})
    rig.store.uninstall(CONTEXT7, ["atlas"])
    assert set(_servers("atlas")) <= {"computer", "deck", "mac"}
    (meta,) = vault.meta(rig.vault_path)
    assert meta.grants == ("globex",)
    rig.store.uninstall(CONTEXT7, ["globex"])
    assert vault.meta(rig.vault_path) == []   # the last grant takes the key
    assert set(_servers("globex")) <= {"computer", "deck", "mac"}
    assert rig.store.installed()["desks"] == [
        {"desk": d, "items": []} for d in ("atlas", "globex", "twin")]


def test_a_new_registry_version_offers_an_update(rig):
    rig.store.install(MS_LEARN, ["atlas"])
    ledger = json.loads((rig.root / "installed.json").read_text())
    ledger["desks"]["atlas"][MS_LEARN]["version"] = "0.9.0"
    (rig.root / "installed.json").write_text(json.dumps(ledger))
    (row,) = rig.store.installed("atlas")["desks"][0]["items"]
    assert row["update_available"] is True
    rig.store.update(MS_LEARN, ["atlas"])
    (row,) = rig.store.installed("atlas")["desks"][0]["items"]
    assert row["version"] == "1.0.0" and row["update_available"] is False


# ── the launcher: the key reaches the process, not the config ────────────────


def test_the_launcher_hands_a_stdio_connector_its_key_in_the_environment(rig):
    from server import connector_run
    rig.store.install(BRAVE, ["atlas"], secrets={"BRAVE_API_KEY": KEY})
    server = next(v for k, v in _servers("atlas").items()
                  if k not in ("computer", "deck", "mac"))
    assert KEY not in json.dumps(server)
    assert server["args"][:2] == ["-m", "server.connector_run"]
    argv, env = connector_run.resolve("atlas", BRAVE, vault_path=rig.vault_path)
    assert argv[:2] == ["npx", "-y"]
    assert argv[2] == "@brave/brave-search-mcp-server@2.1.3"
    assert env == {"BRAVE_API_KEY": KEY}
    with pytest.raises(connector_run.LaunchError):
        connector_run.resolve("globex", BRAVE, vault_path=rig.vault_path)


def test_the_headers_helper_prints_the_header_with_a_bearer_scheme(rig):
    from server import connector_run
    rig.store.install(CONTEXT7, ["atlas"], secrets={"AUTHORIZATION": KEY})
    headers = connector_run.headers("atlas", CONTEXT7, vault_path=rig.vault_path)
    assert headers == {"Authorization": f"Bearer {KEY}"}


def test_what_can_be_installed_is_listed_before_what_cannot(rig):
    """Live, on the box: a 17k-star curriculum repo from the GitHub search
    (listed, not installable) sat above connectors he could actually add."""
    rig.store.catalog()
    doc = json.loads((rig.root / "catalog.json").read_text())
    for item in doc["items"]:
        if item["id"] == NOTION:          # listed, not installable (OAuth)
            item["stars"] = 10 ** 6
    (rig.root / "catalog.json").write_text(json.dumps(doc))
    items = rig.store.catalog(kind="connector", limit=200)["items"]
    flags = [i["installable"] for i in items]
    assert flags == sorted(flags, reverse=True)


def test_a_removed_skill_is_named_as_gone_in_the_reload_note(rig):
    """MEASURED live: after a removal and a resume, the desk still said it had
    the skill -- the resumed conversation carries the skill list it was given
    at start. The reload note must say plainly that it is gone."""
    rig.store.install(PDF, ["atlas"])
    rig.reloads.clear()
    rig.store.uninstall(PDF, ["atlas"])
    ((desk, note),) = rig.reloads
    assert desk == "atlas"
    assert "pdf" in note and "no longer" in note.lower()
    assert "do not use" in note.lower()


def test_an_exhausted_github_api_still_lists_skills_and_plugins(rig):
    """MEASURED on the box: the anonymous REST budget (60 an hour) was spent on
    star counts, the plugin directory's metadata call answered 403, and the
    whole directory dropped out of the catalog. The commit to pin comes from
    git's own ref advertisement, and stars are a nicety that may be missing."""
    rig.fetch.api_limited = True
    kinds = {i["kind"] for i in rig.store.catalog(limit=200)["items"]}
    assert {"skill", "plugin", "connector"} <= kinds
    assert rig.item(PDF)["version"] == SHA[:7] and rig.item(PDF)["stars"] is None
