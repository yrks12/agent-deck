"""A desk never told it can hire fabricates the outcome instead.

Measured on the owner's own machine. A manager hired through the interview
door was told to hire two people. `YOS_HIRE` appears **zero** times in the
~9KB of prompt it was given -- read off `--append-system-prompt` straight from
`ps`, plus the interview seed and `mandate.md` -- and **zero** times in its
whole transcript. It reached for Claude Code's own in-window `Agent` tool
instead, and then told the owner:

    Both hired and running -- repo scan and machine scan, read-only, no fetch
    and no sudo on either.

At that moment `roster.json` had no new desks, the board showed `reports: []`,
and `events.jsonl` carried neither a `hire` nor a `hire_refused` line. The
claim was unfalsifiable from his board and the attempt left no trace at all.

The subsystem is not broken: the moment the marker was pasted into the chat by
hand, both desks existed in 11 seconds with the right `reports_to` and `by` in
the ledger. `hire`, `onboard`, `harvest`, the depth cap, the live cap and the
`hire_refused` logging all work. They were **unreachable from any prompt on
this machine**, and that is the fault these tests pin.

`onboard.interview_prompt` documents `YOS_DESK`; `hire.brief` documented no
marker at all. They already share `REGISTER` verbatim for exactly this reason,
so the hiring text is shared the same way and these tests hold the two doors
together.

Every test asserts the GOOD signal -- the marker present in a real brief, the
example line actually parsing, the caps named with the numbers they will hit --
never the absence of an error.
"""

from server import hire, onboard
from server.roster import Desk


def desk(name="acme", reports_to="cos", label="product"):
    return Desk(
        name=name,
        cwd="/tmp",
        engine="claude",
        mission="You own the reading product.",
        label=label,
        charter="You own the reading product itself, end to end.",
        reports_to=reports_to,
    )


def _marker_lines(text: str) -> list[str]:
    """Every `YOS_HIRE` line in `text`, as `onboard.parse_lines` sees them."""
    return [line for line in text.splitlines()
            if line.strip().startswith(onboard.HIRE_PREFIX)]


def test_a_hired_desk_is_told_the_marker_exists():
    """THE test. A brief with no `YOS_HIRE` in it is a desk that cannot hire
    and does not know it -- so it invents having done so."""
    text = hire.brief(desk())
    assert onboard.HIRE_PREFIX in text, (
        "the brief never names the hiring marker; the desk has no way to hire "
        "and will use something that leaves no record")


def test_the_root_desk_is_told_too():
    """The chief of staff is the desk that hires most, and it takes the other
    branch of `brief` -- the one written for a desk reporting to the owner."""
    assert onboard.HIRE_PREFIX in hire.brief(desk(name="cos", reports_to=None))


def test_the_example_in_the_brief_is_a_line_the_parser_actually_reads():
    """The example is real, not illustrative -- the same bar
    `onboard.interview_prompt` holds its `YOS_DESK` example to. If the two
    drift, a desk copies a line the harvester silently drops."""
    lines = _marker_lines(hire.brief(desk()))
    assert lines, "no YOS_HIRE example line to copy"

    patches, hires = onboard.parse_lines("\n".join(lines))
    assert not patches
    assert len(hires) == 1, f"the example did not parse as one hire: {lines}"
    request = hires[0]
    for field in ("name", "label", "charter", "cwd"):
        assert getattr(request, field), f"{field} missing from the example"


def test_the_brief_names_the_caps_the_desk_will_hit():
    """A desk told it can hire and then silently refused is the same defect in
    different clothes. Both caps, by number, and the slug the refusal carries
    so the desk can recognise its own."""
    text = hire.brief(desk())
    assert str(hire.MAX_LIVE) in text, "the live cap is not in the brief"
    assert str(hire.MAX_DEPTH) in text, "the depth cap is not in the brief"
    assert "too_many_live" in text
    assert "too_deep" in text


def test_the_brief_says_a_hire_is_a_desk_and_not_yet_a_person():
    """Both new desks in the live run came up OFFLINE and stayed there, and the
    manager, asked for a status, did the work itself. `OFFLINE` is the word on
    the board, so it is the word the desk is told to expect."""
    assert "OFFLINE" in hire.brief(desk())


def test_both_doors_carry_the_same_hiring_text():
    """Verbatim, like `REGISTER`. Two wordings of the same rules is a second
    thing to keep in step, and the interview door is where a brand-new hire
    reads its instructions before it has a desk to be briefed with."""
    assert hire.HIRING in hire.brief(desk())
    assert hire.HIRING in onboard.interview_prompt("watch my repos", "a mandate")
