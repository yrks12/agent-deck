"""Desks learn: a correction becomes a lesson, lessons are shared, procedures
become skills.

Owner, 2026-09-30: "are my agents learning over time how to execute stuff?"
MEASURED on the box that day: 5 of ~30 desk workspaces had any memory notes
(Atlas 5, listing-closer 4, new-hire-82d9ab 3, shorts-lead 2), there was no
shared team memory, no desk-written skill, no transcript where a desk reused a
past lesson, and the brief never told a desk to save or read one.

What this file holds the deck to:

* an owner (or engineer) correction is delivered WITH a nudge to save it --
  on both delivery paths, the live socket (`office.attribute`) and the queue
  (`hooks/cc-office.js`), and the two use the same detector;
* `mcp__deck__save_lesson` writes the lesson into the desk's own memory and,
  when shared, into team memory with an index;
* the team index is in every desk's brief AND in the versioned rules, so a
  running desk is handed a new lesson on its next message;
* the index has a size budget it never exceeds;
* a lesson or skill that carries a secret is refused, and nothing is written;
* `mcp__deck__save_skill` writes a SKILL.md a second desk's inventory sees;
* the same title saves once (dedupe), and the same skill name updates in place.

Hermetic: every store is a tmp dir.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from server import (capabilities, deck_mcp, features, hire, learning, office,
                    rules)
from server.roster import Desk

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "cc-office.js"

CORRECTION = ("don't ask me to take over the screen for a sign-in, open the "
              "sign-in page so the Chrome login card reaches my app, because "
              "takeover is my last resort")
ORDINARY = "what did the channel do yesterday?"

PROBE = {"name": "wake-probe", "cwd": "/srv/ws/wake-probe", "engine": "claude",
         "mission": "probe", "reports_to": "atlas"}
OTHER = {"name": "learn-probe", "cwd": "/srv/ws/learn-probe",
         "engine": "claude", "mission": "probe", "reports_to": "atlas"}
CHIEF = {"name": "atlas", "cwd": "/srv/ws/atlas", "engine": "claude",
         "mission": "run", "reports_to": None}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    team = tmp_path / "team-memory"
    skills = tmp_path / "skills"
    projects = tmp_path / "projects"
    monkeypatch.setattr(learning, "TEAM_DIR", team)
    monkeypatch.setattr(learning, "SKILLS_DIR", skills)
    monkeypatch.setattr(learning, "PROJECTS_DIR", projects)
    # save_lesson republishes the rules; keep that in the tmp bus too.
    monkeypatch.setattr(rules, "RULES_DIR", tmp_path / "rules")
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"version": 1,
                                  "agents": [CHIEF, PROBE, OTHER]}))
    monkeypatch.setattr(deck_mcp, "ROSTER_PATH", roster)
    return {"team": team, "skills": skills, "projects": projects,
            "root": tmp_path}


def tool(desk, which, **args):
    reply = deck_mcp.handle(desk, {"jsonrpc": "2.0", "id": 9,
                                   "method": "tools/call",
                                   "params": {"name": which,
                                              "arguments": args}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


LESSON = dict(
    title="Sign in with the Chrome login card, never takeover",
    fact="At a sign-in, open the site's sign-in page so the card reaches his "
         "app; he taps Use my Chrome login. Passkey is second. Takeover is "
         "never ours to ask for.",
    why="The owner corrected a desk that asked him to take over the screen.",
    how="Before any sign-in: check shared logins, then open the sign-in page "
        "and wait for the card.",
    shared=True)


# ── 1. a correction produces a lesson ─────────────────────────────────────


def test_the_detector_tells_a_correction_from_an_ordinary_message():
    assert learning.looks_like_correction(CORRECTION)
    assert learning.looks_like_correction("No, use the deck routine instead")
    assert learning.looks_like_correction("never use CronCreate")
    assert not learning.looks_like_correction(ORDINARY)
    assert not learning.looks_like_correction("thanks, great work")


@pytest.mark.parametrize("sender", ["owner", office.ENGINEER,
                                    office.ENGINEER_TEST])
def test_a_correction_on_the_socket_carries_the_save_a_lesson_nudge(sender):
    framed = office.attribute(CORRECTION, sender)
    assert learning.LESSON_NOTE in framed
    assert "mcp__deck__save_lesson" in learning.LESSON_NOTE


def test_an_ordinary_message_or_a_peer_gets_no_nudge():
    assert learning.LESSON_NOTE not in office.attribute(ORDINARY, "owner")
    assert learning.LESSON_NOTE not in office.attribute(CORRECTION, "scout",
                                                        who="scout")


def _queue(tmp_path, sender, text):
    home = tmp_path / "claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    now = time.time()
    (bus / "office.json").write_text(json.dumps({"generated_at": now,
        "sessions": {"sid-1": {"name": "wake-probe", "cwd": "/srv/w",
                               "toplevel": "/srv/w", "branch": "main",
                               "state": "IDLE"}}}))
    (bus / "messages.jsonl").write_text(json.dumps(
        {"id": "m1", "ts": now, "to": "sid-1", "from": sender,
         "text": text}) + "\n")
    done = subprocess.run(
        ["node", str(HOOK)], capture_output=True, text=True, timeout=10,
        input=json.dumps({"session_id": "sid-1",
                          "hook_event_name": "UserPromptSubmit"}),
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(home)})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def test_a_queued_correction_carries_the_same_nudge(tmp_path):
    assert learning.LESSON_NOTE in _queue(tmp_path, "engineer", CORRECTION)


def test_a_queued_ordinary_message_carries_no_nudge(tmp_path):
    assert learning.LESSON_NOTE not in _queue(tmp_path, "owner", ORDINARY)


def test_the_hook_and_the_deck_use_one_detector():
    js = HOOK.read_text()
    assert json.dumps(learning.CORRECTION_PATTERN, ensure_ascii=False) in js
    assert json.dumps(learning.LESSON_NOTE, ensure_ascii=False) in js


def test_save_lesson_writes_own_memory_and_team_memory(stores):
    out, err = tool("wake-probe", "save_lesson", **LESSON)
    assert not err, out
    lesson = stores["team"] / f"{out['slug']}.md"
    assert lesson.is_file()
    body = lesson.read_text()
    assert LESSON["fact"] in body and LESSON["why"] in body
    assert LESSON["how"] in body and "wake-probe" in body
    index = (stores["team"] / learning.INDEX_NAME).read_text()
    assert LESSON["title"] in index
    own = stores["projects"] / "-srv-ws-wake-probe" / "memory"
    assert (own / f"{out['slug']}.md").is_file()
    assert f"({out['slug']}.md)" in (own / "MEMORY.md").read_text()


def test_a_private_lesson_stays_in_the_desks_own_memory(stores):
    out, err = tool("wake-probe", "save_lesson", **{**LESSON, "shared": False})
    assert not err, out
    assert not (stores["team"] / f"{out['slug']}.md").exists()
    assert (stores["projects"] / "-srv-ws-wake-probe" / "memory"
            / f"{out['slug']}.md").is_file()


# ── 2. the shared index is injected and versioned ─────────────────────────


def test_the_team_index_is_in_the_brief_and_the_rules(stores):
    tool("wake-probe", "save_lesson", **LESSON)
    other = Desk(name="learn-probe", cwd="/srv/ws/learn-probe",
                 engine="claude", mission="probe", reports_to="atlas")
    text = hire.brief(other, inventory=capabilities.Inventory(), team=[])
    assert LESSON["title"] in text
    assert any(LESSON["title"] in line for line in rules.lines())


def test_a_new_lesson_changes_the_rules_version_a_running_desk_is_given(
        stores, tmp_path):
    rules.publish()
    before = rules.version()
    tool("wake-probe", "save_lesson", **LESSON)
    after = json.loads((tmp_path / "rules" / "current.json").read_text())
    assert after["version"] != before
    assert any(LESSON["title"] in line for line in after["lines"])


def test_the_brief_tells_a_desk_to_save_and_read_lessons(stores):
    text = "\n".join(rules.lines())
    assert "mcp__deck__save_lesson" in text
    assert "mcp__deck__save_skill" in text


# ── 3. the size budget holds ──────────────────────────────────────────────


def test_the_index_never_outgrows_its_budget(stores):
    for i in range(60):
        out, err = tool("wake-probe", "save_lesson", **{
            **LESSON, "title": f"Lesson number {i} about procedure {i}",
            "fact": f"Fact {i}. " * 20})
        assert not err, out
    section = learning.section()
    assert len(section) <= learning.INDEX_MAX, len(section)
    assert "Lesson number 59" in section          # newest shown
    assert "more" in section                       # the rest are counted
    assert len(list(stores["team"].glob("*.md"))) == 61   # 60 + INDEX


def test_an_oversized_lesson_is_refused(stores):
    out, err = tool("wake-probe", "save_lesson",
                    **{**LESSON, "fact": "x " * learning.LESSON_MAX})
    assert err and out["reason"] == "too_long"


# ── 4. secrets are rejected ───────────────────────────────────────────────


@pytest.mark.parametrize("leak", [
    "the password is hunter2",
    "api_key=sk-" "abcdef0123456789abcdef",
    "use token: ghp_" "ABCDEFGHIJKLMNOP1234567890",
    "curl https://bob:pa55word@example.com/x",
])
def test_a_lesson_with_a_secret_is_refused_and_nothing_written(stores, leak):
    out, err = tool("wake-probe", "save_lesson", **{**LESSON, "how": leak})
    assert err and out["reason"] == "secret"
    assert not stores["team"].exists() or not list(stores["team"].glob("*.md"))
    assert not stores["projects"].exists()


def test_a_skill_with_a_secret_is_refused(stores):
    out, err = tool("wake-probe", "save_skill", name="deploy-acme",
                    description="Deploy acme", body="export TOKEN=abc123def")
    assert err and out["reason"] == "secret"
    assert not (stores["skills"] / "deploy-acme").exists()


# ── 5. save_skill writes a loadable SKILL.md a second desk sees ───────────


def test_a_saved_skill_is_loadable_and_a_second_desk_sees_it(stores):
    out, err = tool("wake-probe", "save_skill", name="publish-a-short",
                    description="Publish a finished Short to YouTube from a "
                                "desk browser",
                    body="1. Copy the mp4 into Uploads.\n2. Open Studio.\n"
                         "3. Upload, set title, publish.")
    assert not err, out
    md = stores["skills"] / "publish-a-short" / "SKILL.md"
    text = md.read_text()
    assert text.startswith("---\nname: publish-a-short\ndescription: ")
    assert "\n---\n" in text and "Open Studio" in text
    second = Desk(name="learn-probe", cwd="/srv/ws/learn-probe",
                  engine="claude", mission="probe", reports_to="atlas")
    inv = capabilities.detect(second, home=stores["root"], bus=stores["root"],
                              which=lambda _: None,
                              cache_path=stores["root"] / "c.json")
    assert "publish-a-short" in inv.skills
    assert "publish-a-short" in learning.section()


def test_a_skill_never_overwrites_one_a_desk_did_not_write(stores):
    mine = stores["skills"] / "pdf"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: pdf\ndescription: x\n---\nhis")
    out, err = tool("wake-probe", "save_skill", name="pdf",
                    description="d", body="b")
    assert err and out["reason"] == "name_taken"
    assert (mine / "SKILL.md").read_text().endswith("his")


# ── 6. dedupe ─────────────────────────────────────────────────────────────


def test_the_same_lesson_title_saves_once(stores):
    first, _ = tool("wake-probe", "save_lesson", **LESSON)
    again, err = tool("learn-probe", "save_lesson",
                      **{**LESSON, "title": LESSON["title"].upper() + "!",
                         "how": "Updated how."})
    assert not err, again
    assert again["deduped"] is True and again["slug"] == first["slug"]
    lessons = [p for p in stores["team"].glob("*.md")
               if p.name != learning.INDEX_NAME]
    assert len(lessons) == 1
    body = lessons[0].read_text()
    assert "Updated how." in body and "learn-probe" in body
    index = (stores["team"] / learning.INDEX_NAME).read_text()
    assert index.count(f"({first['slug']}.md)") == 1


def test_the_same_skill_name_updates_in_place(stores):
    tool("wake-probe", "save_skill", name="deploy-deck",
         description="Deploy the deck", body="v1")
    out, err = tool("learn-probe", "save_skill", name="deploy-deck",
                    description="Deploy the deck", body="v2")
    assert not err and out["deduped"] is True
    assert (stores["skills"] / "deploy-deck" / "SKILL.md").read_text() \
        .rstrip().endswith("v2")


# ── 7. seeded, and every new tool is in the registry ──────────────────────


def test_seeding_fills_team_memory_once_and_dedupes(stores):
    n = learning.seed()
    assert n >= 4
    titles = "\n".join(l.title for l in learning.lessons())
    for must in ("Chrome login", "type_password", "deploy-box", "Mac"):
        assert must in titles, must
    assert learning.seed() == 0          # second run adds nothing


def test_the_new_tools_are_in_the_feature_registry():
    tools = {t for f in features.FEATURES for t in f.tools}
    assert "mcp__deck__save_lesson" in tools
    assert "mcp__deck__save_skill" in tools


def test_a_long_file_name_is_not_mistaken_for_a_secret(stores):
    out, err = tool("wake-probe", "save_lesson", **{
        **LESSON, "how": "Read sign-in-with-the-chrome-login-card-first-"
                         "passkey-second-neve.md in team memory first."})
    assert not err, out
