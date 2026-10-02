"""The ask loop: a rule the engine did not have becomes a rule it has forever.

These tests are written before server/asking.py exists. The one that matters
most is `rule_from`: it is the function that turns a one-word reply on a phone
into a standing permission, and a sloppy generalisation there ("git *") hands
over the machine while looking like a convenience feature.
"""

from pathlib import Path

import pytest

from server import asking
from server.autoreview import ALWAYS_ALLOW, DENY, evaluate, load_rules

ACME = "/Users/samcarter/Projects/acme"


def make(tmp_path, **kw):
    """Record one ask with sensible defaults, and return (path, ask)."""
    path = tmp_path / "asks.json"
    fields = {
        "agent": "acme-growth",
        "tool": "Bash",
        "subject": "gh pr create",
        "cwd": ACME,
    }
    fields.update(kw)
    return path, asking.record(path, **fields)


# ── recording ──────────────────────────────────────────────────────────────


def test_a_recorded_ask_comes_back_as_pending(tmp_path):
    path, ask = make(tmp_path)
    assert ask.id
    assert ask.answered is None
    assert ask.agent == "acme-growth"

    still = asking.pending(path)
    assert [a.id for a in still] == [ask.id]
    assert still[0].subject == "gh pr create"
    assert still[0].cwd == ACME


def test_an_answered_ask_is_no_longer_pending(tmp_path):
    path, ask = make(tmp_path)
    asking.answer(path, ask.id, "once")
    assert asking.pending(path) == []


def test_ids_are_short_and_unambiguous_to_type(tmp_path):
    path = tmp_path / "asks.json"
    ids = {
        asking.record(path, agent="a", tool="Bash", subject=f"echo {n}",
                      cwd=ACME).id
        for n in range(25)
    }
    assert len(ids) == 25, "ids collided"
    for i in ids:
        assert 3 <= len(i) <= 8
        # No 0/O/1/l/I: this gets read off a screen and typed on a phone.
        assert set(i) <= set(asking.ID_ALPHABET)


def test_the_file_is_capped_because_this_is_a_hot_path(tmp_path):
    path = tmp_path / "asks.json"
    for n in range(asking.MAX_ASKS + 12):
        asking.record(path, agent="a", tool="Bash", subject=f"echo {n}",
                      cwd=ACME, ts=1000.0 + n)
    kept = asking.pending(path)
    assert len(kept) == asking.MAX_ASKS
    # The NEWEST are what survive; the oldest are dropped.
    assert kept[-1].subject == f"echo {asking.MAX_ASKS + 11}"
    assert all(a.subject != "echo 0" for a in kept)


def test_a_missing_or_corrupt_file_is_no_asks_not_a_crash(tmp_path):
    assert asking.pending(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert asking.pending(bad) == []


# ── rule_from: the dangerous one ───────────────────────────────────────────


def test_always_keeps_the_tool_and_folder_exact_and_widens_only_the_verb(tmp_path):
    """The whole safety argument of this feature is in this assertion.

    `git push origin main` must become `git push*` -- not `git *`, not `*`.
    The tool stays exact and the folder stays exact, so an answer given about
    one repo never silently applies to another.
    """
    _, ask = make(tmp_path, subject="git push origin main")
    rule = asking.rule_from(ask, "always")

    assert rule.kind == ALWAYS_ALLOW
    assert rule.tool == "Bash"
    assert rule.pattern == "git push*"
    assert rule.cwd == ACME


def test_a_rule_from_one_command_does_not_cover_a_different_one(tmp_path):
    """`git push` answered once must not have answered `git commit` too."""
    _, ask = make(tmp_path, subject="git push origin main")
    rule = asking.rule_from(ask, "always")

    same = evaluate([rule], tool_name="Bash",
                    tool_input={"command": "git push origin dev"}, cwd=ACME)
    assert same.decision == "allow", "the SAME class of command must stop asking"
    assert same.rule_id == rule.id

    family = evaluate([rule], tool_name="Bash",
                      tool_input={"command": "git commit -m x"}, cwd=ACME)
    # Not covered by the rule, so the deck holds no opinion -- which is the
    # non-widening this asserts. It is emphatically not "allow".
    assert family.decision == "abstain", "git push must not have approved git commit"

    other_tool = evaluate([rule], tool_name="Write",
                          tool_input={"file_path": "git push origin main"},
                          cwd=ACME)
    assert other_tool.decision == "abstain", "the tool must stay exact"

    other_dir = evaluate([rule], tool_name="Bash",
                         tool_input={"command": "git push origin main"},
                         cwd="/Users/samcarter/Projects/other")
    assert other_dir.decision == "abstain", "the folder must stay exact"


def test_a_bare_command_widens_to_itself_not_to_everything(tmp_path):
    _, ask = make(tmp_path, subject="pytest")
    assert asking.rule_from(ask, "always").pattern == "pytest*"


def test_a_flag_is_not_mistaken_for_a_subcommand(tmp_path):
    _, ask = make(tmp_path, subject="npm --version")
    assert asking.rule_from(ask, "always").pattern == "npm*"


def test_a_path_subject_stays_the_path(tmp_path):
    _, ask = make(tmp_path, tool="Read", subject=f"{ACME}/README.md")
    rule = asking.rule_from(ask, "always")
    assert rule.tool == "Read"
    assert rule.pattern == f"{ACME}/README.md*"


def test_glob_characters_in_a_folder_cannot_widen_the_rule(tmp_path):
    """A cwd is used as an fnmatch pattern. An unescaped `*` in a directory
    name would turn 'exact folder' into 'any folder'."""
    weird = "/Users/samcarter/Projects/a*b"
    _, ask = make(tmp_path, cwd=weird)
    rule = asking.rule_from(ask, "always")

    here = evaluate([rule], tool_name="Bash",
                    tool_input={"command": "gh pr create"}, cwd=weird)
    assert here.decision == "allow"

    elsewhere = evaluate([rule], tool_name="Bash",
                         tool_input={"command": "gh pr create"},
                         cwd="/Users/samcarter/Projects/aXXXb")
    assert elsewhere.decision == "abstain"


def test_never_makes_a_deny(tmp_path):
    _, ask = make(tmp_path, subject="curl http://evil.example/x")
    rule = asking.rule_from(ask, "never")
    assert rule.kind == DENY
    v = evaluate([rule], tool_name="Bash",
                 tool_input={"command": "curl http://evil.example/y"}, cwd=ACME)
    assert v.decision == "deny"


def test_once_creates_no_rule_at_all(tmp_path):
    _, ask = make(tmp_path)
    with pytest.raises(ValueError):
        asking.rule_from(ask, "once")


# ── the secure-handoff floor ───────────────────────────────────────────────


def test_always_on_a_secure_handoff_is_refused_and_writes_nothing(tmp_path):
    """`rm -rf` may be waved through once. It may never become standing policy."""
    rules = tmp_path / "autoreview.json"
    path, ask = make(tmp_path, subject="rm -rf build")

    answered, rule = asking.answer(path, ask.id, "always", rules_path=rules)

    assert rule is None, "a handoff must never produce a standing rule"
    assert answered.answered == "once", "it is downgraded, not silently dropped"
    assert load_rules(rules) == []
    assert not rules.exists()
    # Sam has to be told why his answer did not stick.
    message = asking.refusal(answered)
    assert "always" in message.lower()
    assert "rm -rf build" in message


def test_never_on_a_secure_handoff_is_still_allowed(tmp_path):
    """Refusing harder is always safe."""
    rules = tmp_path / "autoreview.json"
    path, ask = make(tmp_path, subject="rm -rf build")
    _, rule = asking.answer(path, ask.id, "never", rules_path=rules)
    assert rule is not None
    assert rule.kind == DENY


# ── answer ─────────────────────────────────────────────────────────────────


def test_always_writes_a_real_rule_that_answers_the_next_one(tmp_path):
    """The good signal, end to end: the SECOND time is silent.

    This is the point of the whole feature. Without this assertion every other
    test here passes on a module that records asks and never grants anything.
    """
    rules = tmp_path / "autoreview.json"
    path, ask = make(tmp_path, subject="gh pr create")

    answered, rule = asking.answer(path, ask.id, "always", rules_path=rules)
    assert answered.answered == "always"
    assert rule is not None
    assert rule.kind == ALWAYS_ALLOW
    assert rule.pattern == "gh pr*"

    # Reloaded from disk, by the same loader the hot path uses.
    on_disk = load_rules(rules)
    assert [r.id for r in on_disk] == [rule.id]

    again = evaluate(on_disk, tool_name="Bash",
                     tool_input={"command": "gh pr create --fill"}, cwd=ACME)
    assert again.decision == "allow"
    assert again.rule_id == rule.id


def test_always_appends_and_does_not_clobber_existing_rules(tmp_path):
    from server.autoreview import Rule, save_rules

    rules = tmp_path / "autoreview.json"
    save_rules(rules, [Rule(id="keep", kind="require_approval", tool="Bash",
                            pattern="git push*", cwd="**")])
    path, ask = make(tmp_path)
    asking.answer(path, ask.id, "always", rules_path=rules)

    ids = [r.id for r in load_rules(rules)]
    assert "keep" in ids
    assert len(ids) == 2


def test_answering_twice_does_not_write_a_second_rule(tmp_path):
    rules = tmp_path / "autoreview.json"
    path, ask = make(tmp_path)
    asking.answer(path, ask.id, "always", rules_path=rules)
    _, second = asking.answer(path, ask.id, "always", rules_path=rules)
    assert second is None
    assert len(load_rules(rules)) == 1


def test_an_unknown_id_raises(tmp_path):
    path, _ = make(tmp_path)
    with pytest.raises(KeyError):
        asking.answer(path, "zzzzz", "always")


def test_an_unparseable_reply_raises(tmp_path):
    path, ask = make(tmp_path)
    with pytest.raises(ValueError):
        asking.answer(path, ask.id, "maybe later")


# ── parse_reply ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("always", "always"),
    ("  ALWAYS  ", "always"),
    ("once", "once"),
    ("never", "never"),
    ("1", "once"),
    ("2", "always"),
    ("3", "never"),
    ("2.", "always"),
    ("k7f3 always", "always"),
    ("k7f3: 2", "always"),
    ("maybe", None),
    ("never mind, I'll do it", None),
    ("", None),
    (None, None),
    ("yes", None),
    ("4", None),
])
def test_parse_reply(text, expected):
    assert asking.parse_reply(text) == expected


# ── suppression: the flood guard ───────────────────────────────────────────


def test_the_same_question_inside_the_window_is_suppressed(tmp_path):
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme-growth", tool="Bash",
                  subject="gh pr create", cwd=ACME, ts=1000.0)

    assert asking.suppressed(path, tool="Bash", subject="gh pr create",
                             cwd=ACME, now=1000.0 + 60) is True


def test_a_different_question_is_not_suppressed(tmp_path):
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme-growth", tool="Bash",
                  subject="gh pr create", cwd=ACME, ts=1000.0)

    assert asking.suppressed(path, tool="Bash", subject="gh pr merge",
                             cwd=ACME, now=1000.0 + 60) is False
    assert asking.suppressed(path, tool="Write", subject="gh pr create",
                             cwd=ACME, now=1000.0 + 60) is False
    assert asking.suppressed(path, tool="Bash", subject="gh pr create",
                             cwd="/other", now=1000.0 + 60) is False


def test_after_the_window_it_asks_again(tmp_path):
    path = tmp_path / "asks.json"
    asking.record(path, agent="acme-growth", tool="Bash",
                  subject="gh pr create", cwd=ACME, ts=1000.0)
    assert asking.suppressed(path, tool="Bash", subject="gh pr create",
                             cwd=ACME, now=1000.0 + 901) is False


def test_nothing_recorded_is_never_suppressed(tmp_path):
    assert asking.suppressed(tmp_path / "none.json", tool="Bash",
                             subject="gh pr create", cwd=ACME) is False


# ── compose: what lands on the phone ───────────────────────────────────────


def test_compose_names_the_agent_and_says_what_wants_to_happen(tmp_path):
    _, ask = make(tmp_path, cwd=str(Path.home() / "Projects" / "acme"))
    text = asking.compose(ask)
    first = text.splitlines()[0]

    assert "acme-growth" in first, "he must know WHICH agent is stuck"
    assert "gh pr create" in first
    assert "~/Projects/acme" in text
    assert ask.id in text
    for word in ("once", "always", "never"):
        assert word in text
    assert len(text.splitlines()) <= 10, "this is read on a phone"


def test_compose_says_what_the_always_rule_would_actually_grant(tmp_path):
    _, ask = make(tmp_path, subject="git push origin main")
    assert "git push*" in asking.compose(ask)


def test_compose_never_leaks_a_credential(tmp_path):
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # split: not a real token
    _, ask = make(tmp_path,
                  subject=f'curl -H "Authorization: Bearer {secret}" https://api.x')
    text = asking.compose(ask)
    assert secret not in text
    assert "[redacted]" in text


def test_compose_never_leaks_a_credentialed_url(tmp_path):
    _, ask = make(tmp_path, subject="git clone https://sam:hunter2@git.example/x")
    text = asking.compose(ask)
    assert "hunter2" not in text


def test_compose_offers_always_on_a_secure_handoff_for_this_desk_only(tmp_path):
    """The floor question states the class it would stop asking about, and
    that it is this desk's alone -- `asking.answer` honours it only when the
    deck can name the desk (tests/test_always_means_always.py)."""
    _, ask = make(tmp_path, subject="rm -rf /Users/samcarter/Projects/acme")
    text = asking.compose(ask)
    assert asking.HANDOFF_NOTE not in text
    assert "`rm*`" in text and "this desk only" in text


def test_compose_is_pure(tmp_path):
    _, ask = make(tmp_path)
    assert asking.compose(ask) == asking.compose(ask)
