"""Detector 1: the prompt-time door covers every tool that can draw a modal,
and the question the owner gets about one is a sentence he can read.

WHY THIS FILE EXISTS. `PermissionRequest` was registered against the same
narrow matcher as `PreToolUse` -- `Bash|Read|Write|Edit|NotebookEdit|WebFetch`
-- because nobody had measured whether the event fires for anything else. A
hired desk that prompts on an MCP tool therefore still froze on a modal nobody
outside the process can answer, which is the whole defect the door was built to
close.

MEASURED, Claude Code 2.1.252, interactive pty, `--settings` registering one
`PermissionRequest` hook with `matcher: "*"`, evidence read from the hook's own
JSONL log and never from the screen:

* control, Bash::

      {"event":"PermissionRequest","tool":"Bash",
       "input_keys":["command","description"]}

* an MCP tool::

      {"event":"PermissionRequest",
       "tool":"mcp__claude_ai_Google_Calendar__list_calendars",
       "input_keys":[]}

* `AskUserQuestion`::

      {"event":"PermissionRequest","tool":"AskUserQuestion",
       "input_keys":["questions"]}

So the event fires for all three, and `matcher: "*"` matches all three. (Also
measured, and worth writing down: `PermissionRequest` does NOT fire in `-p`
headless mode at all. A run there produced a real `permission_denials` entry
with the hook log empty, so headless is not a rig this can be measured in.)

WHY WIDENING IS SAFE HERE AND NOT ON `PreToolUse`. `PreToolUse` runs inside
every tool call in the session; a bug in a wide matcher there costs every
search and every read. `PermissionRequest` fires only where a modal was already
about to be drawn -- the session was going to stop regardless -- so the worst a
bug can do is turn a stall into a denial, and a denial is a sentence the agent
can read out loud and route around.

THE COUNTER-ARGUMENT, AND THE HALF OF IT THAT IS REAL. A denial the owner
cannot interpret is barely better than a stall. `autoreview.tool_subject`
falls back to `json.dumps(tool_input)`, so an MCP ask reached the phone reading
`wants to use mcp__claude_ai_Google_Calendar__list_calendars on {}`. That is
why this file asserts on the *message*, not just on the matcher.
"""

import json
import re

import pytest

from server import approval, asking, autoreview

# Read off the hook log of the pty runs described above. Each of these was
# observed to fire `PermissionRequest` on 2.1.252.
MEASURED_PROMPTING_TOOLS = (
    "Bash",
    "mcp__claude_ai_Google_Calendar__list_calendars",
    "AskUserQuestion",
)


def matches(matcher: str, tool_name: str) -> bool:
    """Claude Code's own hook-matcher semantics: `*` or empty is everything,
    anything else is a regex over the tool name."""
    if matcher in ("", "*"):
        return True
    return re.fullmatch(matcher, tool_name) is not None


def permission_matcher() -> str:
    document = approval.settings_document("/usr/bin/node")
    entries = document["hooks"]["PermissionRequest"]
    assert len(entries) == 1, entries
    return entries[0].get("matcher", "")


# ── the door is wide enough ────────────────────────────────────────────────


@pytest.mark.parametrize("tool", MEASURED_PROMPTING_TOOLS)
def test_the_prompt_time_hook_is_registered_for_every_tool_measured_to_prompt(tool):
    """The GOOD signal: the hook that cannot stall is asked about this tool."""
    assert matches(permission_matcher(), tool), (
        f"{tool} was MEASURED to fire PermissionRequest on 2.1.252, but the "
        f"deck registers that hook as {permission_matcher()!r}, so a hired "
        "desk prompting on it freezes on a modal nobody can answer."
    )


def test_pretooluse_stays_narrow():
    """Deliberately NOT widened. `PreToolUse` is on the hot path of every tool
    call; `PermissionRequest` only fires where a modal was already coming."""
    document = approval.settings_document("/usr/bin/node")
    assert document["hooks"]["PreToolUse"][0]["matcher"] == \
        "Bash|Read|Write|Edit|NotebookEdit|WebFetch"


# ── and the ask it produces is readable ────────────────────────────────────


def ask_for(tool: str, tool_input: dict) -> asking.Ask:
    return asking.Ask(
        id="ab3de", ts=0.0, agent="Acme", tool=tool,
        subject=asking.redact(autoreview.tool_subject(tool, tool_input)),
        cwd="/Users/samcarter/Projects/acme",
    )


def test_an_mcp_ask_names_the_server_and_the_operation():
    """The GOOD signal: the owner can tell what he is being asked about.

    `mcp__claude_ai_Google_Calendar__list_calendars` is the wire name. What has
    to reach the phone is the server and the operation as words.
    """
    message = asking.compose(
        ask_for("mcp__claude_ai_Google_Calendar__list_calendars", {}))
    assert "Google Calendar" in message
    assert "list calendars" in message or "list_calendars" in message
    assert "mcp__" not in message, (
        "the raw wire name is not a question a human can answer: " + message)
    assert "{}" not in message, (
        "an empty tool input reached the phone as a JSON blob: " + message)


def test_an_mcp_ask_says_what_the_arguments_were():
    message = asking.compose(ask_for(
        "mcp__docker-mcp__container_logs", {"container": "acme-api", "tail": 50}))
    assert "container=acme-api" in message
    assert '{"container"' not in message, (
        "raw JSON reached the phone: " + message)


def test_an_askuserquestion_ask_carries_the_question_itself():
    """The GOOD signal: he is shown the question the agent wanted to ask him,
    not the serialised shape of the tool call."""
    message = asking.compose(ask_for("AskUserQuestion", {"questions": [
        {"question": "Ship the migration tonight or wait for Monday?",
         "options": [{"label": "tonight"}, {"label": "Monday"}]}]}))
    assert "Ship the migration tonight or wait for Monday?" in message
    assert "options" not in message, (
        "the tool's own JSON leaked into the phone message: " + message)


def test_a_widened_ask_can_still_be_answered_always():
    """`always` on an ask whose subject is empty must produce a rule rather
    than raise -- an unanswerable question is the bug being fixed, and a 500 on
    the owner's tap is the same bug wearing a different hat."""
    rule = asking.rule_from(
        ask_for("mcp__claude_ai_Google_Calendar__list_calendars", {}), "always")
    assert rule.tool == "mcp__claude_ai_Google_Calendar__list_calendars"
    assert rule.pattern
    assert autoreview.evaluate(
        [rule],
        tool_name="mcp__claude_ai_Google_Calendar__list_calendars",
        tool_input={},
        cwd="/Users/samcarter/Projects/acme").decision == "allow"


def test_the_rule_from_an_mcp_answer_does_not_leak_to_another_mcp_tool():
    """The floor: one answer about one MCP operation is not an answer about
    the whole server."""
    rule = asking.rule_from(ask_for(
        "mcp__docker-mcp__container_logs", {"container": "acme-api"}), "always")
    assert autoreview.evaluate(
        [rule], tool_name="mcp__docker-mcp__container_remove",
        tool_input={"container": "acme-api"},
        cwd="/Users/samcarter/Projects/acme").decision == "abstain"


def test_tool_subject_still_never_raises_for_a_shape_nobody_predicted():
    assert isinstance(
        autoreview.tool_subject("mcp__x__y", {"a": [1, {"b": 2}], "c": None}), str)
    assert isinstance(autoreview.tool_subject("AskUserQuestion", {}), str)
    assert isinstance(
        autoreview.tool_subject("AskUserQuestion", {"questions": "nope"}), str)
    assert json.dumps(autoreview.tool_subject("mcp__x__y", {}))
