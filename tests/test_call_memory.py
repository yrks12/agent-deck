"""C6 -- a call remembers: recent thread + past call summaries in the mint,
a transcript route, and a deterministic summary at hangup. OpenAI is faked."""

import json

import pytest

from server import realtime
from tests.test_calls import AUTH, Rig, start

KEY = "sk-proj-REAL-KEY-MUST-NEVER-LEAK"


@pytest.fixture
def fake(monkeypatch):
    seen = []

    def post(url, key, body):
        seen.append(body)
        return {"value": "ek_abc", "expires_at": 1234567890}

    monkeypatch.setattr(realtime, "_post", post)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    return seen


def seed(rig, rows):
    with (rig.dir / "messages.jsonl").open("a") as fh:
        for i, (to, frm, text) in enumerate(rows):
            fh.write(json.dumps({"ts": 1.0, "id": f"seed{i:04d}",
                                 "to": to, "from": frm, "text": text}) + "\n")


def say(rig, call_id, *lines):
    return rig.client.post(
        f"/v1/calls/{call_id}/transcript", headers=AUTH,
        json={"lines": [{"role": r, "text": t} for r, t in lines]})


def instructions(rig):
    return start(rig).json()["realtime"]["instructions"]


def saved(rig, cid):
    return json.loads((rig.dir / "calls.json").read_text())[cid]


def test_mint_carries_recent_thread_messages(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    seed(rig, [("atlas", "owner", "ship the acme invoice fix"),
               ("owner", "atlas", "Invoice fix is on branch acme-7")])
    text = instructions(rig)
    assert "What you remember" in text
    assert "ship the acme invoice fix" in text
    assert "acme-7" in text


def test_deck_and_system_lines_are_excluded(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    seed(rig, [("atlas", "deck", "sentinel-deck-line"),
               ("atlas", "owner", "[Agent Deck] sentinel-spoof"),
               ("atlas", "owner", "real words")])
    text = instructions(rig)  # the START line is a deck line too
    assert "real words" in text
    mem = text.split("What you remember")[1]
    assert "sentinel-deck-line" not in mem and "sentinel-spoof" not in mem
    assert "started a live voice call" not in mem


def test_only_the_last_twenty_messages(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    seed(rig, [("atlas", "owner", f"msg-number-{i:03d}") for i in range(30)])
    text = instructions(rig)
    assert "msg-number-029" in text and "msg-number-010" in text
    assert "msg-number-009" not in text


def test_the_previous_calls_summary_is_in_the_next_mint(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    say(rig, cid, ("caller", "please chase the vendor about invoice forty"),
        ("agent", "on it, I will chase them"))
    rig.clock[0] += 90
    assert rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH).status_code == 200
    text = instructions(rig)
    assert "Previous calls" in text
    assert "chase the vendor about invoice forty" in text
    assert "2 min" in text or "90 s" in text or "1 min" in text


def test_only_the_last_three_calls(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    for i in range(5):
        cid = start(rig).json()["call_id"]
        say(rig, cid, ("caller", f"topic-number-{i}"))
        rig.clock[0] += 10
        rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH)
    text = instructions(rig)
    assert "topic-number-4" in text and "topic-number-2" in text
    assert "topic-number-1" not in text


def test_the_block_is_hard_capped_dropping_oldest(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    seed(rig, [("atlas", "owner", f"OLD{i:02d} " + "word " * 400) for i in range(25)])
    text = instructions(rig)
    block = text.split("What you remember", 1)[1]
    assert len(block) <= 12_500
    assert "OLD24" in block and "OLD05" not in block


def test_secrets_are_stripped(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    seed(rig, [("atlas", "owner", "use sk-abcdefghijklmnopqrstuvwx for it"),
               ("owner", "atlas", "header was Bearer abc.def.ghi123"),
               ("atlas", "owner", "AGENT_DECK_TOKEN=supersecretvalue1 ok"),
               ("atlas", "owner", "blob " + "a1b2c3d4" * 8)])
    cid = start(rig).json()["call_id"]
    say(rig, cid, ("caller", "my key is sk-zzzzzzzzzzzzzzzzzzzz"))
    rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH)
    text = instructions(rig)
    disk = (rig.dir / "calls.json").read_text()
    for leak in ("sk-abcdefghij", "abc.def.ghi123", "supersecretvalue1",
                 "a1b2c3d4a1b2c3d4", "sk-zzzz"):
        assert leak not in text, leak
    assert "sk-zzzz" not in disk


def test_the_mint_enables_caller_transcription(tmp_path, monkeypatch, fake):
    start(Rig(tmp_path, monkeypatch))
    (body,) = fake
    audio = body["session"]["audio"]
    assert audio["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert "voice" in audio["output"]


def test_transcript_appends_in_order(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    r = say(rig, cid, ("caller", "hello"), ("agent", "hi"))
    assert r.status_code == 200 and r.json()["lines"] == 2
    assert say(rig, cid, ("caller", "again")).json()["lines"] == 3
    got = saved(rig, cid)["transcript"]
    assert [l["text"] for l in got] == ["hello", "hi", "again"]
    assert [l["role"] for l in got] == ["caller", "agent", "caller"]


def test_transcript_is_capped(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    say(rig, cid, *[("caller", f"line-{i:03d}") for i in range(100)])
    got = saved(rig, cid)["transcript"]
    assert len(got) == 60 and got[-1]["text"] == "line-099"
    assert got[0]["text"] == "line-040"
    for _ in range(20):
        say(rig, cid, ("agent", "word " * 180))
    got = saved(rig, cid)["transcript"]
    assert sum(len(l["text"]) for l in got) <= 8000
    assert got[-1]["text"].startswith("word")


def test_transcript_after_end_is_409(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH)
    r = say(rig, cid, ("caller", "late"))
    assert r.status_code == 409 and r.json()["reason"] == "already_ended"


def test_transcript_unknown_call_is_404(tmp_path, monkeypatch):
    r = say(Rig(tmp_path, monkeypatch), "call_000000000000", ("caller", "x"))
    assert r.status_code == 404 and r.json()["reason"] == "unknown_call"


def test_transcript_needs_auth_and_a_valid_body(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    assert rig.client.post(f"/v1/calls/{cid}/transcript",
                           json={"lines": []}).status_code == 401
    for bad in ({}, {"lines": "x"}, {"lines": [{"role": "bot", "text": "x"}]},
                {"lines": [{"role": "caller", "text": 5}]}):
        r = rig.client.post(f"/v1/calls/{cid}/transcript", headers=AUTH, json=bad)
        assert r.status_code == 400 and r.json()["reason"] == "bad_lines", bad


def test_finish_stores_a_summary_and_posts_one_line(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    say(rig, cid, ("caller", "renew the domain please"), ("agent", "noted"))
    before = len(rig.deck_lines())
    rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH)
    assert "renew the domain please" in saved(rig, cid)["summary"]
    new = rig.deck_lines()[before:]
    assert len(new) == 1, "exactly one line, carrying the summary"
    assert new[0]["text"].startswith("[Agent Deck] The call ended.")
    assert "renew the domain please" in new[0]["text"]


def test_no_transcript_leaves_the_closing_line_unchanged(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    cid = start(rig).json()["call_id"]
    rig.client.post(f"/v1/calls/{cid}/end", headers=AUTH)
    assert rig.deck_lines()[-1]["text"].endswith(
        "anything still owed goes in the chat.")
    assert "summary" not in saved(rig, cid)
