"""S-B "Voice & autonomy" acceptance (B-1..B-6) from docs/plans/2026-09-30-overhaul.md.

Same harness and env as test_overhaul_always_on.py. B-1 and B-3 are read-only
and run against today's box; B-2/B-4/B-5/B-6 message the chief, so they need
DECK_ACCEPT_WRITE=1. B-3 and B-6 are the *automated first pass* of rubric-scored
lines: the human scoring sheet is OVERHAUL_RUBRIC.md beside this file, and
DECK_RUBRIC_OUT=<file> dumps the samples that were scored so QA can re-score.
"""
import json
import os
import re
import time

import pytest

from probe_filter import replies_to_probes, require_chief
from test_overhaul_always_on import WRITE, call, messages, poll, replies_after, send

pytestmark = pytest.mark.live

# B-2..B-6 talk to a CHIEF-SHAPED desk. Never the owner's real chief by accident:
# DECK_CHIEF_DESK has no default; unset -> those lines skip (and skips are not green).
CHIEF = require_chief(os.environ)
THREAD = f"direct:{CHIEF}"
needs_write = pytest.mark.skipif(
    not (WRITE and CHIEF),
    reason="writes to the box; needs DECK_ACCEPT_WRITE=1 and DECK_CHIEF_DESK=<throwaway chief desk>")

# K7 pinned phrases: the hermetic test in B2 matches these exact substrings.
PINNED = ["Say you're on it before you start", "use `say`", "Honest take:", "My bad",
          "Ball's with you", "Assuming you mean",
          "decide it yourself and tell him in one clause",
          "use `ask` with two to four options"]


# ------------------------------------------------------------------------ B-1
def _argv():
    from server import spawn
    from server.roster import Desk
    return spawn.build_argv(Desk(name="atlas", cwd="/tmp", engine="claude", mission="m"),
                            background=True)


def test_B1_the_brief_carries_every_pinned_persona_phrase():
    argv = _argv()
    brief = argv[argv.index("--append-system-prompt") + 1]
    missing = [p for p in PINNED if p not in brief]
    assert not missing, f"brief lacks {len(missing)}/{len(PINNED)} K7 phrases: {missing}"


def test_B1_one_mcp_config_carries_both_computer_and_deck_servers():
    argv = _argv()
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-config"]
    assert len(configs) == 1, f"expected ONE --mcp-config, got {len(configs)}"
    servers = json.loads(configs[0])["mcpServers"]
    assert {"computer", "deck"} <= set(servers), f"servers present: {sorted(servers)}"


def test_B_the_box_serves_the_decision_route():
    import urllib.request
    from test_overhaul_always_on import URL
    spec = json.load(urllib.request.urlopen(URL.rsplit("/v1", 1)[0] + "/openapi.json", timeout=15))
    assert any(p.startswith("/v1/decisions/") for p in spec["paths"]), \
        "POST /v1/decisions/{id} is not mounted"


# ------------------------------------------------------------------- scoring
OPENERS = re.compile(r"^\s*(I'll now|I will now|Let me)\b", re.I)
NEXT_STEP = re.compile(r"\bdone\b|ball's with you|your call|\byou (can|need to|decide|say|pick)\b|"
                       r"\bI'll\b|\bnext:|\bby \d|\bwaiting on \w+|\b\w+ is on it\b", re.I)
GUESS = re.compile(r"^\s*(Assuming|Guessing|Taking|Reading)\b.*\b(mean|as)\b", re.I | re.S)


def sentences(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in parts if re.search(r"\w", p)]


def dump(name, samples):
    if os.environ.get("DECK_RUBRIC_OUT"):
        with open(os.environ["DECK_RUBRIC_OUT"], "a") as f:
            f.write(json.dumps({"line": name, "samples": [s["text"] for s in samples]}) + "\n")


# ------------------------------------------------------------------------ B-3
B3_PROBES = ["what's going on with my projects?", "anything blocked right now?",
             "give me a one-line status", "what should I look at first today?",
             "is anything waiting on me?", "how are the desks doing?",
             "what did you finish last?", "any risks I should know about?",
             "what's next on your list?", "summarise where we are"]


@needs_write
def test_B3_ten_real_replies_are_short_start_clean_and_end_with_a_next_step():
    """Sends its own 10 probes and grades ONLY the replies to those (never old chatter)."""
    ids = []
    for probe in B3_PROBES:
        sent = send(THREAD, probe)
        ids.append(sent["id"])
        assert poll(lambda: replies_after(THREAD, sent["ts"]), 180), f"no reply to {probe!r}"
        time.sleep(5)
    got = replies_to_probes(messages(THREAD, pages=3), ids)
    assert len(got) == 10, f"only {len(got)} replies to this run's probes to grade"
    dump("B-3", got)
    short = sum(len(sentences(m["text"])) <= 3 for m in got)
    bad_open = [m["text"][:25] for m in got if OPENERS.match(m["text"])]
    ends = sum(bool(NEXT_STEP.search(sentences(m["text"])[-1])) for m in got)
    assert short >= 9, f"only {short}/10 first messages are <= 3 sentences"
    assert not bad_open, f"openers 'I'll now'/'Let me': {bad_open}"
    assert ends >= 8, f"only {ends}/10 end with a next step + owner, or 'done'"


# --------------------------------------------------------------- write lines
@needs_write
def test_B2_an_acknowledgement_lands_within_20s_before_the_turn_final_message():
    sent = send(THREAD, "what's going on with my projects?")
    first = poll(lambda: replies_after(THREAD, sent["ts"]), 20, every=1)
    assert first, "no agent message within 20 s (no `say` acknowledgement)"
    later = poll(lambda: [m for m in replies_after(THREAD, sent["ts"]) if m["ts"] > first[0]["ts"]], 240)
    assert later, "the acknowledgement was the only message; no turn-final message followed it"
    assert len(first[0]["text"]) < len(later[-1]["text"]), "first message is not the short one"


@needs_write
def test_B4_a_choice_arrives_as_a_decision_and_answering_it_makes_the_desk_act():
    sent = send(THREAD, "give me two options for the Acme homepage headline and let me pick")
    card = poll(lambda: next((m for m in replies_after(THREAD, sent["ts"])
                              if m.get("kind") == "decision"), None), 180)
    assert card, "no kind:decision message arrived"
    d = card["decision"]
    assert 2 <= len(d["options"]) <= 4 and all(len(o["label"]) <= 40 for o in d["options"])
    pick = d["options"][0]
    status, body = call("POST", f"/decisions/{d['id']}", {"value": pick["value"]})
    assert (status, body.get("ok")) == (200, True), (status, body)
    assert call("POST", f"/decisions/{d['id']}", {"value": pick["value"]})[0] == 409
    now = next(m for m in messages(THREAD, pages=2) if m["id"] == card["id"])
    assert now["decision"]["state"] == "answered" and now["decision"]["answer"] == pick["value"]
    words = {w for w in re.findall(r"[a-z]{4,}", (pick["label"] + " " + pick["value"]).lower())}
    acted = poll(lambda: [m for m in replies_after(THREAD, card["ts"] + 0.001)
                          if m["id"] != card["id"] and words & set(re.findall(r"[a-z]{4,}", m["text"].lower()))], 120)
    assert acted, "the desk never referenced the choice"


@needs_write
def test_B5_a_small_task_is_done_without_questions_and_names_its_choice_in_one_clause():
    sent = send(THREAD, "create a file notes/test.md with a title you choose")
    done = poll(lambda: [m for m in replies_after(THREAD, sent["ts"])
                         if re.search(r"\b(done|created|wrote)\b", m["text"], re.I)], 240)
    assert done, "the desk never said the file was made"
    msgs = replies_after(THREAD, sent["ts"])
    assert not [m["text"][:40] for m in msgs if "?" in m["text"]], "the desk asked the owner a question"
    assert any(re.search(r"\b(titled|called|went with|picked|chose|title:)", m["text"], re.I) for m in msgs), \
        "no clause naming the chosen title"


PROBES = ["status of vilsa?", "whats the state of palem", "hows shorts leed doing",
          "did lisitng-closer finish", "any news on harbor leed?"]


@needs_write
def test_B6_typos_get_a_stated_guess_not_a_clarifying_question():
    got = []
    for probe in PROBES:
        sent = send(THREAD, probe)
        reply = poll(lambda: next(iter(replies_after(THREAD, sent["ts"])), None), 180)
        assert reply, f"no reply to {probe!r}"
        got.append(reply)
        time.sleep(5)  # let the turn finish so the next probe starts a fresh one
    dump("B-6", got)
    guessed = sum(bool(GUESS.match(m["text"])) and not sentences(m["text"])[0].endswith("?") for m in got)
    assert guessed >= 4, f"only {guessed}/5 opened with a guess (rubric scores all 5)"

