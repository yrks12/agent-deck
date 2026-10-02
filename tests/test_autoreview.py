from server.autoreview import Rule, evaluate, tool_subject, load_rules, save_rules

ALLOW_ALL_BASH = Rule(id="a", kind="always_allow", tool="Bash",
                      pattern="*", cwd="**")
ASK_PUSH = Rule(id="b", kind="require_approval", tool="Bash",
                pattern="git push*", cwd="**")


def test_require_approval_beats_always_allow():
    """The precedence rule. If this inverts, a narrow guard is silently
    swallowed by a broad allow -- the exact failure that makes an approval
    system worse than none."""
    v = evaluate([ALLOW_ALL_BASH, ASK_PUSH], tool_name="Bash",
                 tool_input={"command": "git push origin main"}, cwd="/x")
    assert v.decision == "ask"
    assert v.rule_id == "b"


def test_rule_order_in_the_file_does_not_change_the_verdict():
    """Same two rules, reversed. Precedence must come from kind, not position."""
    v = evaluate([ASK_PUSH, ALLOW_ALL_BASH], tool_name="Bash",
                 tool_input={"command": "git push origin main"}, cwd="/x")
    assert v.decision == "ask"
    assert v.rule_id == "b"


def test_matching_always_allow_actually_allows():
    """Asserts the GOOD signal: a real allow, with the rule that granted it.
    Without this, a module that returned 'ask' for everything would pass the
    precedence tests above and be useless."""
    v = evaluate([ALLOW_ALL_BASH], tool_name="Bash",
                 tool_input={"command": "git status"}, cwd="/x")
    assert v.decision == "allow"
    assert v.rule_id == "a"


def test_no_rule_abstains_and_still_never_allows():
    """"Nothing matched" is not an opinion, so the deck no longer states one.

    It used to answer `ask`, and that was the defect the owner felt: a
    `PreToolUse` "ask" is returned AS the decision before Claude Code's own
    rules or classifier run, so the deck was not reporting his prompts, it was
    generating them -- thirteen cards, `pwd` among them. What must never happen
    is still asserted: silence never becomes permission.
    """
    v = evaluate([], tool_name="Bash", tool_input={"command": "ls"}, cwd="/x")
    assert v.decision == "abstain"
    assert v.decision != "allow"
    assert v.rule_id is None


def test_cwd_scopes_a_rule():
    scoped = Rule(id="c", kind="always_allow", tool="Read", pattern="*",
                  cwd="/Users/samcarter/Projects/**")
    inside = evaluate([scoped], tool_name="Read",
                      tool_input={"file_path": "/Users/samcarter/Projects/p/a.py"},
                      cwd="/Users/samcarter/Projects/p")
    outside = evaluate([scoped], tool_name="Read",
                       tool_input={"file_path": "/etc/passwd"}, cwd="/etc")
    assert inside.decision == "allow"
    assert outside.decision == "ask"


def test_secure_handoff_cannot_be_overridden():
    """A user rule that says 'allow everything' must still not auto-approve a
    credential read."""
    v = evaluate([Rule(id="d", kind="always_allow", tool="*", pattern="*",
                       cwd="**")],
                 tool_name="Read", tool_input={"file_path": "/x/.env"}, cwd="/x")
    assert v.decision == "ask"
    assert "handoff" in v.reason


def test_tool_subject_never_raises_on_an_unknown_tool():
    assert isinstance(tool_subject("SomeFutureTool", {"weird": [1, 2]}), str)


def test_rules_round_trip_through_the_file(tmp_path):
    path = tmp_path / "autoreview.json"
    save_rules(path, [ASK_PUSH])
    assert load_rules(path) == [ASK_PUSH]


def test_missing_file_is_no_rules_not_a_crash(tmp_path):
    assert load_rules(tmp_path / "nope.json") == []
