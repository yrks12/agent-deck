"""Connectors & Skills: a trusted store a desk (or the owner) installs from.

A **connector** is an MCP server; a **skill** is a `SKILL.md` folder. The store
reads three sources, caches what it trusts on disk for a day, and installs onto
one desk, some desks or all of them. See `docs/connectors.md` for the wire
contract and the measurements this module is built on.

Trust is a claim the badge makes to the owner, so it is only ever `official`
when the item PROVES its publisher (namespace AND repository owner both on the
`PUBLISHERS` list) -- a registry namespace alone only proves someone owns a
domain, and `app.vercel.<anything>` is anyone's Vercel app.

Secrets never leave the vault except through `vault.env_for`, which only
`server/connector_run.py` calls, at the moment a connector starts. Nothing in
this module logs, returns or raises with a value in it.

Every impure edge (HTTP, the auth probe, the reload, the clock) is injected, so
the tests run the real code over real-format fixtures with no network.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import re
import secrets as _secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import atomic, decisions, mcp_oauth, roster, vault
from . import owner as deck_owner
from .paths import BUS_DIR, CLAUDE_HOME

__all__ = ["Store", "StoreError", "classify_registry", "plan_reload", "Reloader",
           "servers_for", "default_store", "on_decision", "INSTALLED_PATH"]

ROOT = str(Path(__file__).resolve().parents[1])
STORE_DIR = BUS_DIR / "connectors"
#: Per-desk connectors. `deck_mcp.config` reads it on every spawn.
INSTALLED_PATH = STORE_DIR / "installed.json"
#: Where reload runs write what they did (never a secret: notes and slugs).
RELOAD_LOG = CLAUDE_HOME / "deck-mcp" / "reload.log"

REFRESH_EVERY = 24 * 3600.0
REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
SKILL_MARKER = ".deck-install.json"
MAX_SKILL_FILES = 300
MAX_SKILL_BYTES = 10 * 1024 * 1024
#: Names the deck's own servers already use in a desk's MCP config.
RESERVED_SERVERS = frozenset({"computer", "deck", "mac"})

TRUSTS = ("official", "verified", "unverified")
AUTHS = ("none", "api_key", "oauth", "unknown")

STATUS = {
    "bad_input": 400, "missing_secret": 400,
    "unknown_item": 404, "unknown_desk": 404, "not_installed": 404,
    "unverified": 409, "needs_oauth": 409, "not_installable": 409, "conflict": 409,
    "bad_state": 409, "oauth_denied": 409, "not_oauth": 409, "oauth_unsupported": 409,
    "fetch_failed": 502, "install_failed": 502, "oauth_register_failed": 502,
    "oauth_token_failed": 502,
}

OAUTH_WHY = ("This connector signs in with OAuth: press Connect and approve it in "
             "your browser. The deck never signs in for you.")
#: How long a Connect may sit at the provider's consent page.
CONNECT_TTL = 15 * 60
#: Refresh an access token this long before it expires.
REFRESH_MARGIN = 300.0
PLUGIN_SOURCE = {"owner": "anthropics", "repo": "claude-plugins-official",
                 "branch": "main", "market": "claude-plugins-official"}


class StoreError(Exception):
    """A refusal in the deck's `{ok, reason, detail}` shape."""

    def __init__(self, reason: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status = status or STATUS.get(reason, 400)

    def body(self) -> dict:
        return {"ok": False, "reason": self.reason, "detail": self.detail}


# ── who counts as official ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Publisher:
    name: str
    #: Registry namespaces (the part of a server name before "/"), lowercase.
    namespaces: tuple[str, ...]
    #: Source owners: "github.com/<org>" (lowercase), or another host's path.
    owners: tuple[str, ...]


def _gh(*orgs: str) -> tuple[str, ...]:
    return tuple(f"github.com/{o.lower()}" for o in orgs)


#: The vendors whose OWN servers and skills the store badges. A vendor is here
#: because its namespace and its GitHub org were checked by hand against the
#: registry on 2026-09-30. Adding one is a decision about what the owner's
#: agents run with full access: add the org, never a person.
PUBLISHERS: tuple[Publisher, ...] = (
    Publisher("Anthropic", ("com.anthropic", "io.github.anthropics"), _gh("anthropics")),
    Publisher("GitHub", ("com.github", "io.github.github"), _gh("github")),
    Publisher("Microsoft", ("com.microsoft", "io.github.microsoft"),
              _gh("microsoft", "MicrosoftDocs", "Azure", "NuGet")),
    Publisher("Notion", ("com.notion", "io.github.makenotion"), _gh("makenotion")),
    Publisher("Linear", ("app.linear", "io.github.linear"), _gh("linear")),
    Publisher("Stripe", ("com.stripe", "io.github.stripe"), _gh("stripe")),
    Publisher("Atlassian", ("com.atlassian", "io.github.atlassian"), _gh("atlassian")),
    Publisher("Sentry", ("io.sentry", "io.github.getsentry"), _gh("getsentry")),
    Publisher("Supabase", ("com.supabase", "io.github.supabase-community"),
              _gh("supabase", "supabase-community")),
    Publisher("Vercel", ("com.vercel", "io.github.vercel"), _gh("vercel")),
    Publisher("Cloudflare", ("com.cloudflare", "io.github.cloudflare"), _gh("cloudflare")),
    Publisher("Upstash", ("com.upstash", "io.github.upstash"), _gh("upstash")),
    Publisher("Brave", ("com.brave", "io.github.brave"), _gh("brave")),
    Publisher("Exa", ("ai.exa", "io.github.exa-labs"), _gh("exa-labs")),
    Publisher("Firecrawl", ("dev.firecrawl", "io.github.firecrawl"),
              _gh("firecrawl", "mendableai")),
    Publisher("Mapbox", ("com.mapbox", "io.github.mapbox"), _gh("mapbox")),
    Publisher("MongoDB", ("com.mongodb", "io.github.mongodb-js"), _gh("mongodb-js", "mongodb")),
    Publisher("Grafana", ("com.grafana", "io.github.grafana"), _gh("grafana")),
    Publisher("HashiCorp", ("com.hashicorp", "io.github.hashicorp"), _gh("hashicorp")),
    Publisher("PayPal", ("com.paypal", "io.github.paypal"), _gh("paypal")),
    Publisher("PostHog", ("com.posthog", "io.github.posthog"), _gh("PostHog")),
    Publisher("Postman", ("com.postman", "io.github.postmanlabs"), _gh("postmanlabs")),
    Publisher("monday.com", ("com.monday", "io.github.mondaycom"), _gh("mondaycom")),
    Publisher("Webflow", ("com.webflow", "io.github.webflow"), _gh("webflow")),
    Publisher("Wix", ("com.wix", "io.github.wix"), _gh("wix")),
    Publisher("Zapier", ("com.zapier", "io.github.zapier"), _gh("zapier")),
    Publisher("Apify", ("com.apify", "io.github.apify"), _gh("apify")),
    Publisher("Airtable", ("com.airtable",), _gh("Airtable")),
    Publisher("CircleCI", ("com.circleci",), _gh("CircleCI-Public")),
    Publisher("GitLab", ("com.gitlab",), ("gitlab.com/gitlab-org",)),
    Publisher("Hugging Face", ("co.huggingface", "io.github.huggingface"), _gh("huggingface")),
    Publisher("Auth0", ("com.auth0",), _gh("auth0")),
    Publisher("Snyk", ("io.snyk",), _gh("snyk")),
    Publisher("Browserbase", ("com.browserbase", "io.github.browserbase"), _gh("browserbase")),
    Publisher("Redis", ("io.github.redis",), _gh("redis")),
)

#: Skill repositories. Each is a Claude plugin marketplace whose plugins list
#: `./skills/<name>` folders (anthropics/skills, measured 2026-09-30).
SKILL_SOURCES = (
    {"owner": "anthropics", "repo": "skills", "branch": "main",
     "publisher": "Anthropic", "source": "anthropic-skills"},
)


def _owner_of(repo_url: str | None) -> str | None:
    """"github.com/microsoftdocs" for https://github.com/MicrosoftDocs/mcp."""
    if not repo_url:
        return None
    parsed = urllib.parse.urlparse(str(repo_url).strip())
    parts = [p for p in parsed.path.split("/") if p]
    if not parsed.netloc or not parts:
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    return f"{host}/{parts[0].lower()}"


def _short_repo(repo_url: str | None) -> str:
    parsed = urllib.parse.urlparse(str(repo_url or ""))
    return (parsed.netloc + parsed.path).removesuffix(".git").rstrip("/")


def _publisher_for_ns(ns: str) -> Publisher | None:
    ns = ns.lower()
    return next((p for p in PUBLISHERS if ns in p.namespaces), None)


def _publisher_for_owner(owner: str | None) -> Publisher | None:
    return next((p for p in PUBLISHERS if owner and owner in p.owners), None)


def classify_registry(name: str, repo_url: str | None) -> tuple[str, str, str]:
    """`(trust, publisher, note)` for a registry server name and its repo URL.

    Official only when the namespace is EXACTLY one of a listed publisher's and
    any repository it names belongs to that publisher. Never raises."""
    ns = str(name or "").partition("/")[0]
    pub = _publisher_for_ns(ns)
    if pub is None:
        return ("unverified", ns or "unknown",
                f"Published under {ns or 'an unknown namespace'}, which is not on "
                "the deck's list of official publishers.")
    owner = _owner_of(repo_url)
    if owner is not None and owner not in pub.owners:
        return ("unverified", ns,
                f"Claims the {pub.name} namespace ({ns}) but its source is "
                f"{_short_repo(repo_url)}, not a {pub.name} account.")
    where = f", source {_short_repo(repo_url)}" if owner else ""
    return "official", pub.name, f"Published by {pub.name} ({ns}){where}."


# ── small pure helpers ───────────────────────────────────────────────────────


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _env_name(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_").upper()
    if not out or out[0].isdigit():
        out = f"K_{out}"
    return out


def _server_slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9_-]+", "-", str(text or "").lower()).strip("-")
    return out or "connector"


def frontmatter(text: str) -> dict:
    """The `key: value` lines between the first two `---` of a SKILL.md."""
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def public(item: dict) -> dict:
    """The wire shape: every key the contract names, nothing internal."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ── the network edges (replaced in tests) ────────────────────────────────────

Fetch = Callable[..., "tuple[int, bytes]"]


def http_fetch(url: str, headers: dict | None = None,
               timeout: float = 20.0) -> tuple[int, bytes]:
    """GET `url`. `(status, body)`; an HTTP error is a status, not a raise."""
    hdrs = {"User-Agent": "agent-deck-store", "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN") or ""
    if token and url.startswith(GITHUB_API):
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def http_post(url: str, data: bytes = b"", headers: dict | None = None,
              timeout: float = 20.0) -> tuple[int, dict, bytes]:
    """POST. `(status, headers, body)`; an HTTP error is a status, not a raise."""
    hdrs = {"User-Agent": "agent-deck-store"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def probe_auth(url: str, timeout: float = 8.0) -> str:
    """Does a remote MCP server answer `initialize` without credentials?

    `none` on 2xx, `oauth` on 401/403, `unknown` on anything else."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "agent-deck-store", "version": "1"}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "agent-deck-store"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return "none" if 200 <= resp.status < 300 else "unknown"
    except urllib.error.HTTPError as exc:
        return "oauth" if exc.code in (401, 403) else "unknown"
    except Exception:  # noqa: BLE001 - a probe never takes the refresh down
        return "unknown"


# ── parsing the sources ──────────────────────────────────────────────────────


def _field(name: str, description: str, required: bool) -> dict:
    return {"name": name, "label": name, "description": str(description or ""),
            "required": bool(required)}


def _recipes(server: dict, probes: dict[str, str], which) -> list[dict]:
    """Every way the deck could run this server, best first. Internal shape:
    {"rank", "auth", "fields", "recipe"} or {"rank", "why"} for a dead end."""
    out: list[dict] = []
    for remote in server.get("remotes") or []:
        kind = remote.get("type")
        url = str(remote.get("url") or "")
        if kind not in ("streamable-http", "sse") or not url.startswith("https://"):
            continue
        if "{" in url:
            out.append({"rank": 9, "why": "Its address needs values the deck can't fill in yet."})
            continue
        wire = "http" if kind == "streamable-http" else "sse"
        secret = [h for h in remote.get("headers") or []
                  if h.get("isSecret") or h.get("isRequired")]
        if secret:
            fields = [_field(_env_name(h.get("name")), h.get("description"),
                             h.get("isRequired", True) is not False) for h in secret]
            headers = {str(h.get("name")): {"field": _env_name(h.get("name")),
                                             "template": h.get("value") or None}
                       for h in secret}
            out.append({"rank": 2, "auth": "api_key", "fields": fields,
                        "recipe": {"type": wire, "url": url, "headers": headers}})
            continue
        auth = probes.get(url, "unknown")
        rank = {"none": 1, "unknown": 4, "oauth": 8}.get(auth, 4)
        out.append({"rank": rank, "auth": auth, "fields": [],
                    "recipe": {"type": wire, "url": url}})
    for pkg in server.get("packages") or []:
        reg = pkg.get("registryType")
        ident = str(pkg.get("identifier") or "")
        version = str(pkg.get("version") or server.get("version") or "")
        if (pkg.get("transport") or {}).get("type", "stdio") != "stdio" or not ident:
            continue
        if reg == "npm":
            command, args = "npx", ["-y", f"{ident}@{version}" if version else ident]
        elif reg == "pypi" and which("uvx"):
            command, args = "uvx", [f"{ident}=={version}" if version else ident]
        else:
            out.append({"rank": 9, "why": f"Ships only as a {reg} package, which the "
                        "deck can't run yet."})
            continue
        env = [e for e in pkg.get("environmentVariables") or []
               if e.get("isSecret") or e.get("isRequired")]
        fields = [_field(str(e.get("name")), e.get("description"), bool(e.get("isRequired")
                         or e.get("isSecret"))) for e in env if e.get("name")]
        out.append({"rank": 3, "auth": "api_key" if fields else "none", "fields": fields,
                    "recipe": {"type": "stdio", "command": command, "args": args,
                               "env": {f["name"]: f["name"] for f in fields}}})
    out.sort(key=lambda r: r["rank"])
    return out


def registry_item(entry: dict, probes: dict[str, str] | None = None,
                  which=shutil.which) -> dict | None:
    """One `/v0/servers` entry as a store item (with `_recipe`), or None."""
    server = entry.get("server") if isinstance(entry, dict) else None
    if not isinstance(server, dict) or not server.get("name"):
        return None
    meta = ((entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official")
            or {})
    if meta.get("status") not in (None, "active"):
        return None
    name = str(server["name"])
    repo = (server.get("repository") or {}).get("url") or None
    trust, publisher, note = classify_registry(name, repo)
    options = _recipes(server, probes or {}, which)
    best = next((o for o in options if "recipe" in o), None)
    item = {
        "id": f"mcp:{name}", "kind": "connector",
        "name": name, "title": server.get("title") or name.partition("/")[2] or name,
        "description": str(server.get("description") or ""),
        "publisher": publisher, "source": "mcp-registry",
        "repo": repo, "stars": None, "updated_at": meta.get("updatedAt"),
        "trust": trust, "trust_note": note,
        "version": server.get("version"),
        "auth": "unknown", "secrets": [], "installable": False, "why_not": None,
        "installed_on": [],
    }
    if best is None:
        dead = next((o for o in options if "why" in o), None)
        item["why_not"] = (dead or {}).get("why") or "It publishes no way to run it."
        return item
    item["auth"] = best["auth"]
    item["secrets"] = best["fields"]
    item["_recipe"] = best["recipe"]
    # An OAuth server installs through Connect (`Store.connect`).
    item["installable"] = True
    return item


def skill_items(src: dict, marketplace: dict, tree: dict, skill_md: dict[str, str],
                repo_meta: dict, commit: str) -> list[dict]:
    """anthropics/skills' marketplace + its tree at `commit` -> one item per
    skill folder that has a SKILL.md at that commit."""
    blobs = {e["path"]: e for e in (tree.get("tree") or [])
             if isinstance(e, dict) and e.get("type") == "blob"}
    repo = f"https://github.com/{src['owner']}/{src['repo']}"
    seen: dict[str, dict] = {}
    for plugin in marketplace.get("plugins") or []:
        for raw in plugin.get("skills") or []:
            path = str(raw).removeprefix("./").rstrip("/")
            if f"{path}/SKILL.md" not in blobs or path in seen:
                continue
            fm = frontmatter(skill_md.get(path, ""))
            folder = path.rsplit("/", 1)[-1]
            files = [{"path": p, "mode": e.get("mode", "100644"), "size": e.get("size", 0)}
                     for p, e in blobs.items() if p.startswith(f"{path}/")]
            seen[path] = {
                "id": f"skill:{src['owner']}/{src['repo']}:{folder}", "kind": "skill",
                "name": folder, "title": fm.get("name") or folder,
                "description": fm.get("description", ""),
                "publisher": src["publisher"], "source": src["source"],
                "repo": repo, "stars": repo_meta.get("stargazers_count"),
                "updated_at": repo_meta.get("pushed_at"),
                "trust": "official",
                "trust_note": f"Published by {src['publisher']} in "
                              f"github.com/{src['owner']}/{src['repo']} "
                              f"(bundle: {plugin.get('name')}).",
                "version": commit[:7], "auth": "none", "secrets": [],
                "installable": True, "why_not": None, "installed_on": [],
                "_commit": commit, "_path": path, "_files": files,
                "_owner": src["owner"], "_repo": src["repo"],
            }
    return list(seen.values())


def github_items(search: dict, known_repos: set[str]) -> list[dict]:
    """Official orgs' MCP repos that the registry does not carry: listed so the
    owner can see them, never installable (there is no recipe to run)."""
    out = []
    for repo in search.get("items") or []:
        url = str(repo.get("html_url") or "")
        if not url or url.lower().rstrip("/") in known_repos or repo.get("archived"):
            continue
        pub = _publisher_for_owner(_owner_of(url))
        if pub is None:
            continue
        out.append({
            "id": f"gh:{repo.get('full_name')}", "kind": "connector",
            "name": str(repo.get("name") or ""), "title": str(repo.get("name") or ""),
            "description": str(repo.get("description") or ""),
            "publisher": pub.name, "source": "github", "repo": url,
            "stars": repo.get("stargazers_count"), "updated_at": repo.get("pushed_at"),
            "trust": "official",
            "trust_note": f"A repository of {pub.name}'s own GitHub account.",
            "version": None, "auth": "unknown", "secrets": [], "installable": False,
            "why_not": "Not published to the MCP registry, so the deck has no way "
                       "to run it yet. Open the repo to see how.",
            "installed_on": [],
        })
    return out


def plugin_items(market: dict, repo_meta: dict, commit: str) -> list[dict]:
    """anthropics/claude-plugins-official's marketplace.json -> store items.

    `official` only for Anthropic's own plugins (author Anthropic, kept in the
    directory's `./plugins/`, or in an `anthropics` repo). Every other entry is
    `verified`: listed in Anthropic's curated directory -- which is all the
    badge claims."""
    src = PLUGIN_SOURCE
    directory = f"https://github.com/{src['owner']}/{src['repo']}"
    out = []
    for plugin in market.get("plugins") or []:
        name = str(plugin.get("name") or "")
        if not name:
            continue
        author = str((plugin.get("author") or {}).get("name") or "")
        source = plugin.get("source")
        installable, why_not, version, stars = True, None, commit[:7], None
        if isinstance(source, str):
            path = source.removeprefix("./").rstrip("/")
            repo, stars = directory, repo_meta.get("stargazers_count")
            mine = author == "Anthropic" and path.startswith("plugins/")
            publisher = author or path.rsplit("/", 1)[-1]
        elif isinstance(source, dict) and source.get("source") in ("git-subdir", "url", "github"):
            url = str(source.get("url") or "")
            if source.get("source") == "github" and source.get("repo"):
                url = f"https://github.com/{source['repo']}"
            repo = url.removesuffix(".git")
            owner = (_owner_of(repo) or "/").split("/", 1)[1]
            mine = author == "Anthropic" and owner == "anthropics"
            publisher = author or (urllib.parse.urlparse(repo).path.strip("/").split("/")
                                   or ["unknown"])[0] or "unknown"
            if source.get("sha"):
                version = str(source["sha"])[:7]
        else:
            repo, mine, publisher = directory, False, author or "unknown"
            installable = False
            why_not = "It installs by running a command, which the deck won't do unattended."
        trust = "official" if mine else "verified"
        note = (f"Published by Anthropic in {src['owner']}/{src['repo']}." if mine else
                f"Listed in Anthropic's plugin directory ({src['owner']}/{src['repo']}); "
                f"made by {publisher}. Plugins can bundle hooks and MCP servers that "
                "run on your server.")
        out.append({
            "id": f"plugin:{name}@{src['market']}", "kind": "plugin",
            "name": name, "title": plugin.get("displayName") or name,
            "description": str(plugin.get("description") or ""),
            "publisher": publisher, "source": src["market"], "repo": repo,
            "stars": stars, "updated_at": repo_meta.get("pushed_at"),
            "trust": trust, "trust_note": note, "version": version,
            "auth": "none", "secrets": [], "installable": installable,
            "why_not": why_not, "installed_on": [],
            "_plugin": f"{name}@{src['market']}",
        })
    return out


# ── OAuth tokens: the only code that reads or rotates them ──────────────────


def _oauth_doc(store_dir: Path) -> dict:
    try:
        doc = json.loads((Path(store_dir) / "oauth.json").read_text())
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc.get("connectors"), dict):
        doc["connectors"] = {}
    return doc


def _save_oauth_doc(store_dir: Path, doc: dict) -> None:
    atomic.write_text(Path(store_dir) / "oauth.json", json.dumps(doc, indent=1), mode=0o600)


@contextmanager
def _oauth_lock(store_dir: Path):
    Path(store_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(store_dir) / ".oauth.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _store_token(vault_path: Path, name: str, value: str, label: str,
                 grants) -> None:
    current = {m.name: m for m in vault.meta(vault_path)}
    keep = set(current[name].grants) if name in current else set()
    vault.put(vault_path, name=name, value=value,
              description=f"{label} (Connectors & Skills, OAuth)",
              grants=sorted(keep | set(grants)))


def _refresh_locked(item_id: str, meta: dict, *, store_dir: Path, vault_path: Path,
                    reader: str, post, now: float) -> None:
    """Trade the refresh token for new ones. Caller holds the oauth lock."""
    values = vault.env_for(vault_path, reader)
    refresh_token = values.get(meta["refresh"])
    if not refresh_token:
        raise mcp_oauth.OAuthError("reconnect", "No refresh token; press Connect again.")
    secret = values.get(meta["client_secret"]) if meta.get("client_secret") else None
    try:
        got = mcp_oauth.refresh(meta["token_endpoint"], meta["resource"],
                                meta["client_id"], secret, refresh_token, post=post)
    except mcp_oauth.OAuthError as exc:
        raise mcp_oauth.OAuthError("reconnect", f"{exc.detail} Press Connect again.") from None
    _store_token(vault_path, meta["access"], got["access_token"], meta["title"], ())
    if got.get("refresh_token"):
        _store_token(vault_path, meta["refresh"], got["refresh_token"], meta["title"], ())
    meta["expires_at"] = now + float(got.get("expires_in") or 3600)
    meta.pop("broken", None)


def fresh_access(item_id: str, desk: str, *, store_dir: Path, vault_path: Path,
                 post=None, now: float | None = None) -> str:
    """The access token for `desk`, refreshed first if it is about to expire.
    Raises OAuthError. The ONLY function that returns one."""
    now = time.time() if now is None else float(now)
    with _oauth_lock(store_dir):
        doc = _oauth_doc(store_dir)
        meta = doc["connectors"].get(item_id)
        if not meta:
            raise mcp_oauth.OAuthError("reconnect", "Not connected; press Connect.")
        if float(meta.get("expires_at") or 0) - now < REFRESH_MARGIN:
            _refresh_locked(item_id, meta, store_dir=store_dir, vault_path=vault_path,
                            reader=desk, post=post or http_post, now=now)
            _save_oauth_doc(store_dir, doc)
        token = vault.env_for(vault_path, desk).get(meta["access"])
    if not token:
        raise mcp_oauth.OAuthError("not_granted", f"{desk} was not given this connector.")
    return token


# ── reload: making an install take effect ────────────────────────────────────

BUSY_STATES = frozenset({"WORKING", "NEEDS_YOU"})


def _inline(flags) -> bool:
    flags = list(flags or [])
    for i, flag in enumerate(flags[:-1]):
        if flag == "--mcp-config":
            return str(flags[i + 1]).lstrip().startswith("{")
    return False


def plan_reload(engine: str, *, live: bool, flags) -> str:
    """What a reload will do to a desk. PURE. See docs/connectors.md."""
    if engine != "claude":
        return "none"
    if live:
        return "restart_after_turn" if _inline(flags) else "after_turn"
    return "restarting" if _inline(flags) else "next_wake"


DETAIL = {
    "after_turn": "{d} reloads its tools when its current turn ends (same conversation).",
    "restart_after_turn": "{d} restarts once its turn ends to pick this up "
                          "(it was started before tools could reload; its "
                          "conversation is replayed).",
    "restarting": "{d} is asleep on an older setup; it is being restarted to pick this up.",
    "next_wake": "{d} is asleep; it picks this up when it next wakes.",
    "none": "{d} doesn't run Claude Code, so there is nothing to reload.",
}


def _job_state(session_id: str) -> dict | None:
    from . import spawn
    try:
        entries = list(Path(spawn.JOBS_DIR).iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            state = json.loads((entry / "state.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and state.get("sessionId") == session_id:
            state.setdefault("daemonShort", entry.name)
            return state
    return None


def _live_job(name: str):
    from . import office, wake
    sids = sorted(office.live_session_ids(name) or ())
    if not sids:
        job = wake.last_job(name)
        if job is None or not wake.running(job.session_id):
            return None
        sids = [job.session_id]
    for sid in sids:
        state = _job_state(sid)
        if state is not None:
            return (sid, str(state.get("daemonShort")), str(state.get("cwd") or ""),
                    list(state.get("respawnFlags") or []))
    return None


def _last_flags(name: str):
    from . import wake
    job = wake.last_job(name)
    state = _job_state(job.session_id) if job else None
    return list(state.get("respawnFlags") or []) if state else None


def _desk_state(name: str) -> str:
    from . import office
    try:
        board = json.loads(office.OFFICE_FILE.read_text())
    except (OSError, ValueError):
        return "IDLE"
    best = None
    for entry in ((board or {}).get("sessions") or {}).values():
        if isinstance(entry, dict) and entry.get("name") == name:
            if best is None or float(entry.get("started_at") or 0) >= float(
                    best.get("started_at") or 0):
                best = entry
    return str((best or {}).get("state") or "IDLE")


def _desk_of(name: str):
    try:
        return next((d for d in roster.load_roster(roster.DEFAULT_PATH)
                     if d.name == name), None)
    except Exception:  # noqa: BLE001
        return None


def _stop(short: str) -> bool:
    from . import spawn
    return spawn.stop_job(short)


def _resume(sid: str, cwd: str, seed: str) -> str:
    from . import spawn
    return spawn.resume_background(sid, cwd=cwd, seed=seed)


def _running(session_id: str) -> bool:
    from . import wake
    return wake.running(session_id)


def _restart(desk) -> dict:
    from . import wake
    return wake.restart_headless(desk)


class Reloader:
    """Stop and resume a desk as itself once it is idle (or restart one on an
    inline config). Every edge injected; `run` is the whole procedure."""

    def __init__(self, *, desk_of=_desk_of, live_job=_live_job, last_flags=_last_flags,
                 state=_desk_state, stop=_stop, resume=_resume, restart=_restart,
                 sleep=time.sleep, running=_running, wait_max: float = 1800.0,
                 poll: float = 5.0, exit_max: float = 60.0) -> None:
        self.desk_of, self.live_job, self.last_flags = desk_of, live_job, last_flags
        self.state, self.stop, self.resume, self.restart = state, stop, resume, restart
        self.sleep, self.wait_max, self.poll = sleep, wait_max, poll
        self.running, self.exit_max = running, exit_max

    def run(self, name: str, note: str) -> dict:
        desk = self.desk_of(name)
        if desk is None or desk.engine != "claude":
            return {"desk": name, "action": "none"}
        job = self.live_job(name)
        if job is None:
            if plan_reload(desk.engine, live=False, flags=self.last_flags(name)) == "restarting":
                self.restart(desk)
                return {"desk": name, "action": "restarted"}
            return {"desk": name, "action": "next_wake"}
        waited = 0.0
        while self.state(name) in BUSY_STATES:
            if waited >= self.wait_max:
                return {"desk": name, "action": "gave_up"}
            self.sleep(self.poll)
            waited += self.poll
        sid, short, cwd, flags = job
        if plan_reload(desk.engine, live=True, flags=flags) == "restart_after_turn":
            self.restart(desk)
            return {"desk": name, "action": "restarted"}
        self.stop(short)
        # MEASURED: `--resume` while the stopped process is still exiting
        # "starts a copy" -- new id, auto name, default permissions -- that the
        # board cannot see. Wait until it is really gone; if it never goes,
        # leave it stopped: its next wake resumes it with the new config.
        waited = 0.0
        while self.running(sid):
            if waited >= self.exit_max:
                return {"desk": name, "action": "stopped"}
            self.sleep(1.0)
            waited += 1.0
        self.resume(sid, cwd or desk.cwd, note)
        return {"desk": name, "action": "resumed"}


def schedule_reload(name: str, note: str) -> dict:
    """The default reloader: say now what will happen, do it in a DETACHED
    process. Detached because a desk that installs onto itself calls this from
    its own MCP subprocess, which the stop is about to end."""
    desk = _desk_of(name)
    if desk is None or desk.engine != "claude":
        action = "none"
    else:
        job = _live_job(name)
        action = plan_reload(desk.engine, live=job is not None,
                             flags=job[3] if job else _last_flags(name))
    if action in ("after_turn", "restart_after_turn", "restarting"):
        RELOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RELOAD_LOG, "a") as log:
            subprocess.Popen(  # noqa: S603 - our own module, fixed argv
                [sys.executable, "-m", "server.connectors", "reload", "--desk", name,
                 "--note", note], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True,
                env={**os.environ, "PYTHONPATH": ROOT})
    return {"desk": name, "action": action, "detail": DETAIL[action].format(d=name)}


# ── the store ────────────────────────────────────────────────────────────────


class Store:
    def __init__(self, *, fetch: Fetch = http_fetch, probe=probe_auth,
                 roster_path: Path | None = None, root: Path | None = None,
                 vault_path: Path | None = None, reloader=schedule_reload,
                 clock=time.time, background: bool = True, which=shutil.which,
                 post=http_post, runner=None) -> None:
        self.fetch, self.probe, self.reloader, self.clock = fetch, probe, reloader, clock
        self.post = post
        if runner is None:
            from .skills import cli_runner as runner
        self.runner = runner
        self.roster_path = Path(roster_path or roster.DEFAULT_PATH)
        self.root = Path(root or STORE_DIR)
        self.vault_path = Path(vault_path or vault.DEFAULT_PATH)
        self.background, self.which = background, which
        self._lock = threading.RLock()
        self._refreshing = False
        self._stale = False
        self._extra: dict[str, dict] = {}   # unverified items met by live search

    # ── paths and small stores ──

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.json"

    @property
    def ledger_path(self) -> Path:
        return self.root / "installed.json"

    @property
    def requests_path(self) -> Path:
        return self.root / "requests.json"

    @contextmanager
    def _flock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _ledger(self) -> dict:
        data = self._read(self.ledger_path)
        data.setdefault("version", 1)
        if not isinstance(data.get("desks"), dict):
            data["desks"] = {}
        return data

    def _desks_all(self) -> list:
        return [d for d in roster.load_roster(self.roster_path) if d.engine == "claude"]

    def _targets(self, desks) -> list:
        everyone = roster.load_roster(self.roster_path)
        if desks == "all":
            return [d for d in everyone if d.engine == "claude"]
        if not isinstance(desks, list) or not desks or not all(
                isinstance(d, str) for d in desks):
            raise StoreError("bad_input", 'desks is "all" or a list of desk names.')
        by_name = {d.name: d for d in everyone}
        out = []
        for name in dict.fromkeys(desks):
            desk = by_name.get(name)
            if desk is None:
                raise StoreError("unknown_desk", f"No desk named {name!r}.")
            if desk.engine != "claude":
                raise StoreError("bad_input", f"{name} doesn't run Claude Code.")
            out.append(desk)
        return out

    # ── the catalog ──

    def _get(self, url: str) -> dict:
        status, body = self.fetch(url)
        if status != 200:
            raise StoreError("fetch_failed", f"{url} answered {status}.")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise StoreError("fetch_failed", f"{url} did not answer a JSON object.")
        return value

    def _text(self, url: str) -> str:
        status, body = self.fetch(url)
        if status != 200:
            raise StoreError("fetch_failed", f"{url} answered {status}.")
        return body.decode("utf-8", "replace")

    def _head(self, owner: str, repo: str, branch: str) -> str:
        """The branch's commit from git's ref advertisement -- not the REST API,
        whose anonymous budget (60 an hour) MEASURED ran out on the box."""
        body = self._text(f"https://github.com/{owner}/{repo}.git/info/refs"
                          "?service=git-upload-pack")
        found = re.search(r"([0-9a-f]{40}) refs/heads/" + re.escape(branch) + r"\b", body)
        if not found:
            raise StoreError("fetch_failed", f"{owner}/{repo} has no branch {branch}.")
        return found.group(1)

    def _repo_meta(self, base: str) -> dict:
        """Stars and last push. A nicety: missing when the API says no."""
        try:
            status, body = self.fetch(base)
            value = json.loads(body) if status == 200 else {}
        except Exception:  # noqa: BLE001
            value = {}
        return value if isinstance(value, dict) else {}

    def _refresh_skills(self) -> list[dict]:
        items: list[dict] = []
        for src in SKILL_SOURCES:
            base = f"{GITHUB_API}/repos/{src['owner']}/{src['repo']}"
            meta = self._repo_meta(base)
            commit = self._head(src["owner"], src["repo"], src["branch"])
            tree = self._get(f"{base}/git/trees/{commit}?recursive=1")
            raw = f"{RAW}/{src['owner']}/{src['repo']}/{commit}"
            market = json.loads(self._text(f"{raw}/.claude-plugin/marketplace.json"))
            blobs = {e.get("path") for e in tree.get("tree") or []}
            paths = {str(s).removeprefix("./").rstrip("/")
                     for p in market.get("plugins") or [] for s in p.get("skills") or []}
            wanted = sorted(p for p in paths if f"{p}/SKILL.md" in blobs)
            with ThreadPoolExecutor(max_workers=8) as pool:
                texts = dict(zip(wanted, pool.map(
                    lambda p: self._text(f"{raw}/{p}/SKILL.md"), wanted)))
            items += skill_items(src, market, tree, texts, meta, commit)
        return items

    def _refresh_plugins(self) -> list[dict]:
        src = PLUGIN_SOURCE
        base = f"{GITHUB_API}/repos/{src['owner']}/{src['repo']}"
        meta = self._repo_meta(base)
        commit = self._head(src["owner"], src["repo"], src["branch"])
        market = json.loads(self._text(
            f"{RAW}/{src['owner']}/{src['repo']}/{commit}/.claude-plugin/marketplace.json"))
        return plugin_items(market, meta, commit)

    def _registry_search(self, search: str, limit: int = 100) -> list[dict]:
        query = urllib.parse.urlencode({"search": search, "version": "latest",
                                        "limit": str(limit)})
        return list(self._get(f"{REGISTRY}?{query}").get("servers") or [])

    def _refresh_registry(self) -> list[dict]:
        entries: dict[str, dict] = {}
        spaces = sorted({ns for p in PUBLISHERS for ns in p.namespaces})
        with ThreadPoolExecutor(max_workers=8) as pool:
            pages = list(pool.map(self._registry_search, spaces))
        for ns, page in zip(spaces, pages):
            for entry in page:
                name = str((entry.get("server") or {}).get("name") or "")
                if name.partition("/")[0].lower() == ns:
                    entries[name] = entry
        # Probe every trusted remote that asks for no key: none vs OAuth.
        urls = set()
        for entry in entries.values():
            server = entry["server"]
            if classify_registry(server["name"],
                                 (server.get("repository") or {}).get("url"))[0] != "official":
                continue
            for remote in server.get("remotes") or []:
                url = str(remote.get("url") or "")
                if url.startswith("https://") and "{" not in url and not remote.get("headers"):
                    urls.add(url)
        with ThreadPoolExecutor(max_workers=8) as pool:
            probes = dict(zip(sorted(urls), pool.map(self.probe, sorted(urls))))
        items = [i for i in (registry_item(e, probes, self.which) for e in entries.values())
                 if i is not None and i["trust"] != "unverified"]
        self._add_repo_meta(items)
        return items

    def _add_repo_meta(self, items: list[dict]) -> None:
        repos = sorted({_short_repo(i["repo"]) for i in items
                        if i.get("repo") and _owner_of(i["repo"]) and
                        _owner_of(i["repo"]).startswith("github.com/")})

        def meta(short: str):
            owner_repo = short.split("/", 1)[1]
            try:
                status, body = self.fetch(f"{GITHUB_API}/repos/{owner_repo}")
                return json.loads(body) if status == 200 else None
            except Exception:  # noqa: BLE001 - stars are a nicety
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            found = dict(zip(repos, pool.map(meta, repos)))
        for item in items:
            got = found.get(_short_repo(item.get("repo"))) if item.get("repo") else None
            if isinstance(got, dict):
                item["stars"] = got.get("stargazers_count")
                item["updated_at"] = got.get("pushed_at") or item.get("updated_at")

    def _refresh_github(self, known: set[str]) -> list[dict]:
        orgs = sorted({o.split("/", 1)[1] for p in PUBLISHERS for o in p.owners
                       if o.startswith("github.com/")})
        chunks, cur = [], "topic:mcp-server"
        for org in orgs:
            nxt = f"{cur} org:{org}"
            if len(nxt) > 240:
                chunks.append(cur)
                cur = f"topic:mcp-server org:{org}"
            else:
                cur = nxt
        chunks.append(cur)
        out: list[dict] = []
        for q in chunks:
            url = f"{GITHUB_API}/search/repositories?" + urllib.parse.urlencode(
                {"q": q, "per_page": "50", "sort": "stars"})
            out += github_items(self._get(url), known)
        return out

    def refresh(self, force: bool = False) -> dict:
        """Rebuild the trusted catalog. A failing source keeps its last good
        items and marks the catalog stale."""
        old = self._read(self.catalog_path)
        old_items = old.get("items") or []
        stale = False
        by_source: dict[str, list[dict]] = {}
        for source, build in (("anthropic-skills", self._refresh_skills),
                              (PLUGIN_SOURCE["market"], self._refresh_plugins),
                              ("mcp-registry", self._refresh_registry)):
            try:
                by_source[source] = build()
            except Exception:  # noqa: BLE001 - one source down is not all down
                stale = True
                by_source[source] = [i for i in old_items if i.get("source") == source]
        known = {str(i["repo"]).lower().rstrip("/").removesuffix(".git")
                 for i in by_source["mcp-registry"] if i.get("repo")}
        try:
            by_source["github"] = self._refresh_github(known)
        except Exception:  # noqa: BLE001
            stale = True
            by_source["github"] = [i for i in old_items if i.get("source") == "github"]
        items = [i for group in by_source.values() for i in group]
        if stale and not old_items and not items:
            self._stale = True
            raise StoreError("fetch_failed", "No catalog source answered.")
        at = self.clock()
        doc = {"version": 1, "refreshed_at": at if not stale else old.get("refreshed_at", at),
               "checked_at": at, "items": items}
        atomic.write_text(self.catalog_path, json.dumps(doc))
        self._stale = stale
        return doc

    def _refresh_quietly(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            self._stale = True
        finally:
            self._refreshing = False

    def _catalog_doc(self) -> dict:
        doc = self._read(self.catalog_path)
        checked = float(doc.get("checked_at") or doc.get("refreshed_at") or 0)
        due = not doc.get("items") or self.clock() - checked >= REFRESH_EVERY
        if not due:
            return doc
        if doc.get("items") and self.background:
            with self._lock:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh_quietly, daemon=True,
                                     name="store-refresh").start()
            return doc
        try:
            return self.refresh()
        except Exception:  # noqa: BLE001 - serve what we have
            self._stale = True
            if doc.get("items"):
                return doc
            raise StoreError("fetch_failed", "The catalog could not be loaded.")

    def refresh_async(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_quietly, daemon=True,
                         name="store-refresh").start()

    # ── what is installed where ──

    def _skill_markers(self) -> dict[str, dict[str, dict]]:
        """{desk: {item id: marker}} read from each desk's folder."""
        out: dict[str, dict[str, dict]] = {}
        for desk in self._desks_all():
            found: dict[str, dict] = {}
            for marker in sorted((Path(desk.cwd) / ".claude" / "skills").glob(
                    f"*/{SKILL_MARKER}")):
                data = self._read(marker)
                if data.get("id"):
                    found[str(data["id"])] = {**data, "_folder": str(marker.parent)}
            out[desk.name] = found
        return out

    def _installed_on(self, items: list[dict]) -> None:
        ledger = self._ledger()["desks"]
        markers = self._skill_markers()
        plugins = self._ledger().get("plugins") or {}
        cwd_of = {d.name: os.path.normpath(d.cwd) for d in self._desks_all()}
        for item in items:
            on = [d for d, found in markers.items() if item["id"] in found]
            on += [d for d, rows in ledger.items() if item["id"] in (rows or {})]
            on += [d for d, cwd in cwd_of.items() if item["id"] in (plugins.get(cwd) or {})]
            item["installed_on"] = sorted(dict.fromkeys(on), key=on.index)

    def _all_items(self) -> list[dict]:
        return list(self._catalog_doc().get("items") or [])

    def _lookup(self, item_id: str) -> dict:
        for item in self._all_items():
            if item["id"] == item_id:
                return dict(item)
        if item_id in self._extra:
            return dict(self._extra[item_id])
        if item_id.startswith("mcp:"):
            name = item_id[4:]
            try:
                page = self._registry_search(name, limit=20)
            except Exception:  # noqa: BLE001
                page = []
            for entry in page:
                if (entry.get("server") or {}).get("name") == name:
                    item = registry_item(entry, {}, self.which)
                    if item is not None:
                        self._extra[item_id] = item
                        return dict(item)
        raise StoreError("unknown_item", f"Nothing in the store is called {item_id!r}.")

    # ── reading ──

    def catalog(self, kind: str | None = None, q: str = "", trust: str = "trusted",
                limit: int = 50, offset: int = 0) -> dict:
        if kind not in (None, "", "connector", "skill", "plugin"):
            raise StoreError("bad_input", "kind is connector, skill or plugin.")
        if trust not in ("trusted", "all"):
            raise StoreError("bad_input", "trust is trusted or all.")
        doc = self._catalog_doc()
        items = [dict(i) for i in doc.get("items") or []]
        if trust == "all" and q.strip():
            seen = {i["id"] for i in items}
            try:
                for entry in self._registry_search(q.strip(), limit=50):
                    item = registry_item(entry, {}, self.which)
                    if item is not None and item["id"] not in seen:
                        self._extra[item["id"]] = item
                        items.append(dict(item))
                        seen.add(item["id"])
            except Exception:  # noqa: BLE001 - the trusted list still answers
                pass
        if kind:
            items = [i for i in items if i["kind"] == kind]
        words = [w for w in q.lower().split() if w]
        if words:
            def hay(i):
                return " ".join(str(i.get(k) or "") for k in
                                ("name", "title", "description", "publisher")).lower()
            items = [i for i in items if all(w in hay(i) for w in words)]
        rank = {"official": 0, "verified": 1, "unverified": 2}
        items.sort(key=lambda i: (not i.get("installable"), rank.get(i["trust"], 3),
                                  -(i.get("stars") or 0),
                                  str(i.get("title") or "").lower()))
        total = len(items)
        page = items[offset:offset + limit]
        self._installed_on(page)
        return {"items": [public(i) for i in page], "total": total,
                "refreshed_at": _iso(doc.get("refreshed_at")), "stale": self._stale}

    def item(self, item_id: str) -> dict:
        item = self._lookup(str(item_id or ""))
        self._installed_on([item])
        return public(item)

    def installed(self, desk: str | None = None) -> dict:
        desks = self._desks_all()
        if desk is not None:
            desks = [d for d in desks if d.name == desk]
            if not desks:
                raise StoreError("unknown_desk", f"No Claude desk named {desk!r}.")
        by_id = {i["id"]: i for i in self._all_items()}
        ledger = self._ledger()["desks"]
        markers = self._skill_markers()
        out = []
        for d in desks:
            rows = []
            for item_id, marker in sorted(markers.get(d.name, {}).items()):
                cat = by_id.get(item_id) or {}
                commit = str(marker.get("commit") or "")
                rows.append({"id": item_id, "kind": "skill",
                             "name": marker.get("name") or item_id.rsplit(":", 1)[-1],
                             "title": cat.get("title") or marker.get("name") or item_id,
                             "desk": d.name, "version": commit[:7] or None,
                             "commit": commit or None,
                             "installed_at": marker.get("installed_at"),
                             "trust": marker.get("trust") or cat.get("trust") or "unverified",
                             "update_available": bool(cat.get("_commit")) and
                             cat.get("_commit") != commit})
            for item_id, entry in sorted((ledger.get(d.name) or {}).items()):
                cat = by_id.get(item_id) or {}
                rows.append({"id": item_id, "kind": "connector",
                             "name": entry.get("name"), "title": entry.get("title"),
                             "desk": d.name, "version": entry.get("version"),
                             "commit": None, "installed_at": entry.get("installed_at"),
                             "trust": entry.get("trust") or "unverified",
                             "update_available": bool(cat.get("version")) and
                             cat.get("version") != entry.get("version")})
            plugins = (self._ledger().get("plugins") or {}).get(os.path.normpath(d.cwd)) or {}
            for item_id, entry in sorted(plugins.items()):
                cat = by_id.get(item_id) or {}
                rows.append({"id": item_id, "kind": "plugin", "name": entry.get("name"),
                             "title": cat.get("title") or entry.get("name"),
                             "desk": d.name, "version": entry.get("version"),
                             "commit": None, "installed_at": entry.get("installed_at"),
                             "trust": entry.get("trust") or "unverified",
                             # The CLI keeps its own pin; Update re-runs install.
                             "update_available": False})
            out.append({"desk": d.name, "items": rows})
        return {"desks": out}

    # ── install ──

    def install(self, item_id: str, desks, secrets: dict | None = None,
                accept_unverified: bool = False, *, replace: bool = False) -> dict:
        item = self._lookup(str(item_id or ""))
        targets = self._targets(desks)
        if secrets is not None and not (isinstance(secrets, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in secrets.items())):
            raise StoreError("bad_input", "secrets maps field names to text.")
        if item["trust"] == "unverified" and not accept_unverified:
            raise StoreError("unverified", f"{item['title']} is from an unverified "
                             "source. Agents run with full access; confirm to install.")
        if not item.get("installable"):
            raise StoreError("not_installable", item.get("why_not") or "Not installable.")
        if item.get("auth") == "oauth" and item["id"] not in _oauth_doc(self.root)["connectors"]:
            raise StoreError("needs_oauth", OAUTH_WHY)
        with self._flock():
            if item["kind"] == "skill":
                rows, notes = self._install_skill(item, targets, replace)
            elif item["kind"] == "plugin":
                rows, notes = self._install_plugin(item, targets)
            else:
                rows, notes = self._install_connector(item, targets, secrets or {})
        reloads = [self.reloader(d.name, notes[d.name]) for d in targets]
        self._installed_on([item])
        return {"ok": True, "item": public(item), "installed": rows, "reload": reloads}

    def update(self, item_id: str, desks) -> dict:
        targets = self._targets(desks)
        present = {d["desk"]: {i["id"] for i in d["items"]}
                   for d in self.installed()["desks"]}
        for d in targets:
            if item_id not in present.get(d.name, set()):
                raise StoreError("not_installed", f"{item_id} is not on {d.name}.")
        item = self._lookup(item_id)
        return self.install(item_id, [d.name for d in targets],
                            accept_unverified=True,
                            replace=True)

    def _install_skill(self, item: dict, targets: list, replace: bool):
        folders = []
        for d in targets:
            dest = Path(d.cwd) / ".claude" / "skills" / item["name"]
            marker = self._read(dest / SKILL_MARKER)
            if dest.exists() and marker.get("id") != item["id"]:
                raise StoreError("conflict", f"{d.name} already has a skill folder "
                                 f"named {item['name']} that the deck didn't install; "
                                 "it was left alone.")
            folders.append((d, dest))
        files = self._download_skill(item)
        at = _iso(self.clock())
        rows, notes, done = [], {}, set()
        for d, dest in folders:
            if str(dest) not in done:
                self._place(dest, files, {"id": item["id"], "name": item["name"],
                                          "commit": item["_commit"], "installed_at": at,
                                          "trust": item["trust"], "repo": item["repo"],
                                          "path": item["_path"]})
                done.add(str(dest))
            rows.append({"desk": d.name, "id": item["id"], "version": item["version"],
                         "commit": item["_commit"], "installed_at": at})
            notes[d.name] = (f"[Agent Deck] Your tools changed: the skill "
                             f"'{item['name']}' was installed in .claude/skills/"
                             f"{item['name']}. Nothing to do; use it when it fits.")
        return rows, notes

    def _download_skill(self, item: dict) -> dict[str, tuple[bytes, bool]]:
        files = item.get("_files") or []
        if len(files) > MAX_SKILL_FILES or sum(int(f.get("size") or 0)
                                               for f in files) > MAX_SKILL_BYTES:
            raise StoreError("not_installable", "That skill is too large to install.")
        base = f"{RAW}/{item['_owner']}/{item['_repo']}/{item['_commit']}"
        prefix = item["_path"] + "/"
        out: dict[str, tuple[bytes, bool]] = {}
        for f in files:
            rel = f["path"][len(prefix):]
            parts = Path(rel).parts
            if not rel or rel.startswith("/") or ".." in parts:
                raise StoreError("not_installable", "That skill has an unsafe file path.")
            status, body = self.fetch(f"{base}/{urllib.parse.quote(f['path'])}")
            if status != 200:
                raise StoreError("fetch_failed", f"Could not download {f['path']} ({status}).")
            out[rel] = (body, str(f.get("mode")) == "100755")
        return out

    @staticmethod
    def _place(dest: Path, files: dict, marker: dict) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.deck-{_secrets.token_hex(4)}"
        try:
            for rel, (data, executable) in files.items():
                path = tmp / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                if executable:
                    path.chmod(0o755)
            (tmp / SKILL_MARKER).write_text(json.dumps(marker, indent=1))
            if dest.exists():
                old = dest.parent / f".{dest.name}.old-{_secrets.token_hex(4)}"
                dest.rename(old)
                tmp.rename(dest)
                shutil.rmtree(old, ignore_errors=True)
            else:
                tmp.rename(dest)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    # ── plugins: the CLI, as measured in docs/skills.md ──

    def _cli(self, args: list, cwd: str | None) -> dict:
        from .skills import OP_TIMEOUT, parse_result, redact
        try:
            res = self.runner(args, cwd, OP_TIMEOUT)
        except FileNotFoundError:
            raise StoreError("install_failed", "Claude Code is not installed on the server.")
        except subprocess.TimeoutExpired:
            raise StoreError("install_failed", "Claude Code took too long.")
        result = parse_result(res.stdout) or {}
        if res.rc != 0 and result.get("failureCode") != "already_in_goal_state":
            said = result.get("message") or (res.stderr or "").strip().splitlines()[-1:] or [""]
            said = said if isinstance(said, str) else said[0]
            raise StoreError("install_failed", redact(str(said))[:300] or "claude plugin failed")
        return result

    def _ensure_market(self) -> None:
        src = PLUGIN_SOURCE
        res = self.runner(["plugin", "marketplace", "list", "--json"], None, 60)
        try:
            names = {m.get("name") for m in json.loads(res.stdout or "[]")}
        except (ValueError, AttributeError):
            names = set()
        if src["market"] in names:
            self._cli(["plugin", "marketplace", "update", src["market"]], None)
        else:
            self._cli(["plugin", "marketplace", "add", f"{src['owner']}/{src['repo']}"], None)

    def _install_plugin(self, item: dict, targets: list):
        pid = item["_plugin"]
        self._ensure_market()
        cwds = list(dict.fromkeys(os.path.normpath(d.cwd) for d in targets))
        for cwd in cwds:
            self._cli(["plugin", "install", pid, "--json", "-s", "local"], cwd)
        versions = {}
        try:
            listed = json.loads(self.runner(["plugin", "list", "--json"], None, 60).stdout)
            for row in listed.get("installed") or []:
                if row.get("id") == pid:
                    versions[os.path.normpath(str(row.get("projectPath") or ""))] = row.get("version")
        except (ValueError, AttributeError, TypeError):
            pass
        at = _iso(self.clock())
        ledger = self._ledger()
        plugins = ledger.setdefault("plugins", {})
        for cwd in cwds:
            plugins.setdefault(cwd, {})[item["id"]] = {
                "name": item["name"], "version": versions.get(cwd) or item.get("version"),
                "installed_at": at, "trust": item["trust"]}
        atomic.write_text(self.ledger_path, json.dumps(ledger, indent=1))
        rows, notes = [], {}
        for d in targets:
            cwd = os.path.normpath(d.cwd)
            rows.append({"desk": d.name, "id": item["id"],
                         "version": versions.get(cwd) or item.get("version"),
                         "commit": None, "installed_at": at})
            notes[d.name] = (f"[Agent Deck] Your tools changed: the plugin '{pid}' was "
                             "installed. Its skills and commands are yours to use.")
        return rows, notes

    # ── OAuth: Connect ──

    @property
    def pending_path(self) -> Path:
        return self.root / "oauth_pending.json"

    def _pending(self) -> dict:
        return self._read(self.pending_path)

    def _save_pending(self, doc: dict) -> None:
        atomic.write_text(self.pending_path, json.dumps(doc, indent=1), mode=0o600)

    def _getraw(self, url: str) -> tuple[int, bytes]:
        try:
            return self.fetch(url)
        except Exception:  # noqa: BLE001
            return 0, b""

    def connect(self, item_id: str, desks) -> dict:
        """Start a sign-in. Returns the provider's consent URL for his browser."""
        item = self._lookup(str(item_id or ""))
        recipe = item.get("_recipe") or {}
        if (item["kind"] != "connector" or item.get("auth") not in ("oauth", "unknown")
                or recipe.get("type") not in ("http", "sse") or recipe.get("headers")):
            raise StoreError("bad_input", f"{item['title']} doesn't sign in with OAuth.")
        if item["trust"] == "unverified":
            raise StoreError("unverified", "Connect is only offered for trusted connectors.")
        names = [d.name for d in self._targets(desks)]
        try:
            found = mcp_oauth.discover(recipe["url"], get=self._getraw, post=self.post)
            client = mcp_oauth.register(found, post=self.post)
        except mcp_oauth.OAuthError as exc:
            raise StoreError(exc.reason, exc.detail) from None
        verifier, challenge = mcp_oauth.pkce()
        state = _secrets.token_urlsafe(32)
        now = self.clock()
        with self._flock():
            doc = {k: v for k, v in self._pending().items()
                   if now - float(v.get("created") or 0) < 24 * 3600}
            doc[state] = {"id": item["id"], "desks": names, "created": now,
                          "status": "pending", "detail": "", "verifier": verifier,
                          "client_id": client["client_id"],
                          "client_secret": client["client_secret"], "found": found}
            self._save_pending(doc)
        return {"ok": True, "authorize_url": mcp_oauth.authorize_url(
                    found, client["client_id"], challenge, state),
                "state": state, "redirect_uri": mcp_oauth.REDIRECT_URI,
                "expires_at": _iso(now + CONNECT_TTL)}

    def _settle_pending(self, state: str, status: str, detail: str = "") -> None:
        with self._flock():
            doc = self._pending()
            if state in doc:
                doc[state].update(status=status, detail=detail, verifier=None,
                                  client_secret=None)
                self._save_pending(doc)

    def complete(self, state: str | None = None, code: str | None = None,
                 callback_url: str | None = None) -> dict:
        """His browser came back with a code: trade it, keep the tokens, install."""
        error = None
        if callback_url:
            parsed = mcp_oauth.parse_callback(callback_url)
            state, code, error = parsed["state"], parsed["code"], parsed["error"]
        state = str(state or "")
        with self._flock():
            row = self._pending().get(state)
            if (not state or row is None or row.get("status") != "pending"
                    or self.clock() - float(row.get("created") or 0) > CONNECT_TTL):
                raise StoreError("bad_state", "That sign-in link is used up or expired; "
                                 "press Connect again.")
        if error or not code:
            self._settle_pending(state, "failed", "declined at the provider")
            raise StoreError("oauth_denied", "The sign-in was declined at the provider.")
        item = self._lookup(row["id"])
        found = row["found"]
        try:
            tokens = mcp_oauth.exchange(found, row["client_id"], row.get("client_secret"),
                                        str(code), row["verifier"], post=self.post)
        except mcp_oauth.OAuthError as exc:
            self._settle_pending(state, "failed", exc.detail)
            raise StoreError(exc.reason, exc.detail) from None
        slug = _env_name(item["name"].partition("/")[2] or item["name"])
        meta = {"title": item["title"], "token_endpoint": found["token_endpoint"],
                "resource": found["resource"], "client_id": row["client_id"],
                "auth_server": found["auth_server"],
                "access": f"CONNECTOR_{slug}_ACCESS_TOKEN",
                "refresh": f"CONNECTOR_{slug}_REFRESH_TOKEN",
                "client_secret": f"CONNECTOR_{slug}_CLIENT_SECRET"
                if row.get("client_secret") else None,
                "expires_at": self.clock() + float(tokens.get("expires_in") or 3600)}
        desks = row["desks"]
        with _oauth_lock(self.root):
            _store_token(self.vault_path, meta["access"], tokens["access_token"],
                         item["title"], desks)
            if tokens.get("refresh_token"):
                _store_token(self.vault_path, meta["refresh"], tokens["refresh_token"],
                             item["title"], desks)
            if meta["client_secret"]:
                _store_token(self.vault_path, meta["client_secret"], row["client_secret"],
                             item["title"], desks)
            doc = _oauth_doc(self.root)
            doc["connectors"][item["id"]] = meta
            _save_oauth_doc(self.root, doc)
        self._settle_pending(state, "done")
        return self.install(item["id"], desks)

    def connect_status(self, state: str) -> dict:
        row = self._pending().get(str(state or ""))
        if row is None:
            raise StoreError("bad_state", "No sign-in with that state.")
        status = row.get("status") or "pending"
        if status == "pending" and self.clock() - float(row.get("created") or 0) > CONNECT_TTL:
            status = "expired"
        return {"state": status, "id": row.get("id"), "detail": row.get("detail") or ""}

    def refresh_oauth_due(self) -> list[dict]:
        """Refresh every connected token near expiry, before a desk asks."""
        out = []
        now = self.clock()
        with _oauth_lock(self.root):
            doc = _oauth_doc(self.root)
            grants = {m.name: m.grants for m in vault.meta(self.vault_path)}
            for item_id, meta in doc["connectors"].items():
                if float(meta.get("expires_at") or 0) - now >= REFRESH_MARGIN:
                    continue
                reader = next(iter(grants.get(meta["refresh"]) or ()), None)
                try:
                    if reader is None:
                        raise mcp_oauth.OAuthError("reconnect", "No desk holds this sign-in.")
                    _refresh_locked(item_id, meta, store_dir=self.root,
                                    vault_path=self.vault_path, reader=reader,
                                    post=self.post, now=now)
                    out.append({"id": item_id, "ok": True})
                except mcp_oauth.OAuthError as exc:
                    meta["broken"] = True
                    out.append({"id": item_id, "ok": False, "reason": "reconnect",
                                "detail": exc.detail})
            _save_oauth_doc(self.root, doc)
        return out

    def _vault_name(self, item: dict, field: str) -> str:
        slug = _env_name(item["name"].partition("/")[2] or item["name"])
        return f"CONNECTOR_{slug}_{_env_name(field)}"

    def _install_connector(self, item: dict, targets: list, secrets: dict):
        recipe = dict(item["_recipe"])
        if item.get("auth") == "unknown" and recipe.get("url"):
            if self.probe(recipe["url"]) == "oauth":
                raise StoreError("needs_oauth", OAUTH_WHY)
        names = [d.name for d in targets]
        stored = {m.name: m for m in vault.meta(self.vault_path)}
        plan: list[tuple[str, str, str | None]] = []   # (field, vault name, value)
        oauth = _oauth_doc(self.root)["connectors"].get(item["id"])
        if item.get("auth") == "oauth" and oauth:
            recipe["oauth"] = True
            for field, key in (("ACCESS_TOKEN", "access"), ("REFRESH_TOKEN", "refresh"),
                               ("CLIENT_SECRET", "client_secret")):
                if oauth.get(key) and oauth[key] in stored:
                    plan.append((field, oauth[key], None))
        for field in item.get("secrets") or []:
            vname = self._vault_name(item, field["name"])
            value = secrets.get(field["name"])
            if isinstance(value, str) and value.strip():
                plan.append((field["name"], vname, value.strip()))
            elif vname in stored:
                plan.append((field["name"], vname, None))
            elif field.get("required"):
                raise StoreError("missing_secret", f"{item['title']} needs "
                                 f"{field['name']}; enter it to install.")
        for field, vname, value in plan:
            if value is not None:
                grants = set(stored[vname].grants) if vname in stored else set()
                vault.put(self.vault_path, name=vname, value=value,
                          description=f"{item['title']}: {field} (Connectors & Skills)",
                          grants=sorted(grants | set(names)))
            else:
                for n in names:
                    vault.grant(self.vault_path, vname, n)
        ledger = self._ledger()
        at = _iso(self.clock())
        rows, notes = [], {}
        for d in targets:
            mine = ledger["desks"].setdefault(d.name, {})
            taken = {e.get("server") for i, e in mine.items() if i != item["id"]}
            server = (mine.get(item["id"]) or {}).get("server")
            if not server:
                base = _server_slug(item["name"].partition("/")[2] or item["name"])
                server, n = base, 2
                while server in taken or server in RESERVED_SERVERS:
                    server, n = f"{base}-{n}", n + 1
            mine[item["id"]] = {"id": item["id"], "kind": "connector",
                                "name": item["name"], "title": item["title"],
                                "server": server, "version": item.get("version"),
                                "trust": item["trust"], "installed_at": at,
                                "recipe": recipe,
                                "vault": {f: v for f, v, _ in plan}}
            rows.append({"desk": d.name, "id": item["id"], "version": item.get("version"),
                         "commit": None, "installed_at": at})
            notes[d.name] = (f"[Agent Deck] Your tools changed: the connector "
                             f"'{server}' ({item['title']}) was added. Its tools are "
                             f"mcp__{server}__*; load them with ToolSearch when you "
                             "need them.")
        atomic.write_text(self.ledger_path, json.dumps(ledger, indent=1))
        self._write_configs(names)
        return rows, notes

    def _write_configs(self, names: list[str]) -> None:
        from . import deck_mcp
        for name in names:
            try:
                deck_mcp.config_file(name, ledger_path=self.ledger_path)
            except ValueError:
                pass

    # ── uninstall ──

    def uninstall(self, item_id: str, desks) -> dict:
        item_id = str(item_id or "")
        targets = self._targets(desks)
        removed, notes = [], {}
        with self._flock():
            if item_id.startswith("skill:"):
                markers = self._skill_markers()
                for d in targets:
                    if item_id not in markers.get(d.name, {}):
                        raise StoreError("not_installed", f"{item_id} is not on {d.name}.")
                for d in targets:
                    folder = Path(markers[d.name][item_id]["_folder"])
                    shutil.rmtree(folder, ignore_errors=True)
                    removed.append({"desk": d.name, "id": item_id})
                    # MEASURED: a resumed desk still lists a removed skill --
                    # its conversation carries the list it started with.
                    notes[d.name] = (f"[Agent Deck] Your tools changed: the skill "
                                     f"'{folder.name}' was removed and no longer "
                                     "exists here. Do not use it, even if it still "
                                     "appears in your skills list.")
            elif item_id.startswith("plugin:"):
                plugins = self._ledger().get("plugins") or {}
                for d in targets:
                    if item_id not in (plugins.get(os.path.normpath(d.cwd)) or {}):
                        raise StoreError("not_installed", f"{item_id} is not on {d.name}.")
                pid = item_id.removeprefix("plugin:")
                for cwd in dict.fromkeys(os.path.normpath(d.cwd) for d in targets):
                    self._cli(["plugin", "uninstall", pid, "--json", "-s", "local"], cwd)
                ledger = self._ledger()
                for d in targets:
                    (ledger.setdefault("plugins", {}).get(os.path.normpath(d.cwd)) or {}
                     ).pop(item_id, None)
                    removed.append({"desk": d.name, "id": item_id})
                    notes[d.name] = (f"[Agent Deck] Your tools changed: the plugin "
                                     f"'{pid}' was removed and no longer exists here. "
                                     "Do not use its skills or commands.")
                atomic.write_text(self.ledger_path, json.dumps(ledger, indent=1))
            else:
                ledger = self._ledger()
                for d in targets:
                    if item_id not in (ledger["desks"].get(d.name) or {}):
                        raise StoreError("not_installed", f"{item_id} is not on {d.name}.")
                for d in targets:
                    entry = ledger["desks"][d.name].pop(item_id)
                    for vname in (entry.get("vault") or {}).values():
                        self._drop_grant(vname, d.name)
                    removed.append({"desk": d.name, "id": item_id})
                    notes[d.name] = (f"[Agent Deck] Your tools changed: the connector "
                                     f"'{entry.get('server')}' was removed.")
                atomic.write_text(self.ledger_path, json.dumps(ledger, indent=1))
                self._write_configs([d.name for d in targets])
                if not any(item_id in (rows or {}) for rows in ledger["desks"].values()):
                    with _oauth_lock(self.root):
                        doc = _oauth_doc(self.root)
                        if doc["connectors"].pop(item_id, None) is not None:
                            _save_oauth_doc(self.root, doc)
        reloads = [self.reloader(d.name, notes[d.name]) for d in targets]
        return {"ok": True, "removed": removed, "reload": reloads}

    def _drop_grant(self, vname: str, desk: str) -> None:
        try:
            meta = vault.revoke(self.vault_path, vname, desk)
        except (KeyError, ValueError):
            return
        if not meta.grants:
            vault.forget(self.vault_path, vname)

    # ── desks asking (deck tools) ──

    def request(self, desk: str, item_id: str, desks=None) -> dict:
        """A desk asks for an install. Trusted: done now. Unverified: a card."""
        item = self._lookup(str(item_id or ""))
        targets = desks if desks else [desk]
        if item["trust"] != "unverified":
            return self.install(item["id"], targets)
        names = [d.name for d in self._targets(targets)]
        req = f"req_{_secrets.token_hex(6)}"
        with self._flock():
            reqs = self._read(self.requests_path)
            reqs[req] = {"id": item["id"], "desks": names, "desk": desk,
                         "state": "pending", "ts": self.clock()}
            atomic.write_text(self.requests_path, json.dumps(reqs, indent=1))
        title = item["title"]
        card = decisions.create(
            desk, f"{desk} wants to install {title} ({item['kind']}) on "
                  f"{', '.join(names)}.",
            [{"label": "Install", "value": f"Install {title} [store:{req}]",
              "style": "danger"},
             {"label": "Don't install", "value": f"Don't install {title} [store:{req}]"}],
            help=(f"Unverified source: {item['trust_note']} Agents run with full "
                  "access to your server; a malicious connector or skill can do "
                  f"anything they can. Source: {item.get('repo') or item['name']}."),
            allow_custom=False)
        return {"ok": True, "pending_approval": True, "decision_id": card["id"],
                "detail": f"It is unverified, so {deck_owner.name()} has to approve it. You'll hear "
                          "his answer as a message."}

    def settle(self, req: str, approve: bool) -> dict:
        with self._flock():
            reqs = self._read(self.requests_path)
            row = reqs.get(req)
            if row is None:
                return {"ok": False, "reason": "unknown_request"}
            if row.get("state") != "pending":
                return {"ok": True, "already": True, "state": row.get("state")}
            row["state"] = "approved" if approve else "denied"
            atomic.write_text(self.requests_path, json.dumps(reqs, indent=1))
        if not approve:
            return {"ok": True, "denied": True}
        try:
            out = self.install(row["id"], row["desks"], accept_unverified=True)
        except StoreError as exc:
            return exc.body()
        return out


# ── what deck_mcp and the launcher read ──────────────────────────────────────


def helper_command(desk: str, item_id: str) -> str:
    return (f"PYTHONPATH={shlex.quote(ROOT)} {shlex.quote(sys.executable)} -m "
            f"server.connector_run --desk {shlex.quote(desk)} --id "
            f"{shlex.quote(item_id)} --headers")


def servers_for(desk: str, ledger_path: Path | None = None) -> dict[str, dict]:
    """`{server name: MCP config}` for the connectors on `desk`. No value of a
    secret is ever in here: keys arrive through the launcher. Never raises."""
    path = Path(ledger_path or INSTALLED_PATH)
    try:
        ledger = json.loads(path.read_text())
        rows = (ledger.get("desks") or {}).get(desk) or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out: dict[str, dict] = {}
    for item_id, entry in sorted(rows.items()):
        try:
            recipe = entry["recipe"]
            server = str(entry["server"])
            if server in RESERVED_SERVERS:
                continue
            if recipe["type"] in ("http", "sse"):
                cfg = {"type": recipe["type"], "url": recipe["url"]}
                if recipe.get("headers") or recipe.get("oauth"):
                    cfg["headersHelper"] = helper_command(desk, item_id)
            elif recipe.get("env"):
                cfg = {"type": "stdio", "command": sys.executable,
                       "args": ["-m", "server.connector_run", "--desk", desk,
                                "--id", item_id],
                       "env": {"PYTHONPATH": ROOT}}
            else:
                cfg = {"type": "stdio", "command": recipe["command"],
                       "args": list(recipe.get("args") or [])}
        except (KeyError, TypeError):
            continue
        out[server] = cfg
    return out


_default: Store | None = None


def default_store() -> Store:
    global _default
    if _default is None:
        _default = Store()
    return _default


_REQ = re.compile(r"\[store:(req_[0-9a-f]{12})\]")


def on_decision(card: dict) -> dict | None:
    """His answer to any decision card. None unless it is a store approval."""
    answer = str((card or {}).get("answer") or "")
    found = _REQ.search(answer)
    if not found:
        return None
    return default_store().settle(found.group(1), approve=answer.startswith("Install "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connectors")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rl = sub.add_parser("reload")
    rl.add_argument("--desk", required=True)
    rl.add_argument("--note", default="[Agent Deck] Your tools changed.")
    sub.add_parser("refresh")
    args = parser.parse_args(argv)
    if args.cmd == "refresh":
        doc = default_store().refresh(force=True)
        print(json.dumps({"items": len(doc["items"])}))
        return 0
    lock = RELOAD_LOG.parent / f".reload-{_server_slug(args.desk)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)   # one reload per desk at a time
        out = Reloader().run(args.desk, args.note)
    print(json.dumps({**out, "ts": _iso(time.time())}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
