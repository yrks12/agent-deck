"""A desk must not forget who it is when the conversation is compacted.

The failure being pinned: a long-running session compacts, the early turns are
replaced by a summary, and the opening brief goes with them. The desk keeps
working and starts reporting to the owner instead of its boss -- with nothing
in any log saying why, because from the inside it is behaving reasonably.

The fix has to sit where the fault IS, not where it is noticed. Identity
delivered as a *message* is compactable. Identity in the **system prompt** is
re-sent on every request and cannot be summarised away. So the boss line
belongs in the argv, and these tests pin that it is actually there.

`hire.brief()` is the text that names the boss. `spawn.build_argv` previously
passed only `desk.mission` -- a one-liner that does not name anybody -- which
is exactly how a desk loses its boss and nobody notices.
"""

import json
import subprocess
from pathlib import Path

import pytest

from server import compaction, hire, spawn
from server.roster import Desk

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-compact.js"

ACME = Desk(name="acme-growth", cwd="/tmp", engine="claude", label="growth",
            mission="Own Acme paid acquisition.",
            charter="Own Acme paid acquisition: spend, creative, landing pages.",
            reports_to="acme")


def _system_prompt(argv: list[str]) -> str:
    """The text handed to --append-system-prompt, or '' if there is none."""
    if "--append-system-prompt" not in argv:
        return ""
    return argv[argv.index("--append-system-prompt") + 1]


def test_the_boss_is_named_in_the_system_prompt_not_only_in_a_message():
    """THE test. Revert build_argv to passing `desk.mission` and this fails.

    Asserts the good signal -- the boss's name is present in the one place
    compaction cannot reach -- rather than the absence of anything.
    """
    prompt = _system_prompt(spawn.build_argv(ACME, background=False))
    assert "acme" in prompt, "the boss is not named in the system prompt"
    assert "report" in prompt.lower()


def test_the_system_prompt_is_the_same_text_the_hire_was_briefed_with():
    """One source of truth. If the brief and the system prompt can drift, a
    desk can be told one thing on day one and carry a different one after a
    compaction -- the subtlest version of this bug."""
    prompt = _system_prompt(spawn.build_argv(ACME, background=False))
    assert prompt == hire.brief(ACME)


def test_a_root_desk_still_gets_its_own_line():
    """COS reports to the owner. That must survive compaction too."""
    cos = Desk(name="cos", cwd="/tmp", engine="claude", label="chief",
               mission="Run the company.", charter="Run the company.",
               reports_to=None)
    prompt = _system_prompt(spawn.build_argv(cos, background=False))
    assert "owner" in prompt.lower()


def test_a_desk_with_no_charter_still_names_its_boss():
    """The brief must degrade to naming the boss, never to nothing."""
    bare = Desk(name="x", cwd="/tmp", engine="claude", mission="", charter="",
                reports_to="cos")
    prompt = _system_prompt(spawn.build_argv(bare, background=False))
    assert "cos" in prompt


# ---------------------------------------------------------------- detection

def test_a_compaction_is_recorded_on_the_bus(tmp_path):
    """Prevention is not enough on its own: if the system prompt ever stops
    surviving, nothing would say so. A compaction has to be visible."""
    bus = tmp_path / "events.jsonl"
    compaction.record(bus, session_id="s1", agent="acme-growth", trigger="auto")
    line = json.loads(bus.read_text().splitlines()[-1])
    assert line["event"] == "compact"
    assert line["session_id"] == "s1"
    assert line["agent"] == "acme-growth"
    assert line["trigger"] == "auto"
    assert isinstance(line["ts"], float)


def test_the_repair_message_names_the_boss_too(tmp_path):
    """The belt to the system prompt's braces: what gets re-injected after a
    compaction must itself carry the boss line, or the repair repairs nothing."""
    msg = compaction.repair_message(ACME)
    assert "acme" in msg
    assert "report" in msg.lower()


def test_repair_is_skipped_for_a_session_with_no_desk(tmp_path):
    """An ad-hoc terminal session has no charter to restore. Re-injecting a
    generic lecture into somebody's scratch session is worse than nothing."""
    assert compaction.repair_message(None) is None


# ---------------------------------------------------------------- the hook

def _run_hook(payload: dict, env_extra: dict) -> subprocess.CompletedProcess:
    import os
    return subprocess.run(["node", str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=10,
                          env={**os.environ, **env_extra})


def test_the_hook_exits_clean_and_silent_on_the_happy_path(tmp_path):
    """Contract shared with cc-bus.js: always exit 0, never write stdout.
    A hook that prints breaks the session it is trying to protect."""
    p = _run_hook({"session_id": "s1", "trigger": "auto"},
                  {"DECK_URL": "http://127.0.0.1:1"})
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_the_hook_survives_a_dead_deck(tmp_path):
    """The daemon being down must never take a real session with it."""
    p = _run_hook({"session_id": "s1", "trigger": "manual"},
                  {"DECK_URL": "http://10.255.255.1:7788"})
    assert p.returncode == 0
