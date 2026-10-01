"""Every desk runs on the CURRENT rules, not the brief it was born with.

MEASURED 2026-09-30: a desk's brief is its `--append-system-prompt`, frozen in
the job's `respawnFlags`. A respawn or a wake brings back the SAME stale brief
(server/spawn.py `stop_job`). So after "you never type a password" was
replaced by "after Allow, use type_password", a growth desk still quoted
the old line to the owner. A deploy changed the words in the code and no
running desk ever read them.

The fix: the deck publishes the current rules with a version
(`rules.publish`), records which version each desk was started on
(`rules.mark_seen`), and the office hook -- on SessionStart (resume, compact,
startup) and on every prompt -- injects the rules that changed since that desk
last saw them, marking the lines they replace as superseded. Only on a change.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from server import hire, rules

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "cc-office.js"
DESK = "wake-probe"
SID = "sid-wake-probe-1"

OLD_COMPUTER_RULE = (
    "At a login, a 2FA code, a captcha or any payment, stop: the tools refuse "
    "to type there, and you never type a password or a card number. Tell "
    "whoever you report to, in one line, which site and why, and that it is "
    "waiting on your screen -- the owner will take over and sign in himself, "
    "and that sign-in is then yours in the same browser.")


@pytest.fixture
def bus(tmp_path):
    home = tmp_path / "claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    (bus / "office.json").write_text(json.dumps({"sessions": {
        SID: {"name": DESK, "cwd": "/srv/w", "toplevel": "/srv/w",
              "branch": "main", "state": "IDLE"}}}))
    return bus


def hook(bus: Path, event="SessionStart", source="resume") -> str:
    payload = {"session_id": SID, "hook_event_name": event, "source": source}
    done = subprocess.run(
        ["node", str(HOOK)], input=json.dumps(payload), capture_output=True,
        text=True, timeout=10,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(bus.parent)})
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


_CURRENT_SECTIONS = rules.sections


def v1_sections():
    """The rules as they were when the desk was started: the old prohibition."""
    old = hire.COMPUTER.split("\n")
    old = [line for line in old if "type_password" not in line] + [OLD_COMPUTER_RULE]
    return [s if s is not hire.COMPUTER else "\n".join(old)
            for s in _CURRENT_SECTIONS()]


def test_a_desk_born_on_v1_is_handed_v2_on_resume(bus, monkeypatch):
    monkeypatch.setattr(rules, "sections", v1_sections)
    rules.publish(bus)
    rules.mark_seen(DESK, bus)
    v1 = rules.version()
    monkeypatch.undo()

    rules.publish(bus)                       # v2 ships
    assert rules.version() != v1

    told = hook(bus, "SessionStart", "resume")
    assert "mcp__computer__type_password" in told, "the v2 permission"
    assert "superseded" in told.lower()
    after = told.lower().split("superseded", 1)[1]
    assert "you never type a password" in after, "names the line it replaces"

    # Only on a change: the next session start and the next prompt say nothing
    # about rules.
    assert "Rules updated" not in hook(bus, "SessionStart", "resume")
    assert "Rules updated" not in hook(bus, "UserPromptSubmit")


def test_a_desk_nobody_recorded_gets_the_whole_current_rules(bus):
    """Every desk that exists today was started before versions existed."""
    rules.publish(bus)
    told = hook(bus, "SessionStart", "compact")
    assert "Rules updated" in told
    assert "mcp__computer__type_password" in told
    assert "superseded" in told.lower()
    assert "never type a password" in told, "the known old line is named"
    assert "Rules updated" not in hook(bus, "UserPromptSubmit")


def test_a_desk_started_on_the_current_rules_is_told_nothing(bus):
    rules.publish(bus)
    rules.mark_seen(DESK, bus)
    assert "Rules updated" not in hook(bus, "SessionStart", "startup")


def test_the_rules_are_the_briefs_own_sections():
    from server import capabilities
    from server.roster import Desk
    desk = Desk(name=DESK, cwd="/tmp", engine="claude", mission="m",
                charter="c", reports_to=None)
    text = hire.brief(desk, inventory=capabilities.Inventory(), team=[])
    for section in rules.sections():
        assert section in text
