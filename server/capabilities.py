"""What a desk really has, detected at spawn -- and who it is (B4).

THE DEFECT, in the owner's words: "we need to let atlas know who he is and
what he's capable of because he can basically do anything a person can do in
an office ... also he can expand himself with skills and more so all of that
needs to go into his context". MEASURED on the box, 2026-09-30: atlas had
Gmail, Google Calendar, Google Drive and Claude Docs connected, nine skills and
a plugin installed, `gh` signed in, docker, ffmpeg, its own browser, and full
bypass permissions -- and its 13.8k-character system prompt named none of the
connectors, none of the skills, and never said it could add more. The one
capability paragraph it had (the computer) sat ~17k characters in, where
length dilutes adherence (docs/plans/2026-09-30-overhaul.md).

So two short sections, placed FIRST in the brief:

* `identity(...)` -- who the desk is, whose it is, who reports to it, and what
  "work like a person in an office would" means. Every fact is read from the
  roster (name, title, charter, team) and `office.OWNER_VOICE`; nothing about
  the companies is written here.
* `render(...)` -- a DETECTED inventory, never a wish list: the MCP servers in
  the desk's own `--mcp-config`, the claude.ai connectors connected vs. needing
  sign-in (`claude mcp list`), installed skills and plugins, CLIs on PATH, and
  the owner's Mac only when a `mac` server is actually wired. An item that was
  not detected is not mentioned.

`detect` is the one impure function. Its two subprocess calls (`claude mcp
list` ~2 s, `claude plugin list --json` ~0.3 s, measured on the box) are cached
for `CACHE_TTL` seconds so one start that builds the brief more than once pays
once; everything else is re-read on every call. `render` and `identity` are
pure. Refreshing is a fresh start -- docs/desk-capabilities.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import deck_mcp, deskperms
from .office import OWNER_VOICE
from .paths import BUS_DIR, CLAUDE_HOME
from .roster import Desk

#: Size caps the tests hold. Length dilutes adherence; these are the budget.
IDENTITY_MAX = 1200
CAPABILITIES_MAX = 1500

#: `api.PLACEHOLDER_PREFIX`, spelled out because `api` imports `hire`, which
#: imports this. `tests/test_capabilities.py` holds the two in step.
PLACEHOLDER_PREFIX = "new-hire"

#: Seconds a `claude mcp list` / `claude plugin list` answer is reused.
CACHE_TTL = float(os.environ.get("DECK_CAPABILITIES_TTL", "120"))
#: Under the CLI config home, outside `agent-bus/` (that is deck state).
CACHE_NAME = "cache/agent-deck-capabilities.json"
SUBPROCESS_TIMEOUT = 20.0

#: CLIs worth naming if present. Order is the order they are listed in.
CLIS = ("gh", "git", "docker", "python3", "node", "npm", "uv", "ffmpeg", "jq",
        "curl", "rg", "psql", "sqlite3", "aws", "gcloud", "kubectl",
        "terraform", "stripe", "vercel", "firebase")

#: What a known connector is FOR, so the line reads as a job, not a brand.
PURPOSE = {"gmail": "email", "google calendar": "calendar",
           "google drive": "files", "claude docs": "shared docs",
           "notion": "notes", "slack": "chat", "higgsfield": "AI video",
           "linear": "issues", "github": "code", "stripe": "payments",
           "hubspot": "CRM", "canva": "design", "figma": "design"}

#: The claude.ai account prefix `claude mcp list` puts on a connector's name.
CONNECTOR_PREFIX = "claude.ai "
SKILLS_SHOWN = 16
TEAM_SHOWN = 10


@dataclass(frozen=True)
class Inventory:
    connected: list[str] = field(default_factory=list)
    needs_auth: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: The servers in the desk's own `--mcp-config` (computer, deck, mac ...).
    deck_servers: list[str] = field(default_factory=list)
    macs_paired: int = 0
    skills: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    clis: list[str] = field(default_factory=list)
    gh_signed_in: bool = False
    permission_mode: str = ""
    web: bool = False
    skills_dir: str = ""


# ── the pure half ───────────────────────────────────────────────────────────


def role(desk: Desk) -> str:
    """`chief`, `junior` or `onboarding` -- which subset the desk is told."""
    if desk.name.startswith(PLACEHOLDER_PREFIX + "-"):
        return "onboarding"
    return "chief" if desk.reports_to is None else "junior"


def _joined(names: list[str]) -> str:
    return ", ".join(names)


def _with_purpose(name: str) -> str:
    what = PURPOSE.get(name.lower())
    return f"{name} ({what})" if what else name


def identity(desk: Desk, team: list[Desk] | None = None,
             owner: str = OWNER_VOICE) -> str:
    """Who this desk is. PURE. Facts from the roster only."""
    kind = role(desk)
    who = (f"You are {desk.name}, the {desk.label} desk." if desk.label
           else f"You are {desk.name}.")
    lines = ["Who you are"]
    if kind == "onboarding":
        lines.append(
            f"You are {desk.name} for now: a new hire on {owner}'s team, not "
            "yet named. What you can do is listed next; use it to offer him "
            "real options for the job. Once you name yourself, that job is who "
            "you are.")
        return "\n".join(lines)
    if kind == "chief":
        lines.append(
            f"{who} You are {owner}'s chief of staff: the one desk that "
            "answers to him directly and runs his companies for him. "
            "What you own is set out below; that and the roster are the facts, "
            "so name no company, customer or number they don't.")
    else:
        lines.append(
            f"{who} You are on {owner}'s team, under {desk.reports_to}. What "
            "you own is set out below.")
    team = [d for d in (team or []) if d.reports_to == desk.name]
    if team:
        shown = [f"{d.name} ({d.label})" if d.label else d.name
                 for d in team[:TEAM_SHOWN]]
        more = (f" and {len(team) - TEAM_SHOWN} more"
                if len(team) > TEAM_SHOWN else "")
        lines.append(f"Reporting to you: {_joined(shown)}{more}.")
    boss = owner if kind == "chief" else desk.reports_to
    lines.append(
        "Work the way a capable person in an office would, with a computer, "
        "accounts and a team: read and draft email, keep the calendar, write "
        "and file documents, research on the web and in a real browser, "
        "build and ship code, schedule recurring work"
        + (", hire and direct desks" if kind == "chief" else "")
        + ". An office's limits too: you draft, "
        f"{boss} says send; you find the purchase, {boss} says buy.")
    if kind == "chief":
        lines.append(
            f"He can call you live from his app, and an `ask` he leaves "
            "unanswered for a few minutes reaches his phone.")
    return "\n".join(lines)


def render(inv: Inventory, desk: Desk) -> str:
    """What this desk can use, from `inv`. PURE. An undetected item is absent.

    Held under CAPABILITIES_MAX by listing fewer skill names (the count of the
    rest is still said), never by dropping a kind of capability."""
    shown = min(len(inv.skills), SKILLS_SHOWN)
    while True:
        text = _render(inv, desk, shown)
        if len(text) <= CAPABILITIES_MAX or shown == 0:
            return text
        shown -= 1


def _render(inv: Inventory, desk: Desk, skills_shown: int) -> str:
    kind = role(desk)
    boss = "him" if kind != "junior" else desk.reports_to
    card = "an `ask`" if kind != "junior" else "a `say`"
    lines = ["What you can do (checked when this session started)"]
    if inv.connected:
        lines.append(
            "Connected accounts: "
            + _joined([_with_purpose(n) for n in inv.connected])
            + " -- use them as his own staff would.")
    if inv.needs_auth:
        lines.append(
            "Not signed in: " + _joined(inv.needs_auth)
            + f" -- if the work needs one, put it to {boss} as {card} to sign "
            "it in.")
    if inv.failed:
        lines.append("Not answering right now: " + _joined(inv.failed) + ".")
    tools = []
    if "computer" in inv.deck_servers:
        tools.append("your own Linux computer with a Chromium browser "
                     "(mcp__computer__*)")
    if deck_mcp.NAME in inv.deck_servers:
        tools.append("the deck (say, message_desk)" if kind == "junior"
                     else "the deck (say, ask, message_desk)")
    if inv.web:
        tools.append("WebSearch and WebFetch")
    if inv.clis:
        clis = ["gh (signed in to GitHub)" if c == "gh" and inv.gh_signed_in
                else c for c in inv.clis]
        tools.append("a shell with " + _joined(clis))
    if tools:
        lines.append("Your tools: " + _joined(tools) + ".")
    if "mac" in inv.deck_servers:
        where = ("" if inv.macs_paired else
                 " -- no Mac is paired yet, so it will say so")
        lines.append(f"{OWNER_VOICE}'s Mac: mcp__mac__* runs things on it, "
                     f"only within the grant he sets in the app{where}.")
    if inv.permission_mode == "bypassPermissions":
        lines.append("Permissions: full access -- no tool waits on a prompt.")
    if inv.skills:
        shown = inv.skills[:skills_shown]
        rest = len(inv.skills) - len(shown)
        more = (f" and {rest} more" if shown else f"{rest} installed") \
            if rest else ""
        lines.append("Skills: " + _joined(shown) + more + ".")
    if inv.plugins:
        lines.append("Plugins: " + _joined(inv.plugins) + ".")
    grow = [
        "`claude plugin install <plugin>` for a plugin",
        "`claude mcp add` for an MCP server",
        "a SKILL.md in " + (inv.skills_dir or "your skills dir")
        + "/<name>/ for a procedure you will repeat",
    ]
    if kind == "chief":
        grow.append("or hire a specialist desk")
    lines.append(
        "You can grow: when a job needs an ability you lack, add it -- "
        + _joined(grow) + ". New tools load on your next fresh start. "
        f"Anything that costs money, sends outside the company or changes "
        f"the plan still goes to {boss} as {card} first.")
    return "\n".join(lines)


# ── the impure half ─────────────────────────────────────────────────────────


def _claude_bin(which=shutil.which) -> str:
    found = which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else ""


def _cli_has_state(home: Path) -> bool:
    """A config home the CLI has never signed into has no connectors or
    plugins to list, and asking it costs a subprocess for nothing -- which is
    also what keeps the unit suite (a throwaway CLAUDE_CONFIG_DIR) from
    shelling out to a real `claude`."""
    return any((home / n).exists() for n in (
        ".credentials.json", "mcp-needs-auth-cache.json", "plugins",
        "history.jsonl"))


def parse_mcp_list(text: str) -> dict[str, list[str]]:
    """`claude mcp list` -> connected / needs_auth / failed server names.

    Lines look like `claude.ai Gmail: https://... - ✔ Connected`. The account
    prefix is dropped: the desk calls it Gmail."""
    out: dict[str, list[str]] = {"connected": [], "needs_auth": [], "failed": []}
    for raw in text.splitlines():
        line = raw.strip()
        if ": " not in line or " - " not in line:
            continue
        name, _, rest = line.partition(": ")
        status = rest.rsplit(" - ", 1)[-1].lower()
        name = name.strip()
        if name.startswith(CONNECTOR_PREFIX):
            name = name[len(CONNECTOR_PREFIX):]
        if "connected" in status and "fail" not in status:
            out["connected"].append(name)
        elif "auth" in status:
            out["needs_auth"].append(name)
        elif "fail" in status or "✗" in status or "error" in status:
            out["failed"].append(name)
    return out


#: Env the deck carries that a desk's session does not. MEASURED on the box:
#: the deck's unit loads `CLAUDE_CODE_OAUTH_TOKEN`, and with it `claude mcp
#: list` answers "No MCP servers configured" and `plugin list` `[]` -- that
#: token has no claude.ai connectors. A `--bg` desk runs under the CLI's own
#: daemon (job `backend: daemon`), whose env has no such token, on the signed-in
#: account: Gmail, Calendar, Drive, Docs connected. Ask the CLI as the desk is.
_NOT_THE_DESKS_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)


def _desk_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _NOT_THE_DESKS_ENV}


def _run(argv: list[str], cwd: str, run=subprocess.run) -> str:
    try:
        done = run(argv, cwd=cwd, capture_output=True, text=True,
                   timeout=SUBPROCESS_TIMEOUT, env=_desk_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    # stdout even on a non-zero exit: `mcp list` still lists what it saw.
    return done.stdout or ""


def _cached_cli(cwd: str, *, home: Path, run, which, now: float,
                cache_path: Path) -> dict:
    """The two subprocess answers for `cwd`, reused for CACHE_TTL seconds."""
    try:
        cache = json.loads(cache_path.read_text())
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    hit = cache.get(cwd)
    if isinstance(hit, dict) and now - float(hit.get("ts", 0)) < CACHE_TTL:
        return hit
    fresh = {"ts": now, "mcp": {}, "plugins": []}
    claude = _claude_bin(which) if _cli_has_state(home) else ""
    if claude:
        workdir = cwd if Path(cwd).is_dir() else str(Path.home())
        fresh["mcp"] = parse_mcp_list(_run([claude, "mcp", "list"], workdir, run))
        try:
            fresh["plugins"] = json.loads(
                _run([claude, "plugin", "list", "--json"], workdir, run) or "[]")
        except json.JSONDecodeError:
            fresh["plugins"] = []
    cache[cwd] = fresh
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, cache_path)
    except OSError:
        pass  # a cache that cannot be written costs speed, not the start
    return fresh


def _skill_names(*roots: Path) -> list[str]:
    names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for md in root.rglob("SKILL.md"):
                if len(md.relative_to(root).parts) <= 4:
                    names.add(md.parent.name)
        except OSError:
            continue
    return sorted(names)


def _macs_paired(bus: Path) -> int:
    try:
        nodes = json.loads((bus / "mac" / "nodes.json").read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(nodes, dict):
        nodes = nodes.get("nodes", nodes)
    return len(nodes) if isinstance(nodes, (list, dict)) else 0


def _tilde(path: Path) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def detect(desk: Desk, *, home: Path | None = None, bus: Path | None = None,
           run=subprocess.run, which=shutil.which, now: float | None = None,
           cache_path: Path | None = None) -> Inventory:
    """What `desk` actually has on this machine. Never raises."""
    home = home or CLAUDE_HOME
    bus = bus or BUS_DIR
    now = time.time() if now is None else now
    try:
        servers = sorted(json.loads(deck_mcp.config(desk.name))["mcpServers"])
    except (ValueError, KeyError, TypeError):
        servers = []
    cli = _cached_cli(desk.cwd, home=home, run=run, which=which, now=now,
                      cache_path=cache_path or home / CACHE_NAME)
    mcp = cli.get("mcp") or {}
    plugins = [p for p in cli.get("plugins") or []
               if isinstance(p, dict) and p.get("enabled", True)]
    plugin_skill_roots = [Path(str(p.get("installPath", ""))) / "skills"
                          for p in plugins if p.get("installPath")]
    skills_dir = home / "skills"
    gh_hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    clis = [c for c in CLIS if which(c)]
    return Inventory(
        connected=list(mcp.get("connected") or []),
        needs_auth=list(mcp.get("needs_auth") or []),
        failed=list(mcp.get("failed") or []),
        deck_servers=servers,
        macs_paired=_macs_paired(bus),
        skills=_skill_names(skills_dir, Path(desk.cwd) / ".claude" / "skills",
                            *plugin_skill_roots),
        plugins=sorted(str(p.get("id", "")).split("@")[0] for p in plugins
                       if p.get("id")),
        clis=clis,
        gh_signed_in="gh" in clis and gh_hosts.is_file(),
        permission_mode=deskperms.PERMISSION_MODE,
        web=desk.engine == "claude",
        skills_dir=_tilde(skills_dir),
    )


def current(desk: Desk) -> Inventory:
    """`detect` with the defaults; what `hire.brief` uses when not handed one."""
    return detect(desk)


def as_dict(inv: Inventory) -> dict:
    return asdict(inv)
