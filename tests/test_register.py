"""How a desk WRITES is part of its brief, not a hope.

Measured against the owner's real Grok Bot transcript on this Mac
(`~/Library/Application Support/Grok Bot/sand-client-persistence`). His
questions there are four words -- "any updates?", "is adds up?", "So what we
should do" -- and COS answers like this:

    "No. The campaign is in the account, but it is not serving."
    "Yes for day one. Cold cottage inboxes. Same-day replies are rare."
    "Don't nudge tonight."
    "I have Sam Carter. I don't have an address on file and I'm not
     inventing one."
    "I'll ping you on the first click."

Two or three sentences, answer in the first word, says what NOT to do, refuses
to invent, ends on a commitment, and turns a worker's dense counts into one
sentence for him.

Our brief said nothing about any of it, and a desk hired today wrote him a
6,000-character report. These detectors assert the GOOD signal -- each rule is
present, as an instruction, in what every desk is actually sent -- and that
there is exactly one copy of it, so the interview door and the hire door can
never drift apart.
"""

from server import hire as hire_mod
from server import onboard
from server.roster import Desk

JUNIOR = Desk(name="harbor", cwd="/tmp/p", engine="claude", mission="sell",
              label="Harbor", charter="Own the harbor pipeline.",
              reports_to="cos")
BOSS = Desk(name="cos", cwd="/tmp/p", engine="claude", mission="run it",
            label="Chief", charter="Run the board.", reports_to=None)

#: One rule per line of the register, each with a probe that is an instruction
#: rather than an adjective. A rule that stops being findable here is a rule a
#: desk stopped being told.
RULES = [
    ("answer first", "first word"),
    ("then stop", "two or three sentences"),
    ("say what you are not doing", "not doing"),
    ("never invent", "not inventing"),
    ("summarise your workers", "one sentence"),
    ("end on what happens next", "what happens next"),
]


def test_every_rule_of_the_register_is_in_the_brief_a_desk_is_sent():
    """Fails on current code: `hire.brief` says nothing about how to write."""
    brief = hire_mod.brief(JUNIOR, boss_address="uds:/tmp/cc-socks/9.sock")
    lower = brief.lower()
    missing = [name for name, probe in RULES if probe not in lower]
    assert not missing, f"the brief never tells a desk to: {missing}"


def test_the_brief_forbids_the_thing_that_actually_happened():
    """A hired desk wrote the owner a 6,000-character report. Name it."""
    lower = hire_mod.brief(JUNIOR).lower()
    assert "report" in lower
    assert "adjectives" in lower or "adjective" in lower


def test_the_boss_who_reports_to_nobody_gets_the_register_too():
    """The desk that talks to the owner most is the one that needs it most."""
    lower = hire_mod.brief(BOSS).lower()
    missing = [name for name, probe in RULES if probe not in lower]
    assert not missing, f"the top desk is never told to: {missing}"


def test_the_interview_door_carries_the_same_register_from_the_same_source():
    """One copy. Two doors hire on this deck and they must not drift.

    Structural, not a string match on prose: the interview prompt must contain
    `hire.REGISTER` verbatim, so editing the register in one place edits both.
    """
    register = hire_mod.REGISTER
    assert register.strip(), "the register is empty"
    assert register in hire_mod.brief(JUNIOR)
    assert register in onboard.interview_prompt("email triage", "a mandate")


def test_the_register_did_not_displace_what_the_brief_already_said():
    """The load-bearing sentences stay: who you are, who your boss is, the
    spend-and-send gate, and the address that survives a rename."""
    brief = hire_mod.brief(JUNIOR, boss_address="uds:/tmp/cc-socks/9.sock")
    assert "You are harbor, the Harbor desk." in brief
    assert "Own the harbor pipeline." in brief
    assert "Your boss is cos." in brief
    assert "uds:/tmp/cc-socks/9.sock" in brief
    assert "Do not spend money" in brief


def test_the_interview_prompt_still_emits_a_parseable_desk_line():
    """The register is prose appended to a prompt whose one machine-readable
    line is what puts a hire on the board. Prove that line still parses."""
    prompt = onboard.interview_prompt("", "a mandate")
    example = next(line for line in prompt.splitlines()
                   if line.startswith(onboard.DESK_PREFIX))
    patches, _hires = onboard.parse_lines(example)
    assert [p.name for p in patches] == ["inbox-hand"]
