"""Desks learn: corrections become lessons, lessons are shared, procedures
become skills.

Owner, 2026-09-30: "are my agents learning over time how to execute stuff?"
MEASURED on the box that day: 5 of ~30 desk workspaces had any memory notes,
there was no shared team memory and no desk-written skill, no transcript
showed a desk reusing a past lesson, and the brief never told a desk to save
or read one. A correction the owner made to one desk was lost to every other.

Three small pieces, each on a path the deck already has:

* NUDGE. A message from the owner (or the engineer) that reads like a
  correction -- "don't", "instead", "next time", "never" -- is delivered with
  `LESSON_NOTE` under it, on both delivery paths: `office.attribute` (the live
  socket) and `hooks/cc-office.js` (the queue). Cheap regex, and the desk's
  own judgment decides; `CORRECTION_PATTERN` is byte-equal in both halves.
* LESSONS. `save_lesson` writes one fact + why + how-to-apply into the desk's
  own Claude memory (`~/.claude/projects/<slug>/memory/`, the format the CLI's
  auto-memory reads) and, when shared, into team memory
  (`~/.claude/agent-bus/team-memory/<slug>.md` + `INDEX.md`). Same title, same
  file: a second save updates it and adds the desk to `by`.
* SKILLS. `save_skill` writes `~/.claude/skills/<name>/SKILL.md` -- the user
  skills dir every desk's CLI loads -- with a `.desk-skill` marker, so a desk
  can update a desk-written skill but never overwrite one the owner installed.

`section()` -- how to learn, the team index (titles + one-line hooks) and the
desk-written skills -- is in every brief and in `rules.sections()`, so a new
lesson changes the rules version and a RUNNING desk is handed it on its next
message. It never exceeds `INDEX_MAX`: newest first, the rest counted.

Secrets are refused, never redacted: a lesson that needed a password to make
sense was the wrong lesson. Nothing is written when one is refused.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import atomic, office
from .paths import BUS_DIR, CLAUDE_HOME, PROJECTS_DIR, slug_for

#: Module constants so a test can point each store at a tmp dir.
TEAM_DIR: Path = BUS_DIR / "team-memory"
SKILLS_DIR: Path = CLAUDE_HOME / "skills"
INDEX_NAME = "INDEX.md"
SKILL_MARKER = ".desk-skill"
SEEDED_NAME = ".seeded.json"

#: The whole brief section, how-to included. Length dilutes adherence.
INDEX_MAX = 3500
TITLE_MAX = 90
HOOK_MAX = 110
FIELD_MAX = 1200
LESSON_MAX = 2400           # fact + why + how together
SKILL_DESC_MAX = 300
SKILL_BODY_MAX = 12_000
SKILLS_SHOWN = 8

#: The voices whose corrections are worth a lesson: him typing, and the
#: engineer (who is testing exactly this). Never a peer, never the deck.
LEARNS_FROM = frozenset(office.TYPED_BY_HIM | {office.ENGINEER,
                                               office.ENGINEER_TEST})

#: Cheap on purpose: a false positive costs one line of nudge, and the desk
#: decides. English words need a boundary on both sides; Hebrew prefixes do
#: not. Byte-equal (as a JSON string) in `hooks/cc-office.js`.
CORRECTION_PATTERN = (
    "(?:^|[\\s,.;:!?\"'(])(?:no[,.!]|don['’]?t|do not|never|stop|wrong|"
    "instead|not like that|i told you|that's not|that is not|from now on|"
    "next time|should have|shouldn't have|why did you|you forgot)"
    "(?=[\\s,.;:!?\"')]|$)|אל ת|לא ככה|לא נכון|תפסיק|במקום|בפעם הבאה")
_CORRECTION = re.compile(CORRECTION_PATTERN, re.IGNORECASE)

LESSON_NOTE = (
    "(If this corrects or overrules how you work: before you answer, save it "
    "with mcp__deck__save_lesson -- one fact, why, how to apply -- with "
    "shared=true if another desk could hit the same thing. Then do it the "
    "new way.)")

HOW_YOU_LEARN = "\n".join([
    "How you learn",
    "- When the owner or the engineer corrects or overrules how you work, "
    "save it before you answer: mcp__deck__save_lesson (one fact, why, how to "
    "apply). shared=true when any desk could hit the same thing -- it then "
    "reaches every desk's team memory below.",
    "- When a multi-step procedure worked and will recur (a deploy, a sign-in "
    "flow, publishing a video), save it as numbered steps with "
    "mcp__deck__save_skill; every desk loads it.",
    "- Before a job, check Team memory; when a lesson applies, read its file "
    "and name it by title as you apply it.",
    "- Never put a password, token or key in either: they are refused.",
])


class LearningError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── detectors ───────────────────────────────────────────────────────────────


def looks_like_correction(text: str) -> bool:
    return bool(_CORRECTION.search(str(text or "").lower()))


def note_for(sender: str, text: str) -> str:
    """`LESSON_NOTE` when `sender` is one we learn from and `text` reads like
    a correction, else "". PURE."""
    if (sender or "").strip().lower() not in LEARNS_FROM:
        return ""
    return LESSON_NOTE if looks_like_correction(text) else ""


_SECRETS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]*@",
    r"[?&](?:access_?token|api[_-]?key|apikey|auth|key|passwd|password|pwd|"
    r"secret|token)=[^&\s#]+",
    r"\b(?:passphrase|passwords?|passwd|pwd|secrets?|tokens?|api[_-]?keys?|"
    r"apikey|otp)\b\s*(?:is|are|:|=)\s*\S+",
    r"\b\w*(?:token|secret|password|passwd|api[_-]?key)\w*\s*[:=]\s*\S+",
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}",
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}|\bgh[pousr]_[A-Za-z0-9]{16,}"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{8,}|\bAKIA[0-9A-Z]{12,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b[0-9a-f]{32,}\b",
    # A long opaque run is a key only when it mixes cases and digits: a
    # kebab-case file name or slug is not one.
    r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])"
    r"[A-Za-z0-9_\-]{40,}\b",
))


def has_secret(*texts: str) -> bool:
    return any(p.search(str(t or "")) for t in texts for p in _SECRETS)


# ── helpers ─────────────────────────────────────────────────────────────────


def slug(title: str) -> str:
    """The dedupe key: same words, same file, whatever the case or punctuation."""
    out = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return out[:60].rstrip("-")


def _line(text: str) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, limit: int) -> str:
    text = _line(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hook(fact: str) -> str:
    first = re.split(r"(?<=[.!?])\s", _line(fact), maxsplit=1)[0]
    return _clip(first, HOOK_MAX)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _tilde(path: Path) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text
    meta = {}
    for row in head.splitlines():
        key, colon, value = row.partition(": ")
        if colon and not row.startswith(" "):
            meta[key.strip()] = value.strip()
    return meta, body


def _check(field: str, value, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise LearningError("bad_input", f"{field} is required")
    if len(text) > limit:
        raise LearningError("too_long", f"{field} is {len(text)} characters; "
                            f"the most is {limit}. Keep a lesson to one fact.")
    if office.MARK in text:
        raise LearningError("bad_input", f"{field} may not contain "
                            f"{office.MARK!r}: only the deck writes that")
    return text


def _publish_rules() -> None:
    """The index is part of the versioned rules: republish so running desks
    are handed the change on their next message. Never raises."""
    try:
        from . import rules
        rules.publish()
    except Exception:  # noqa: BLE001 - a lesson saved is not undone by this
        pass


# ── lessons ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lesson:
    slug: str
    title: str
    hook: str
    by: tuple[str, ...]
    updated: str
    path: Path


def _render(title: str, fact: str, why: str, how: str, by: list[str],
            kind: str) -> str:
    return (f"---\nname: {slug(title)}\ntitle: {_line(title)}\n"
            f"description: {_hook(fact)}\ntype: {kind}\n"
            f"by: {', '.join(by)}\nupdated: {_now()}\n---\n\n"
            f"{fact.strip()}\n\n**Why:** {why.strip()}\n\n"
            f"**How to apply:** {how.strip()}\n")


def _write_lesson(folder: Path, title: str, fact: str, why: str, how: str,
                  desk: str, kind: str) -> bool:
    """Write or update `<folder>/<slug>.md`. True if it already existed."""
    path = folder / f"{slug(title)}.md"
    by: list[str] = []
    existed = path.is_file()
    if existed:
        meta, _ = _frontmatter(path.read_text(errors="replace"))
        by = [b.strip() for b in meta.get("by", "").split(",") if b.strip()]
    if desk not in by:
        by.append(desk)
    folder.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, _render(title, fact, why, how, by, kind))
    return existed


def _own_memory(cwd: str, projects: Path, title: str, fact: str, why: str,
                how: str, desk: str) -> Path:
    """The desk's own Claude memory: the file plus its MEMORY.md line."""
    folder = projects / slug_for(cwd) / "memory"
    _write_lesson(folder, title, fact, why, how, desk, "feedback")
    index = folder / "MEMORY.md"
    name = f"{slug(title)}.md"
    entry = f"- [{_line(title)}]({name}) — {_hook(fact)}"
    try:
        rows = index.read_text().splitlines()
    except OSError:
        rows = []
    rows = [r for r in rows if f"({name})" not in r] + [entry]
    atomic.write_text(index, "\n".join(rows) + "\n")
    return folder / name


def lessons(team_dir: Path | None = None) -> list[Lesson]:
    """Every shared lesson, newest first."""
    folder = Path(team_dir or TEAM_DIR)
    out = []
    for path in folder.glob("*.md") if folder.is_dir() else ():
        if path.name == INDEX_NAME:
            continue
        try:
            meta, _ = _frontmatter(path.read_text(errors="replace"))
        except OSError:
            continue
        out.append(Lesson(slug=path.stem, title=meta.get("title", path.stem),
                          hook=meta.get("description", ""),
                          by=tuple(b.strip() for b in
                                   meta.get("by", "").split(",") if b.strip()),
                          updated=meta.get("updated", ""), path=path))
    out.sort(key=lambda l: (l.updated, l.slug), reverse=True)
    return out


def write_index(team_dir: Path | None = None) -> None:
    folder = Path(team_dir or TEAM_DIR)
    rows = ["# Team memory", "",
            "One lesson per file. Read the file when its line applies.", ""]
    rows += [f"- [{l.title}]({l.slug}.md) — {l.hook}" for l in lessons(folder)]
    folder.mkdir(parents=True, exist_ok=True)
    atomic.write_text(folder / INDEX_NAME, "\n".join(rows) + "\n")


def save_lesson(desk: str, title, fact, why, how, *, shared: bool = False,
                cwd: str | None = None, team_dir: Path | None = None,
                projects_dir: Path | None = None) -> dict:
    """Validate, then write to own memory and (if `shared`) team memory."""
    title = _check("title", title, TITLE_MAX)
    fact = _check("fact", fact, FIELD_MAX)
    why = _check("why", why, FIELD_MAX)
    how = _check("how", how, FIELD_MAX)
    if len(fact) + len(why) + len(how) > LESSON_MAX:
        raise LearningError("too_long", f"a lesson is at most {LESSON_MAX} "
                            "characters: one fact, why, and how to apply it")
    if not slug(title):
        raise LearningError("bad_input", "title needs some letters or digits")
    if has_secret(title, fact, why, how):
        raise LearningError("secret", "this looks like it carries a password, "
                            "token or key. Lessons are shared: say where the "
                            "secret lives (the vault, type_password), never "
                            "its value.")
    out: dict = {"ok": True, "slug": slug(title), "shared": bool(shared),
                 "deduped": False}
    if cwd:
        out["own"] = _tilde(_own_memory(cwd, Path(projects_dir or PROJECTS_DIR),
                                        title, fact, why, how, desk))
    if shared:
        folder = Path(team_dir or TEAM_DIR)
        out["deduped"] = _write_lesson(folder, title, fact, why, how, desk,
                                       "team")
        write_index(folder)
        out["team"] = _tilde(folder / f"{out['slug']}.md")
        _publish_rules()
    return out


def delete_lesson(name: str, team_dir: Path | None = None) -> bool:
    folder = Path(team_dir or TEAM_DIR)
    path = folder / f"{slug(name)}.md"
    if not slug(name) or not path.is_file():
        return False
    path.unlink()
    write_index(folder)
    _publish_rules()
    return True


# ── skills ──────────────────────────────────────────────────────────────────

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")


def save_skill(desk: str, name, description, body, *,
               skills_dir: Path | None = None) -> dict:
    """`<skills>/<name>/SKILL.md`, loadable by every desk's CLI."""
    name = str(name or "").strip()
    if not _SKILL_NAME.match(name):
        raise LearningError("bad_input", "name is 2-48 lowercase letters, "
                            "digits and dashes, e.g. publish-a-short")
    description = _line(_check("description", description, SKILL_DESC_MAX))
    body = _check("body", body, SKILL_BODY_MAX)
    if has_secret(name, description, body):
        raise LearningError("secret", "this looks like it carries a password, "
                            "token or key. Skills are shared: name where the "
                            "secret lives, never its value.")
    folder = Path(skills_dir or SKILLS_DIR) / name
    marker = folder / SKILL_MARKER
    existed = folder.exists()
    if existed and not marker.is_file():
        raise LearningError("name_taken", f"a skill named {name!r} is already "
                            "installed and was not written by a desk; pick "
                            "another name")
    folder.mkdir(parents=True, exist_ok=True)
    atomic.write_text(folder / "SKILL.md",
                      f"---\nname: {name}\ndescription: {description}\n---\n\n"
                      f"{body.strip()}\n")
    atomic.write_text(marker, json.dumps({"by": desk, "updated": _now()}))
    _publish_rules()
    return {"ok": True, "name": name, "deduped": existed,
            "path": _tilde(folder / "SKILL.md"),
            "loads": "on each desk's next fresh start; readable now"}


def desk_skills(skills_dir: Path | None = None) -> list[tuple[str, str]]:
    """(name, description) of every desk-written skill, by name."""
    root = Path(skills_dir or SKILLS_DIR)
    out = []
    for marker in sorted(root.glob(f"*/{SKILL_MARKER}")) if root.is_dir() else ():
        try:
            meta, _ = _frontmatter((marker.parent / "SKILL.md").read_text())
        except OSError:
            continue
        out.append((marker.parent.name, meta.get("description", "")))
    return out


# ── the brief section ───────────────────────────────────────────────────────


def section(team_dir: Path | None = None,
            skills_dir: Path | None = None) -> str:
    """How to learn, the team index and desk skills, under INDEX_MAX. PURE
    given the two dirs."""
    folder = Path(team_dir or TEAM_DIR)
    sroot = Path(skills_dir or SKILLS_DIR)
    head = [HOW_YOU_LEARN, "",
            f"Team memory (full lesson: {_tilde(folder)}/<file>):"]
    skills = desk_skills(sroot)[:SKILLS_SHOWN]
    tail = []
    if skills:
        tail.append(f"Desk-written skills ({_tilde(sroot)}/<name>/SKILL.md; "
                    "loaded on a fresh start, readable now): "
                    + "; ".join(f"{n} -- {_clip(d, 80)}" for n, d in skills))
    rows = [f"- {l.title} -- {l.hook} ({l.slug}.md)" for l in lessons(folder)]
    if not rows:
        rows_shown = ["- none yet"]
    else:
        rows_shown = []
        for n in range(len(rows), -1, -1):
            rows_shown = rows[:n]
            if n < len(rows):
                rows_shown.append(f"- and {len(rows) - n} more: ls "
                                  f"{_tilde(folder)}")
            if len("\n".join(head + rows_shown + tail)) <= INDEX_MAX:
                break
    text = "\n".join(head + rows_shown + tail)
    return text if len(text) <= INDEX_MAX else text[: INDEX_MAX - 1] + "…"


# ── seeding ─────────────────────────────────────────────────────────────────

#: Harvested 2026-09-30 from the desk memory notes on the box (atlas,
#: listing-closer, new-hire-82d9ab, shorts-lead, music-studio) and from the
#: fixes shipped that day. General lessons only; project facts stay put.
SEED: tuple[dict, ...] = (
    dict(title="Sign in with the Chrome login card first, passkey second, "
               "never takeover",
         fact="At a sign-in, open the site's sign-in page so its card reaches "
              "his app: he taps Use my Chrome login. Only if that fails, Sign "
              "in fresh with passkey. Never ask him to take over the screen.",
         why="Owner, 2026-09-30: desks asked for takeover for sign-ins his Mac "
             "could hand them in one tap, and the login then goes to every "
             "desk.",
         how="Check shared logins first; then open the sign-in page and wait "
             "for the card. Takeover is his last resort, not a request."),
    dict(title="Passwords go through type_password, never typed by you",
         fact="After Allow on a sign-in or sign-up, use "
              "mcp__computer__type_password for every password field; the deck "
              "generates or reuses it, keeps it in the vault and types it.",
         why="Typed passwords leak into transcripts and are lost to the other "
             "desks; the vault shares them safely.",
         how="Never type a password with type_text and never ask him for one."),
    dict(title="Deploy the deck only with bin/deploy-box",
         fact="Ship deck code to the box only with `DECK_BOX=... "
              "bin/deploy-box` from a clean checkout of main.",
         why="2026-09-30: sessions rsyncing code by hand overwrote a fresh "
             "deploy with older code, a silent rollback.",
         how="Never rsync or scp code to a server another session deploys "
             "to. If deploy-box refuses, rebase or wait for the lock."),
    dict(title="His Mac runs jobs only within the grant he sets",
         fact="Check mcp__mac__status first. In Ask me mode the first job "
              "raises a card on his Mac (Allow 1 hour / Always / Deny) and "
              "nothing runs until he taps Allow.",
         why="The Mac is his; a job that loops on a denied or offline Mac "
             "spams him.",
         how="Use the Mac only for what only it has; never retry when "
             "offline or paused; never copy secrets off it."),
    dict(title="Do the work yourself, do not hand steps back to the owner",
         fact="He hires desks to operate: never ask him to run a command or "
              "do a step you or a desk can do.",
         why="Owner to atlas, 2026-09-28: \"you should never ask me to do it. "
             "you should do it\".",
         how="Go to him only for a secret only he holds, a spend/send yes, or "
             "a hard block -- and then as one tap, not a procedure."),
    dict(title="Schedules live in deck routines, never CronCreate or crontab",
         fact="Recurring work is a YOS_ROUTINE line; he is on America/New_York "
              "time while the box runs UTC.",
         why="CronCreate and crontab jobs die with the session and he cannot "
             "see them; deck routines survive restarts and show in his app.",
         how="Write times in his zone and say the zone."),
    dict(title="Uploading a file from your browser goes through Uploads",
         fact="Copy the file into ~/.claude/agent-bus/browser/computers/<desk>"
              "/Uploads; the browser sees it as /home/agent/Uploads.",
         why="The desk's Chromium runs in its own container and cannot see "
             "the workspace.",
         how="Copy first, then pick it in the site's file dialog. type_text "
             "rejects emoji: leave them out."),
    dict(title="Never invent product claims; wait for a real pointer",
         fact="Do not draft collateral, listings or copy off a guessed feature "
              "set.",
         why="Wrong claims in a published listing are hard to walk back "
             "(listing-closer, 2026-09-07).",
         how="Do the research that does not depend on it, and ask your boss "
             "for the repo, docs or a plain description."),
)


def seed(team_dir: Path | None = None) -> int:
    """Add each SEED lesson once, ever: one deleted later stays deleted."""
    folder = Path(team_dir or TEAM_DIR)
    marker = folder / SEEDED_NAME
    try:
        done = set(json.loads(marker.read_text()))
    except (OSError, ValueError, TypeError):
        done = set()
    added = 0
    for item in SEED:
        key = slug(item["title"])
        if key in done:
            continue
        save_lesson("deck", item["title"], item["fact"], item["why"],
                    item["how"], shared=True, team_dir=folder)
        done.add(key)
        added += 1
    if added:
        folder.mkdir(parents=True, exist_ok=True)
        atomic.write_text(marker, json.dumps(sorted(done)))
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="learning")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("seed")
    gone = sub.add_parser("delete")
    gone.add_argument("slug")
    args = parser.parse_args(argv)
    if args.cmd == "seed":
        print(f"seeded {seed()}")
    elif args.cmd == "delete":
        print("deleted" if delete_lesson(args.slug) else "no such lesson")
    else:
        for l in lessons():
            print(f"{l.slug}\t{', '.join(l.by)}\t{l.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
