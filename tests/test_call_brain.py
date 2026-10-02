"""A call is as smart as chat: the voice is a thin layer over the desk.

Owner, 2026-10-01: "On the chat, Atlas performs way better than in a call.
We need to match call to chat." MEASURED on the box that day (calls.json +
messages.jsonl, 16 Atlas calls with transcripts): of 87 things he said, 29
reached the desk; the other 58 were answered by the Realtime model alone from
a one-paragraph persona and the recent thread -- it told him "I can't look
things up", "I can't see or change the payment link from here", "I can't log
into your Stripe" while the desk could. 42 of 56 requests that did reach the
desk were the voice model's third-person rewrite ("<owner> asked ... please
provide ..."), not his words.

So the minted session now carries what chat Atlas has (who it is, what it
can do, the team memory index, the recent thread) and rules that send every
substantive turn to the desk in his own words, keep only small talk, and
answer a decision card by voice. OpenAI is faked.
"""

import pytest

from server import calls, capabilities, learning, realtime
from tests.test_calls import Rig, start

KEY = "sk-proj-REAL-KEY-MUST-NEVER-LEAK"

INVENTORY = capabilities.Inventory(
    connected=["claude.ai Gmail"], deck_servers=["computer", "deck"],
    skills=["sentinel-skill-xyz"], clis=["gh"], web=True)


@pytest.fixture
def fake(monkeypatch):
    seen = []

    def post(url, key, body):
        seen.append(body)
        return {"value": "ek_abc", "expires_at": 1234567890}

    monkeypatch.setattr(realtime, "_post", post)
    monkeypatch.setattr(capabilities, "current", lambda desk: INVENTORY)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    return seen


def lesson(title, fact):
    learning.save_lesson("atlas", title, fact, "because", "do it",
                         shared=True)


def minted(rig):
    return start(rig).json()["realtime"]


def test_the_voice_knows_what_the_desk_can_do(tmp_path, monkeypatch, fake):
    text = minted(Rig(tmp_path, monkeypatch))["instructions"]
    # The same identity and detected inventory the chat desk is briefed on.
    assert "chief of staff" in text
    assert "Gmail" in text and "sentinel-skill-xyz" in text
    assert "Chromium browser" in text
    # ...and it is told those are done THROUGH the desk, not denied.
    assert "never say you can't" in text.lower()


def test_the_voice_knows_the_team_memory_index(tmp_path, monkeypatch, fake):
    lesson("Sign-ins go through the passkey card",
           "Google sign-ins cannot be copied from his Chrome.")
    text = minted(Rig(tmp_path, monkeypatch))["instructions"]
    assert "Team memory" in text
    assert "Sign-ins go through the passkey card" in text
    assert "Google sign-ins cannot be copied" in text


def test_every_substantive_turn_goes_to_the_desk_in_his_words(
        tmp_path, monkeypatch, fake):
    rt = minted(Rig(tmp_path, monkeypatch))
    text = rt["instructions"].lower()
    assert "every question about work" in text
    assert "his exact words" in text
    assert "small talk" in text
    send = next(t for t in rt["tools"] if t["name"] == "send_to_desk")
    said = send["parameters"]["properties"]["text"]["description"].lower()
    assert "exact words" in said and "do not rephrase" in said


def test_a_decision_card_is_answered_by_voice(tmp_path, monkeypatch, fake):
    rt = minted(Rig(tmp_path, monkeypatch))
    tool = next(t for t in rt["tools"] if t["name"] == "answer_card")
    assert set(tool["parameters"]["required"]) == {"card_id", "choice"}
    assert "asks you to pick]" in rt["instructions"]
    assert "answer_card" in rt["instructions"]
    (body,) = fake
    assert body["session"]["tools"] == rt["tools"]


def test_the_brief_parts_stay_within_a_budget(tmp_path, monkeypatch, fake):
    for i in range(60):
        lesson(f"Lesson number {i} about something long enough",
               "x " * 200)
    text = minted(Rig(tmp_path, monkeypatch))["instructions"]
    assert len(text) <= realtime.INSTRUCTIONS_MAX


def test_a_capability_probe_that_fails_still_mints(tmp_path, monkeypatch, fake):
    def boom(desk):
        raise RuntimeError("claude mcp list hung")

    monkeypatch.setattr(capabilities, "current", boom)
    text = minted(Rig(tmp_path, monkeypatch))["instructions"]
    assert "send_to_desk" in text
    assert "chief of staff" in text, "only the probed part is lost"


def test_the_desk_answers_a_call_like_chat(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    start(rig)
    (line,) = rig.deck_lines()
    assert "exactly as you would in chat" in line["text"]
    assert calls.START_LINE == line["text"]
