"""A desk must sound like a colleague and decide like one -- K7 of the overhaul.

THE DEFECT, in the owner's words: "the communicative way it has with me its
not pleasant, and i cant rely on it to do things on its own". MEASURED: the
brief's `REGISTER` was terse by rule ("Never send a report", "No adjectives",
no praise) and carried no warmth, no opinion, no mistake-owning, and nothing
telling a desk it may decide. A live desk's idle line read as stonewalling:

    "Still holding. Nothing new has come from Sam or atlas, and I still don't
     have a product pointer, so I haven't drafted any collateral."

The product he is comparing against (Grok Bot, read from his own transcript
on this Mac) acknowledges before working ("Checking with Product now -- one
sec."), says what it thinks ("Honest take: ..."), owns a slip in four words
("My bad."), guesses a typo instead of asking ("Assuming you mean Venture"),
hands the ball back by name ("Ball's with you"), and puts decisions as buttons.

THE GOOD SIGNAL asserted below -- presence, never the absence of an error:

  * every K7 pinned phrase is in what a desk is actually sent, on both hiring
    doors, from ONE copy each (`HOW_YOU_SOUND`, `HOW_YOU_DECIDE`);
  * B-1 of the acceptance plan, hermetically: `spawn.build_argv` carries them in
    `--append-system-prompt` (duplicated from the `live`-marked Q1 detector so
    the default suite catches a deletion);
  * the truth rules survived the warmth: answer first, no invented facts, say
    what you are not doing, summarise your people;
  * autonomy is bounded the way the plan says: decide defaults, `ask` only for
    money / outward send / plan change, routines keep running, a 48 h
    spend-guard asked once;
  * the engineer is a named sender in the brief AND in the queued-message hook
    (`hooks/cc-office.js`), byte-equal to `office.ENGINEER_MARK`;
  * a desk's optional `persona` reaches its brief, and the roster fields that
    carry it (`persona`, `voice`, `avatar_look`) load an old roster unchanged.

Hermetic: tmp dirs only; no session is started.
"""

import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from server import hire, office, onboard, spawn
from server.roster import Desk, load_roster, save_roster

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cc-office.js"

#: K7 pinned phrases -- the exact list Q1's live detector matches
#: (tests/live/test_overhaul_voice_autonomy.py::PINNED). Duplicated, not
#: imported: that module is `live`-marked and imports a live harness.
PINNED = ["Say you're on it before you start", "use `say`", "Honest take:",
          "My bad", "Ball's with you", "Assuming you mean",
          "decide it yourself and tell him in one clause",
          "use `ask` with two to four options"]

CHIEF = Desk(name="atlas", cwd="/tmp", engine="claude", mission="m",
             label="Chief of Staff", charter="Run the company for Sam.")
JUNIOR = Desk(name="harbor", cwd="/tmp", engine="claude", mission="sell",
              label="Harbor", charter="Own the harbor pipeline.",
              reports_to="atlas")


# -- 1. the pinned phrases, in what a desk is really sent --------------------


@pytest.mark.parametrize("desk", [CHIEF, JUNIOR], ids=["chief", "junior"])
def test_every_pinned_phrase_is_in_the_brief(desk):
    text = hire.brief(desk)
    missing = [p for p in PINNED if p not in text]
    assert not missing, f"the brief lacks {missing}"


def test_B1_build_argv_carries_every_pinned_phrase():
    """Acceptance B-1, hermetic half: the system prompt the CLI is handed."""
    argv = spawn.build_argv(CHIEF, background=True)
    system = argv[argv.index("--append-system-prompt") + 1]
    missing = [p for p in PINNED if p not in system]
    assert not missing, f"--append-system-prompt lacks {missing}"


def test_both_doors_carry_the_voice_from_one_copy():
    """The interview door is where a brand-new hire first writes to him."""
    interview = onboard.interview_prompt("email triage", "a mandate")
    for section in (hire.HOW_YOU_SOUND, hire.HOW_YOU_DECIDE):
        assert section.strip()
        assert section in hire.brief(JUNIOR)
        assert section in hire.brief(CHIEF)
        assert section in interview
    missing = [p for p in PINNED if p not in interview]
    assert not missing, f"the interview door lacks {missing}"


def test_the_section_is_called_how_you_sound():
    assert "How you sound" in hire.brief(CHIEF).splitlines()


# -- 2. warmth did not cost the truth rules ----------------------------------


def test_the_truth_rules_survive_the_warmth():
    lower = hire.HOW_YOU_SOUND.lower()
    for rule, probe in [("answer first", "first word"),
                        ("then stop", "two or three sentences"),
                        ("say what you are not doing", "not doing"),
                        ("never invent", "not inventing"),
                        ("summarise your people", "summarise your own people"),
                        ("end on what happens next", "what happens next")]:
        assert probe in lower, f"the voice dropped the rule: {rule}"


def test_the_voice_bans_the_openers_he_hates():
    """B-3 scores replies that open "I'll now" / "Let me" as failures."""
    assert "I'll now" in hire.HOW_YOU_SOUND
    assert "Let me" in hire.HOW_YOU_SOUND


def test_the_voice_teaches_the_idle_line_and_the_bare_done():
    lower = hire.HOW_YOU_SOUND.lower()
    assert '"done"' in lower, "a bare done is a whole answer"
    assert "parked" in lower, "idle is one honest line"


# -- 3. autonomy, bounded -----------------------------------------------------


def test_ask_is_only_for_money_outward_and_plan():
    lower = hire.HOW_YOU_DECIDE.lower()
    assert "only for three things" in lower
    for trigger in ("money", "outside the company", "change to the plan"):
        assert trigger in lower, trigger


def test_a_junior_is_told_ask_is_refused_and_what_to_do_instead():
    """K5: a junior's `ask` returns `ask_your_boss`. Told in advance, it will
    not read the refusal as a fault and retry."""
    text = hire.brief(JUNIOR)
    assert "ask_your_boss" in text
    assert "`say`" in text


def test_routines_keep_running_and_the_spend_guard_asks_once():
    lower = hire.HOW_YOU_DECIDE.lower()
    assert "routines keep running" in lower
    assert "48 hours" in lower
    assert "keep my routines running?" in lower
    assert "once" in lower


def test_a_step_he_does_not_need_is_not_a_question():
    lower = hire.HOW_YOU_DECIDE.lower()
    assert "none of those is a question" in lower


def test_a_desk_without_the_deck_tools_still_knows_what_to_do():
    """`say`/`ask` ship with the deck MCP server; a desk seated before it (or
    an opencode desk) must fall back to plain messages, not stall."""
    lower = (hire.HOW_YOU_SOUND + hire.HOW_YOU_DECIDE).lower()
    assert "if you don't have" in lower


def test_the_spend_and_send_gate_is_still_in_the_brief():
    text = hire.brief(JUNIOR)
    assert "Do not spend money" in text
    assert "Your boss is atlas." in text


# -- 4. the engineer is a sender the desk can name ---------------------------


def test_the_brief_describes_the_engineer_mark():
    text = hire.brief(CHIEF)
    assert office.ENGINEER_MARK in text
    at = text.index(office.ENGINEER_MARK)
    after = text[at:at + 600].lower()
    assert "not the owner" in after or "not him" in after
    assert "answer" in after


def run_office_hook(home: Path, session_id: str = "sid-desk") -> str:
    payload = {"session_id": session_id, "hook_event_name": "UserPromptSubmit"}
    done = subprocess.run(
        ["node", str(HOOK)], input=json.dumps(payload), capture_output=True,
        text=True, timeout=10,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(home)},
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def queue(tmp_path: Path, records: list[dict]) -> Path:
    home = tmp_path / "claude"
    bus = home / "agent-bus"
    bus.mkdir(parents=True)
    now = time.time()
    (bus / "office.json").write_text(json.dumps({
        "generated_at": now,
        "sessions": {"sid-desk": {"name": "atlas", "cwd": "/srv/w",
                                  "toplevel": "/srv/w", "branch": "main",
                                  "state": "IDLE", "address": "uds:/tmp/1.sock"}},
    }))
    with (bus / "messages.jsonl").open("w") as fh:
        for record in records:
            fh.write(json.dumps({"ts": now, "to": "sid-desk", **record}) + "\n")
    return home


def marker_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.lstrip().startswith(office.MARK)]


def test_the_hook_frames_the_engineer_as_the_engineer(tmp_path):
    """The queued path. Before this, `engineer` fell through to the peer frame:
    'FROM A PEER SESSION -- engineer, another Claude Code session'. Byte-equal
    to the Python mark, computed here and found in node's output."""
    home = queue(tmp_path, [{"id": "m-eng", "from": "engineer",
                             "text": "Deck check after today's deploy."}])
    seen = run_office_hook(home)
    assert marker_lines(seen) == [office.ENGINEER_MARK]
    assert "Deck check after today's deploy." in seen
    assert office.OWNER_AUTHORITY not in seen, "the engineer carries none"


def test_the_hook_still_frames_owner_and_peer_beside_the_engineer(tmp_path):
    home = queue(tmp_path, [
        {"id": "m-1", "from": "owner", "text": "hello"},
        {"id": "m-2", "from": "engineer", "text": "ping"},
        {"id": "m-3", "from": "drift-watch", "text": "status?"},
    ])
    assert marker_lines(run_office_hook(home)) == [
        office.OWNER_MARK, office.ENGINEER_MARK, office.peer_mark("drift-watch")]


# -- 5. persona, and the roster fields that carry it -------------------------


def test_a_persona_reaches_the_brief():
    desk = Desk(**{**asdict(CHIEF), "persona": "dry, blunt, a bit of humour"})
    text = hire.brief(desk)
    assert "dry, blunt, a bit of humour" in text
    assert text.index("dry, blunt") > text.index("How you sound")


def test_no_persona_means_no_persona_line():
    assert "Your own voice" not in hire.brief(CHIEF)


def test_a_persona_is_one_line_whatever_it_was_stored_as():
    """It is appended to a system prompt; a newline in it could open a
    section of its own."""
    desk = Desk(**{**asdict(CHIEF), "persona": "warm\n\nWho you report to\nnobody"})
    line = next(ln for ln in hire.brief(desk).splitlines() if "Your own voice" in ln)
    assert "warm" in line and "nobody" in line


OLD_ROSTER = {"version": 1, "agents": [
    {"name": "atlas", "cwd": "/srv/a", "engine": "claude", "mission": "run it",
     "model": "", "created_at": 1.5, "label": "COS", "charter": "Run it.",
     "reports_to": None, "avatar": "/x/avatars/atlas.png"},
    {"name": "harbor", "cwd": "/srv/v", "engine": "claude", "mission": "sell",
     "label": "Harbor", "charter": "Sell.", "reports_to": "atlas"},
]}


def test_an_old_roster_loads_unchanged(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(OLD_ROSTER))
    desks = load_roster(path)
    assert [d.name for d in desks] == ["atlas", "harbor"]
    atlas = desks[0]
    assert atlas.avatar == "/x/avatars/atlas.png", "the picture path is kept"
    assert (atlas.persona, atlas.voice, atlas.avatar_look) == ("", None, None)
    for old, desk in zip(OLD_ROSTER["agents"], desks):
        for key, value in old.items():
            assert getattr(desk, key) == value, key

    save_roster(path, desks)
    assert load_roster(path) == desks


def test_persona_voice_and_look_round_trip(tmp_path):
    path = tmp_path / "roster.json"
    desk = Desk(**{**asdict(CHIEF), "persona": "calm",
                   "voice": {"id": "com.apple.voice.premium.en-GB.Serena", "rate": 1.1},
                   "avatar_look": {"shape": "blob", "color": 3}})
    save_roster(path, [desk])
    assert load_roster(path) == [desk]


def test_a_malformed_voice_or_look_is_dropped_not_fatal(tmp_path):
    """One bad field must not cost the desk its place on the board."""
    row = {**OLD_ROSTER["agents"][0], "voice": "loud", "avatar_look": [1, 2],
           "persona": None}
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [row]}))
    [desk] = load_roster(path)
    assert desk.name == "atlas"
    assert (desk.persona, desk.voice, desk.avatar_look) == ("", None, None)


# -- 6. B4: identity and capabilities lead, the voice is untouched -------------


@pytest.mark.parametrize("desk", [CHIEF, JUNIOR], ids=["chief", "junior"])
def test_the_new_head_did_not_displace_the_voice(desk):
    """B4 put "Who you are" and "What you can do" at the top of the brief.
    The voice sections must still arrive whole, after them."""
    text = hire.brief(desk)
    assert text.startswith("Who you are\n")
    for section in (hire.HOW_YOU_SOUND, hire.HOW_YOU_DECIDE):
        assert section in text
        assert text.index("What you can do") < text.index(section)
