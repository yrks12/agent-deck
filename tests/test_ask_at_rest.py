"""A question about a command must not store the command's secret.

Found by an adversarial pass over the ask recorder. `asking.compose()` redacts,
so the message that reaches a phone is safe -- but `asking.record()` stored the
raw subject, so `asks.json` held the credential in cleartext:

    on disk : curl -H "Authorization: Bearer <an sk-ant token>" https://api
    composed: curl -H "Authorization: [redacted] [redacted]" https://api

That was survivable while nothing created asks. The recorder's whole purpose is
to start feeding real tool command lines into that file, which turns a latent
issue into a live one: every `curl -H "Authorization: ..."`, every `gh auth
login --with-token`, every psql URL an agent runs would be written to a
plaintext file and left there.

**The fix goes at the write, not the read.** Redacting in `compose()` protects
one consumer; anything else reading `asks.json` -- the client API, the board, a
backup, a future panel -- gets the raw value. Redacting in `record()` means the
secret never lands.

Two properties this must not break, both tested here: a rule built from a
redacted subject must still be a *usable* rule, and two calls that differ only
in their secret must still collapse to one row under the flood guard.

The second half of this file covers `handoff_verdict`, which the same pass found
to be entirely untested: replacing its body with `return False` passed all ten
of its module's tests. It is the guard that stops the UI offering "always" on a
credential prompt, so an undetected drift there hands out a standing permission
to read secrets.
"""

import json
from pathlib import Path

import pytest

from server import ask_recorder, asking

SECRET = "sk-ant-SECRET123"
CURL = f'curl -H "Authorization: Bearer {SECRET}" https://api.example.com'


def _rows(path: Path) -> list[dict]:
    return json.loads(path.read_text())["asks"]


def test_the_secret_never_reaches_the_file(tmp_path):
    """THE test. Revert the redaction in record() and this fails."""
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme", tool="Bash", subject=CURL, cwd="/tmp")
    raw = path.read_text()
    assert SECRET not in raw, "a credential was written to asks.json in cleartext"


def test_the_question_is_still_answerable(tmp_path):
    """The paired positive. A file that stored nothing would also pass the
    test above; the row must still say what was being asked about."""
    path = tmp_path / "asks.json"
    ask = asking.record(path, agent="acme", tool="Bash", subject=CURL, cwd="/tmp")
    row = _rows(path)[0]
    assert row["tool"] == "Bash"
    assert "curl" in row["subject"], "the row no longer says what was asked"
    assert row["cwd"] == "/tmp"
    assert ask.id


def test_a_rule_built_from_a_redacted_subject_is_still_usable(tmp_path):
    """`always` turns a subject into a rule. If redaction leaves a pattern that
    matches nothing, answering `always` silently grants nothing and the agent
    asks again forever -- the exact loop this feature exists to end."""
    path = tmp_path / "asks.json"
    ask = asking.record(path, agent="acme", tool="Bash", subject=CURL, cwd="/tmp")
    rule = asking.rule_from(ask, "always")
    assert rule.pattern.startswith("curl"), rule.pattern
    assert SECRET not in rule.pattern
    assert "*" in rule.pattern


def test_two_calls_differing_only_in_the_secret_are_one_question(tmp_path):
    """Rotating a token must not re-ask. Asserts the good signal both ways: one
    row for the pair, and a genuinely different command still gets its own."""
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme", tool="Bash", subject=CURL, cwd="/tmp")
    same_shape = CURL.replace(SECRET, "sk-ant-DIFFERENT456")
    assert asking.suppressed(path, tool="Bash", subject=same_shape, cwd="/tmp")
    assert not asking.suppressed(path, tool="Bash", subject="git push", cwd="/tmp")


# ---------------------------------------------------------------- the guard

def test_handoff_verdict_says_yes_to_a_credential():
    """Mutation-proof: `return False` must not survive this."""
    assert ask_recorder.handoff_verdict(
        "Bash", {"command": "gh auth login --with-token < token.txt"}) is True
    assert ask_recorder.handoff_verdict(
        "Read", {"file_path": "/Users/x/.env"}) is True


def test_handoff_verdict_says_no_to_something_ordinary():
    """And `return True` must not survive this one. Together they pin the
    function to the floor rather than to a constant."""
    assert ask_recorder.handoff_verdict(
        "Bash", {"command": "git status"}) is False


def test_handoff_verdict_agrees_with_the_row_it_would_produce(tmp_path):
    """The claim the function was written on: it cannot drift from the row.
    Nothing enforced that. Now something does."""
    path = tmp_path / "asks.json"
    for tool, tool_input in [
        ("Bash", {"command": "gh auth login --with-token < t.txt"}),
        ("Bash", {"command": "git status"}),
        ("Read", {"file_path": "/Users/x/.env"}),
        ("Read", {"file_path": "/Users/x/notes.md"}),
    ]:
        before = ask_recorder.handoff_verdict(tool, tool_input)
        ask = asking.record(path, agent="a", tool=tool,
                            subject=ask_recorder.tool_subject(tool, tool_input),
                            cwd="/tmp")
        assert ask_recorder.is_handoff_ask(ask) is before, (
            f"{tool} {tool_input}: verdict {before} but the row says "
            f"{ask_recorder.is_handoff_ask(ask)}")
