"""A desk is told who it is and what it REALLY has, first (B4).

THE DEFECT, in the owner's words: "we need to let atlas know who he is and
what he's capable of because he can basically do anything a person can do in
an office ... also he can expand himself with skills and more so all of that
needs to go into his context". MEASURED on the box, 2026-09-30, atlas's job
`respawnFlags`: a 13.8k-character brief that named none of its four connected
claude.ai accounts, none of its nine skills or its plugin, and never said it
could add more; the one capability paragraph (the computer) sat deep in.

THE GOOD SIGNAL asserted below -- presence, and absence only where absence is
the claim (an undetected item must not be promised):

  * the brief OPENS with "Who you are" then "What you can do";
  * every detected item is named, and each undetected one is not;
  * the two sections stay under their caps however big the machine is;
  * chief, junior and onboarding desks get their own subset;
  * the detector reads the measured `claude mcp list` shape, finds skills and
    plugins on disk, reuses its subprocess answers, and never shells out to a
    CLI config home that has never been signed into (the unit suite's).

Hermetic: tmp dirs, fake `run`/`which`; no real `claude` is started.
"""

import json
import subprocess
from pathlib import Path

import pytest

from server import api, capabilities as caps, deck_mcp, hire, office, spawn
from server.roster import Desk

CHIEF = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m",
             label="COS", charter="Run the portfolio.")
JUNIOR = Desk(name="harbor", cwd="/tmp", engine="claude", mission="m",
              label="Harbor", charter="Own the harbor pipeline.",
              reports_to="atlas")
NEW = Desk(name="new-hire-abc123", cwd="/tmp", engine="claude", mission="m",
           label="New", charter="Work out the job.")
TEAM = [JUNIOR, Desk(name="acme-lead", cwd="/tmp", engine="claude",
                     mission="m", label="Acme Lead", charter="x",
                     reports_to="atlas")]

#: What atlas had on the box, 2026-09-30.
BOX = caps.Inventory(
    connected=["Claude Docs", "Gmail", "Google Calendar", "Google Drive"],
    needs_auth=["higgsfield", "Notion"],
    deck_servers=["computer", "deck"],
    skills=["docs", "docx", "pdf", "pptx", "xlsx", "skill-creator"],
    plugins=["cowork-plugin-management"],
    clis=["gh", "git", "docker", "python3", "node", "ffmpeg"],
    gh_signed_in=True, permission_mode="bypassPermissions", web=True,
    skills_dir="~/.claude/skills")

#: `claude mcp list`, verbatim from atlas's workspace on the box.
MCP_LIST = """Checking MCP server health…

claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - ✔ Connected
claude.ai higgsfield: https://mcp.higgsfield.ai/mcp - ! Needs authentication
claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Notion: https://mcp.notion.com/mcp - ! Needs authentication
broken: npx broken-mcp - ✗ Failed to connect
"""


def _brief(desk, inv=BOX, team=TEAM):
    return hire.brief(desk, inventory=inv, team=team)


# -- 1. placed where it is read ----------------------------------------------


def test_the_chief_brief_opens_with_identity_then_capabilities():
    text = _brief(CHIEF)
    assert text.startswith("Who you are\n")
    assert text.index("What you can do") < text.index("\nWhat you own\n")
    assert text.index("What you can do") < text.index("How you sound")
    assert text.index("What you can do") < 2600


def test_build_argv_hands_the_cli_the_new_head(monkeypatch):
    monkeypatch.setattr(caps, "current", lambda desk: BOX)
    argv = spawn.build_argv(CHIEF, background=True)
    system = argv[argv.index("--append-system-prompt") + 1]
    assert system.startswith("Who you are\nYou are atlas, the COS desk.")
    assert "Gmail (email)" in system


# -- 2. detected items named, undetected ones not -----------------------------


def test_every_detected_item_is_named():
    text = caps.render(BOX, CHIEF)
    for item in [*BOX.connected, *BOX.needs_auth, *BOX.skills, *BOX.plugins,
                 "gh (signed in to GitHub)", "docker", "ffmpeg",
                 "mcp__computer__", "say, ask, message_desk",
                 "WebSearch and WebFetch", "full access",
                 "~/.claude/skills/<name>/"]:
        assert item in text, item


def test_nothing_undetected_is_promised():
    bare = caps.Inventory()
    text = caps.render(bare, CHIEF)
    for absent in ("Connected accounts", "Not signed in", "mcp__computer__",
                   "mcp__mac__", "Skills:", "Plugins:", "a shell with",
                   "WebSearch", "full access", "gh"):
        assert absent not in text, absent
    assert "You can grow" in text, "self-expansion is always true"


def test_the_mac_is_named_only_when_its_server_is_wired():
    assert "mcp__mac__" not in caps.render(BOX, CHIEF)
    wired = caps.Inventory(**{**caps.as_dict(BOX),
                              "deck_servers": ["computer", "deck", "mac"],
                              "macs_paired": 1})
    text = caps.render(wired, CHIEF)
    assert "mcp__mac__" in text and "grant" in text
    unpaired = caps.Inventory(**{**caps.as_dict(wired), "macs_paired": 0})
    assert "no Mac is paired" in caps.render(unpaired, CHIEF)


def test_a_failed_server_is_said_to_be_down_not_offered():
    inv = caps.Inventory(failed=["broken"])
    assert "Not answering right now: broken." in caps.render(inv, CHIEF)


# -- 3. size caps --------------------------------------------------------------


def test_the_sections_stay_under_their_caps_on_a_big_machine():
    big = caps.Inventory(
        connected=[f"Connector {i}" for i in range(12)],
        needs_auth=[f"Other {i}" for i in range(6)],
        deck_servers=["computer", "deck", "mac"], macs_paired=1,
        skills=[f"skill-number-{i}" for i in range(80)],
        plugins=[f"plugin-{i}" for i in range(6)],
        clis=list(caps.CLIS), gh_signed_in=True,
        permission_mode="bypassPermissions", web=True,
        skills_dir="~/.claude/skills")
    team = [Desk(name=f"desk-number-{i}", cwd="/tmp", engine="claude",
                 mission="m", label=f"Label {i}", charter="x",
                 reports_to="atlas") for i in range(30)]
    for desk in (CHIEF, JUNIOR, NEW):
        assert len(caps.identity(desk, team)) <= caps.IDENTITY_MAX
        text = caps.render(big, desk)
        assert len(text) <= caps.CAPABILITIES_MAX, len(text)
        assert "more." in text, "the skills cut says how many it left out"
    assert "and 20 more" in caps.identity(CHIEF, team)


def test_the_measured_box_fits_comfortably():
    assert len(caps.identity(CHIEF, TEAM)) <= caps.IDENTITY_MAX
    assert len(caps.render(BOX, CHIEF)) <= caps.CAPABILITIES_MAX


# -- 4. each desk its subset ---------------------------------------------------


def test_the_chief_is_the_owners_chief_of_staff_with_a_team():
    text = caps.identity(CHIEF, TEAM)
    assert f"{office.OWNER_VOICE}'s chief of staff" in text
    assert "harbor (Harbor), acme-lead (Acme Lead)" in text
    assert "hire and direct desks" in text
    assert "says send" in text and "says buy" in text
    assert "hire a specialist desk" in caps.render(BOX, CHIEF)
    assert "as an `ask`" in caps.render(BOX, CHIEF)


def test_the_owner_name_is_read_not_written():
    text = caps.identity(CHIEF, TEAM, owner="Dana")
    assert "Dana's chief of staff" in text
    assert office.OWNER_VOICE not in text or office.OWNER_VOICE == "Dana"


def test_a_junior_is_not_the_chief_and_goes_through_its_boss():
    ident = caps.identity(JUNIOR, TEAM)
    assert "chief of staff" not in ident
    assert "under atlas" in ident
    assert "atlas says send" in ident
    text = caps.render(BOX, JUNIOR)
    assert "hire a specialist desk" not in text
    assert "say, ask" not in text, "ask is refused for a junior"
    assert "goes to atlas as a `say` first" in text


def test_an_onboarding_desk_is_told_what_it_can_offer_not_that_it_is_chief():
    ident = caps.identity(NEW, TEAM)
    assert "not yet named" in ident
    assert "chief of staff" not in ident
    assert "Reporting to you" not in ident
    text = caps.render(BOX, NEW)
    assert "Gmail (email)" in text
    assert "hire a specialist desk" not in text


def test_the_placeholder_prefix_matches_the_interview_door():
    assert caps.PLACEHOLDER_PREFIX == api.PLACEHOLDER_PREFIX
    assert caps.role(Desk(name=f"{api.PLACEHOLDER_PREFIX}-1a2b3c", cwd="/",
                          engine="claude", mission="m")) == "onboarding"


# -- 5. the detector -----------------------------------------------------------


def test_the_measured_mcp_list_parses():
    got = caps.parse_mcp_list(MCP_LIST)
    assert got == {"connected": ["Claude Docs", "Gmail", "Google Calendar",
                                 "Google Drive"],
                   "needs_auth": ["higgsfield", "Notion"],
                   "failed": ["broken"]}


class FakeRun:
    def __init__(self, plugin_root: Path):
        self.calls = []
        self.envs = []
        self.plugin_root = plugin_root

    def __call__(self, argv, **kw):
        self.calls.append(argv[1:])
        self.envs.append(kw.get("env"))
        out = MCP_LIST if argv[1:] == ["mcp", "list"] else json.dumps([
            {"id": "cowork-plugin-management@synced", "enabled": True,
             "installPath": str(self.plugin_root)},
            {"id": "off@synced", "enabled": False, "installPath": "/nope"}])
        return subprocess.CompletedProcess(argv, 0, out, "")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-home"
    for skill in ("pdf", "xlsx"):
        (home / "skills" / "synced" / "bucket" / skill).mkdir(parents=True)
        (home / "skills" / "synced" / "bucket" / skill / "SKILL.md").write_text("x")
    (home / "history.jsonl").write_text("")
    return home


def _which(name):
    return f"/usr/bin/{name}" if name in ("claude", "git", "docker") else None


def test_detect_reads_the_machine(tmp_path):
    home = _home(tmp_path)
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "create-cowork-plugin").mkdir(parents=True)
    (plugin / "skills" / "create-cowork-plugin" / "SKILL.md").write_text("x")
    workspace = tmp_path / "ws"
    (workspace / ".claude" / "skills" / "own-report").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "own-report" / "SKILL.md").write_text("x")
    desk = Desk(name="atlas", cwd=str(workspace), engine="claude", mission="m")
    run = FakeRun(plugin)
    inv = caps.detect(desk, home=home, bus=tmp_path / "bus", run=run,
                      which=_which, now=1000.0)
    assert inv.connected == ["Claude Docs", "Gmail", "Google Calendar",
                             "Google Drive"]
    assert inv.needs_auth == ["higgsfield", "Notion"]
    assert inv.plugins == ["cowork-plugin-management"]
    assert inv.skills == ["create-cowork-plugin", "own-report", "pdf", "xlsx"]
    assert inv.clis == ["git", "docker"]
    assert inv.deck_servers == sorted(
        json.loads(deck_mcp.config("atlas"))["mcpServers"])
    assert inv.permission_mode == "bypassPermissions" and inv.web


def test_the_cli_is_asked_as_the_desk_is_without_the_decks_token(
        tmp_path, monkeypatch):
    """MEASURED on the box: with the deck's CLAUDE_CODE_OAUTH_TOKEN set, `claude
    mcp list` says "No MCP servers configured" -- while the desk, running under
    the CLI daemon without it, has Gmail, Calendar, Drive and Docs."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-not-real")
    run = FakeRun(tmp_path)
    caps.detect(CHIEF, home=_home(tmp_path), bus=tmp_path, run=run,
                which=_which, now=1.0, cache_path=tmp_path / "c.json")
    assert run.envs and all(e is not None for e in run.envs)
    assert all("CLAUDE_CODE_OAUTH_TOKEN" not in e for e in run.envs)
    assert all("PATH" in e for e in run.envs)


def test_the_subprocess_answers_are_cached_then_refreshed(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(tmp_path)
    kw = dict(home=home, bus=tmp_path, run=run, which=_which)
    caps.detect(CHIEF, now=1000.0, **kw)
    caps.detect(CHIEF, now=1000.0 + caps.CACHE_TTL / 2, **kw)
    assert len(run.calls) == 2, "one start pays once"
    caps.detect(CHIEF, now=1000.0 + caps.CACHE_TTL + 1, **kw)
    assert len(run.calls) == 4, "a later fresh start re-detects"


def test_a_config_home_never_signed_into_is_not_shelled_out_to(tmp_path):
    run = FakeRun(tmp_path)
    inv = caps.detect(CHIEF, home=tmp_path / "empty", bus=tmp_path, run=run,
                      which=_which, now=1.0)
    assert run.calls == []
    assert inv.connected == [] and inv.plugins == []


def test_detect_never_raises_when_the_cli_is_broken(tmp_path):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    inv = caps.detect(CHIEF, home=_home(tmp_path), bus=tmp_path, run=boom,
                      which=_which, now=1.0)
    assert inv.connected == [] and inv.skills == ["pdf", "xlsx"]


def test_the_macs_paired_count_reads_the_node_file(tmp_path):
    (tmp_path / "mac").mkdir()
    (tmp_path / "mac" / "nodes.json").write_text(json.dumps({"n1": {}}))
    inv = caps.detect(CHIEF, home=tmp_path / "empty", bus=tmp_path,
                      run=FakeRun(tmp_path), which=_which, now=1.0)
    assert inv.macs_paired == 1


@pytest.mark.parametrize("desk", [CHIEF, JUNIOR, NEW], ids=str)
def test_the_default_path_builds_a_brief_without_a_real_cli(desk):
    """`spawn.build_argv` calls `brief` with no inventory; in the suite the
    CLI home is a throwaway, so this must work and must not shell out."""
    text = hire.brief(desk)
    assert text.startswith("Who you are\n")
    assert "What you can do" in text


def test_a_desk_is_told_it_can_change_its_own_avatar():
    text = caps.render(BOX, CHIEF)
    assert "set_my_look" in text and "avatar" in text
    assert "set_my_look" not in caps.render(caps.Inventory(), CHIEF)


def test_a_desk_is_told_its_browser_uploads_files_and_can_be_a_phone():
    """LIVE 2026-10-01: a growth desk told the owner "my browser can't
    upload the avatar or posts". It can; the brief must say so, and when."""
    text = caps.render(BOX, CHIEF)
    assert "upload_file" in text and "mobile_mode" in text
    assert "Instagram" in text
    assert "upload_file" not in caps.render(caps.Inventory(), CHIEF)
