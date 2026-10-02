"""The harvest loop: the deck actually reads what a session says.

`server.onboard` could always parse a `YOS_DESK` / `YOS_HIRE` line and apply
it -- and nothing ever called it, because nothing read a session's output. This
is that reader, and every test here pins a way it can silently ruin the roster:

1. **Offsets survive a restart.** The daemon restarts constantly. An in-memory
   offset re-reads every transcript from byte 0 on every restart and re-applies
   every historical `YOS_HIRE` -- silently re-hiring desks the owner deleted.
2. **A torn line is not lost.** Transcripts are read while they are being
   written; a hire split across two polls has to land, once, on the second.
3. **Exactly once.** Transcripts replay the same message, so the same line seen
   twice must apply once -- and the first time it must actually apply.
4. **A session speaks only for itself.** `onboard.apply_patch` refuses a
   cross-desk patch, but only if it is handed the *true* actor. The harvester
   passing along whatever name the line claimed would disarm that guard
   completely, and nothing downstream would notice.

Hermetic: the roster, the bus ledger and every transcript live under
`tmp_path`. Nothing spawns, nothing dials out, nothing touches ~/.claude.
"""

import json

import pytest

from server import office, onboard, paths
from server.harvest import Harvester
from server.roster import Desk, load_roster, save_roster


@pytest.fixture(autouse=True)
def private_message_queue(tmp_path, monkeypatch):
    """A message queue under `tmp_path`, for every test in this file.

    The harvester sends the hiring desk a one-line receipt for each hire it
    applies or refuses, so hiring is no longer a silent write to the roster.
    Autouse rather than opt-in: `tests/conftest.py` fails any test that reaches
    the owner's live `~/.claude/agent-bus/`, and every hire here would.
    """
    monkeypatch.setattr(office, "BUS_DIR", tmp_path / "bus")
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "bus" / "messages.jsonl")

COS_CHARTER = "You run the office and you are the only desk the owner talks to."
ACME_CHARTER = "You own the Acme & Line reading product itself."


def desk(name, reports_to=None, label="", charter="", cwd="/tmp"):
    return Desk(
        name=name,
        cwd=cwd,
        engine="claude",
        mission=charter,
        label=label,
        charter=charter,
        reports_to=reports_to,
    )


@pytest.fixture
def roster_path(tmp_path):
    path = tmp_path / "roster.json"
    save_roster(
        path,
        [
            desk("cos", None, "admin", COS_CHARTER),
            desk("acme", "cos", "product", ACME_CHARTER),
        ],
    )
    return path


@pytest.fixture
def offsets_path(tmp_path):
    return tmp_path / "harvest-offsets.json"


def said(text: str) -> str:
    """One transcript record in which the session said `text`."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


def hire_line(name, *, cwd, charter="You own paid acquisition.", label="growth"):
    return "YOS_HIRE " + json.dumps(
        {"name": name, "label": label, "charter": charter,
         "description": "Runs paid acquisition.", "cwd": str(cwd)}
    )


def desk_line(**fields):
    return "YOS_DESK " + json.dumps(fields)


def session(name, transcript, *, session_id="s1", cwd="/tmp"):
    """A card as the collector builds it, plus the transcript it reads."""
    return {
        "session_id": session_id,
        "name": name,
        "cwd": cwd,
        "state": "WORKING",
        "transcript": str(transcript),
    }


def names(path):
    return [d.name for d in load_roster(path)]


def by_name(path, name):
    return next(d for d in load_roster(path) if d.name == name)


def hire_events(roster_path):
    log = roster_path.parent / "events.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text().splitlines()
        if line.strip() and json.loads(line).get("event") == "hire"
    ]


# ── 1. the offset outlives the process ─────────────────────────────────────


def test_offset_survives_a_fresh_harvester(tmp_path, roster_path, offsets_path):
    """THE restart detector. A hire already harvested must not be harvested
    again by the next Harvester -- that is how a fired desk comes back."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-growth", cwd=tmp_path)))
    sessions = [session("acme", transcript)]

    first = Harvester(roster_path, offsets_path)
    applied = first.poll(sessions)
    assert [(r["kind"], r["result"], r["name"]) for r in applied] == [
        ("hire", "hired", "acme-growth")
    ]
    assert "acme-growth" in names(roster_path)

    # The owner fires it. The transcript still carries the hire that made it.
    save_roster(roster_path, [d for d in load_roster(roster_path) if d.name != "acme-growth"])

    # The daemon restarts: a brand-new Harvester over the same offsets file.
    second = Harvester(roster_path, offsets_path)
    assert second.poll(sessions) == []
    assert "acme-growth" not in names(roster_path)

    # ...and it is alive, not merely inert: something new still lands.
    with transcript.open("a") as fh:
        fh.write(said(desk_line(label="Palmistry")))
    assert [r["result"] for r in second.poll(sessions)] == ["patched"]
    assert by_name(roster_path, "acme").label == "Palmistry"


# ── 2. a line torn across two polls ────────────────────────────────────────


def test_a_hire_torn_across_two_polls_lands_exactly_once(
    tmp_path, roster_path, offsets_path
):
    """The transcript is read while it is written, so a poll routinely lands
    mid-record. The half-written hire must be picked up on the next tick."""
    transcript = tmp_path / "s1.jsonl"
    record = said(hire_line("acme-growth", cwd=tmp_path))
    cut = record.index("YOS_HIRE") + 20  # mid-JSON, inside the hire payload
    transcript.write_text(said(desk_line(label="Palmistry")) + record[:cut])

    sessions = [session("acme", transcript)]
    harvester = Harvester(roster_path, offsets_path)

    # The complete record before it lands; the torn one waits.
    first = harvester.poll(sessions)
    assert [(r["kind"], r["result"]) for r in first] == [("desk", "patched")]
    assert by_name(roster_path, "acme").label == "Palmistry"

    with transcript.open("a") as fh:
        fh.write(record[cut:])

    second = harvester.poll(sessions)
    assert [(r["kind"], r["result"], r["name"]) for r in second] == [
        ("hire", "hired", "acme-growth")
    ]
    assert [e["name"] for e in hire_events(roster_path)] == ["acme-growth"]
    assert by_name(roster_path, "acme-growth").reports_to == "acme"


def test_a_torn_line_survives_a_restart_mid_record(tmp_path, roster_path, offsets_path):
    """The half-written record is buffered in the offsets file itself -- the
    offset stops before it -- so a restart between the two halves still lands
    the hire rather than eating it."""
    transcript = tmp_path / "s1.jsonl"
    record = said(hire_line("acme-growth", cwd=tmp_path))
    cut = record.index("YOS_HIRE") + 20
    transcript.write_text(record[:cut])
    sessions = [session("acme", transcript)]

    assert Harvester(roster_path, offsets_path).poll(sessions) == []

    with transcript.open("a") as fh:
        fh.write(record[cut:])

    revived = Harvester(roster_path, offsets_path)
    assert [r["name"] for r in revived.poll(sessions)] == ["acme-growth"]


# ── 3. exactly once ────────────────────────────────────────────────────────


def test_the_same_line_twice_applies_once(tmp_path, roster_path, offsets_path):
    """Transcripts replay: the same assistant message is logged more than once.
    Both halves are asserted -- it lands the first time, and only then."""
    transcript = tmp_path / "s1.jsonl"
    line = hire_line("acme-growth", cwd=tmp_path)
    transcript.write_text(said(line))
    sessions = [session("acme", transcript)]

    harvester = Harvester(roster_path, offsets_path)
    assert [r["result"] for r in harvester.poll(sessions)] == ["hired"]

    # A replay of the identical line, appended as fresh bytes.
    with transcript.open("a") as fh:
        fh.write(said(line))

    assert harvester.poll(sessions) == []
    assert names(roster_path).count("acme-growth") == 1
    assert [e["name"] for e in hire_events(roster_path)] == ["acme-growth"]


# ── 4. a session may only speak for itself ─────────────────────────────────


def test_a_session_may_not_patch_another_desk(tmp_path, roster_path, offsets_path):
    """THE privilege detector for this slice. `acme` says two things in one
    breath: a patch of itself (which must land, proving the harvester is
    working and the actor is real) and a patch of its boss (which must be
    refused, naming `acme` as the actor -- proving the harvester passed the
    session's own desk name and not the one the line claimed)."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        said(desk_line(label="Palmistry"))
        + said(desk_line(name="cos", charter="You now report to acme."))
    )

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [(r["result"], r["actor"]) for r in applied] == [
        ("patched", "acme"),
        ("refused", "acme"),
    ]
    assert applied[1]["reason"] == "not_yours"
    assert applied[1]["name"] == "cos"

    assert by_name(roster_path, "acme").label == "Palmistry"
    assert by_name(roster_path, "cos").charter == COS_CHARTER


def test_a_refused_hire_reports_the_caps_own_reason(tmp_path, roster_path, offsets_path):
    """A hire an agent asks for meets the same caps as one the owner types, and
    the refusal keeps the cap's slug so the board can say why."""
    save_roster(
        roster_path,
        load_roster(roster_path) + [desk("acme-growth", "acme", "growth", "Ads.")],
    )
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(hire_line("acme-ads", cwd=tmp_path)))

    applied = Harvester(roster_path, offsets_path).poll(
        [session("acme-growth", transcript)]
    )

    assert [(r["result"], r["reason"]) for r in applied] == [("refused", "too_deep")]
    assert "acme-ads" not in names(roster_path)


# ── the session with no desk ───────────────────────────────────────────────


def test_a_session_with_no_desk_speaks_for_nobody(tmp_path, roster_path, offsets_path):
    """A session that is not a desk has no actor, so nothing it says is
    applied -- and its history is consumed, not banked: the day someone hires
    it a desk, the hires it asked for last week do not all fire at once."""
    transcript = tmp_path / "s2.jsonl"
    transcript.write_text(said(hire_line("ghost", cwd=tmp_path)))
    stranger = session("wanderer", transcript, session_id="s2")

    harvester = Harvester(roster_path, offsets_path)
    assert harvester.poll([stranger]) == []
    assert "ghost" not in names(roster_path)

    save_roster(roster_path, load_roster(roster_path) + [desk("wanderer", "cos", "", "Wanders.")])
    assert harvester.poll([stranger]) == []
    assert "ghost" not in names(roster_path)

    with transcript.open("a") as fh:
        fh.write(said(desk_line(label="Wanderer")))
    assert [r["result"] for r in harvester.poll([stranger])] == ["patched"]
    assert by_name(roster_path, "wanderer").label == "Wanderer"


# ── how the transcript is found in production ──────────────────────────────


def test_the_transcript_is_found_from_cwd_and_session_id(
    tmp_path, roster_path, offsets_path, monkeypatch
):
    """A real card carries no transcript path; it is derived the same way the
    collector derives it, or the harvester reads nothing in production."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(paths, "PROJECTS_DIR", projects)
    work = tmp_path / "work"
    work.mkdir()

    transcript = paths.transcript_path(str(work), "abc-123")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(said(desk_line(label="Palmistry")))

    applied = Harvester(roster_path, offsets_path).poll(
        [{"session_id": "abc-123", "name": "acme", "cwd": str(work), "state": "WORKING"}]
    )

    assert [r["result"] for r in applied] == ["patched"]
    assert by_name(roster_path, "acme").label == "Palmistry"


# ── the channel is an LLM's stdout ─────────────────────────────────────────


def test_junk_around_a_good_line_does_not_stop_it(tmp_path, roster_path, offsets_path):
    """Missing transcripts, unparseable records, prose quoting the prefix and
    a user's own message are all ordinary traffic. The good line still lands."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        "not json at all\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": desk_line(name="cos", charter="owner said so")}]}}) + "\n"
        + said("I could print YOS_DESK {\"label\": \"Nope\"} but here is the real one:")
        + said(desk_line(label="Palmistry"))
    )

    applied = Harvester(roster_path, offsets_path).poll(
        [
            session("acme", transcript),
            session("cos", tmp_path / "missing.jsonl", session_id="s0"),
        ]
    )

    assert [(r["actor"], r["result"]) for r in applied] == [("acme", "patched")]
    assert by_name(roster_path, "acme").label == "Palmistry"


# ── 5. a marker that fails to parse is not silence ─────────────────────────
#
# The live defect: a `YOS_DESK` line cut off mid-JSON (the model dropped the
# closing brace). `onboard.parse_lines` skips it by design -- that part is
# correct, it is untrusted stdout -- but until now the skip left no trace at
# all. The agent believed it had taken the desk; the board disagreed forever;
# nothing anywhere recorded that a line had even been offered.


def test_a_truncated_marker_line_is_refused_and_logged(tmp_path, roster_path, offsets_path):
    """THE detector. A `YOS_DESK` line missing its closing brace must produce
    a refusal record -- naming the actor and the parser's own error -- both
    in `applied` and in the durable bus ledger, and must not touch the roster."""
    transcript = tmp_path / "s1.jsonl"
    truncated = (
        'YOS_DESK {"name": "stall-watch", '
        '"charter": "Taking the desk as stall-watch."'
    )  # the closing brace never came
    transcript.write_text(said("avoidance dressed as work, I say so.\n" + truncated))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert len(applied) == 1
    record = applied[0]
    assert record["result"] == "refused"
    assert record["actor"] == "acme"
    assert record["kind"] == "desk"
    assert "YOS_DESK" in record["reason"]
    assert record["reason"] != ""  # the parser's own error, not a blank slug

    ledger = [
        json.loads(l)
        for l in (roster_path.parent / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]
    refusals = [e for e in ledger if e.get("result") == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["actor"] == "acme"
    assert "YOS_DESK" in refusals[0]["reason"]

    # A refusal must not mutate anything it was refused to change.
    assert by_name(roster_path, "acme").label == "product"


def test_a_wellformed_marker_line_still_applies_with_no_refusal(
    tmp_path, roster_path, offsets_path
):
    """Sweeping the class must not cost the happy path a false positive."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(desk_line(label="Palmistry")))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [r["result"] for r in applied] == ["patched"]


def test_the_marker_word_inside_prose_is_not_a_refusal(tmp_path, roster_path, offsets_path):
    """`YOS_DESK` mentioned mid-sentence, not at the start of a physical line,
    is not an attempt to use the marker and must not be filed as a refusal."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said("I printed YOS_DESK earlier, but not this time."))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert applied == []


def test_the_interview_prompts_own_example_line_is_not_a_refusal(
    tmp_path, roster_path, offsets_path
):
    """The interview prompt embeds a real, well-formed `YOS_DESK` line as a
    sample for a new hire. If an agent echoes its own instructions back --
    quoting the prompt verbatim, sample line included -- a naive marker match
    would file a refusal every single time. It must not."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said(onboard.interview_prompt("", "Some mandate.")))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert not any(r["result"] == "refused" for r in applied)
    assert by_name(roster_path, "cos").charter == COS_CHARTER


# ── 6. a hire missing a required field is not silence either ───────────────
#
# The live defect, one layer earlier than the dropped brace. `parse_lines` does
# `if not all(fields.values()): continue` on a `YOS_HIRE`, so a line with no
# `cwd` is dropped before `harvest` ever sees it. `_refused_markers` covered
# only the case where the whole block yielded NOTHING -- so a cwd-less hire
# sharing a block with any well-formed marker vanished with no refusal, no
# ledger line, nothing. A real hire attempt was lost this way, and the agent
# went on believing it had hired someone.
#
# MEASURED, not assumed, on whether `cwd` should still be required at all:
# `onboard.apply_hire` calls `hire.hire()` with `request.cwd` and NOTHING on
# that path allocates a workspace. The allocator (`api.Surface._workspace`)
# exists only behind the HTTP interview door. So on the agent-to-agent path the
# field is still load-bearing, and the fix is to refuse loudly, not to drop it.


def test_a_hire_with_no_cwd_is_refused_and_says_which_field(
    tmp_path, roster_path, offsets_path
):
    """THE detector. A `YOS_HIRE` line whose JSON parses perfectly but omits
    `cwd` must produce a refusal naming the missing field -- even when the same
    block also carries a marker that DID apply, which is what used to hide it."""
    transcript = tmp_path / "s1.jsonl"
    lonely = "YOS_HIRE " + json.dumps(
        {"name": "growth-hand", "label": "growth", "charter": "You own ads."}
    )
    transcript.write_text(said(desk_line(label="Palmistry") + "\n" + lonely))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    # The well-formed patch still lands -- sweeping the class must not cost the
    # happy path.
    assert by_name(roster_path, "acme").label == "Palmistry"

    refusals = [r for r in applied if r["result"] == "refused"]
    assert len(refusals) == 1, applied
    assert refusals[0]["kind"] == "hire"
    assert refusals[0]["actor"] == "acme"
    assert "cwd" in refusals[0]["reason"], refusals[0]["reason"]

    ledger = [
        json.loads(line)
        for line in (roster_path.parent / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    logged = [e for e in ledger if e.get("result") == "refused"]
    assert len(logged) == 1
    assert "cwd" in logged[0]["reason"]
    assert "growth-hand" not in names(roster_path)


def test_a_hire_with_no_cwd_alone_in_its_block_is_refused_the_same_way(
    tmp_path, roster_path, offsets_path
):
    """The path that already worked must keep working, and must gain the field
    name rather than the old "well-formed but empty"."""
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(said("YOS_HIRE " + json.dumps(
        {"name": "growth-hand", "label": "growth", "charter": "You own ads."})))

    applied = Harvester(roster_path, offsets_path).poll([session("acme", transcript)])

    assert [r["result"] for r in applied] == ["refused"]
    assert "cwd" in applied[0]["reason"], applied[0]["reason"]
