"""C3 -- a call carries a short-lived OpenAI Realtime client secret.

The OpenAI HTTP call is faked. The real API key must never appear in any
response, deck line or log; with no key (or a failed mint) the call still
starts, without `realtime`, with `realtime_error` -- never a 500.
"""

import json

import pytest

from server import realtime
from tests.test_calls import AUTH, DESK, VOICE, Rig, start

REAL_KEY = "sk-proj-REAL-KEY-MUST-NEVER-LEAK"


@pytest.fixture
def fake(monkeypatch):
    seen = []

    def post(url, key, body):
        seen.append((url, key, body))
        return {"value": "ek_abc123secret", "expires_at": 1234567890,
                "session": {}}

    monkeypatch.setattr(realtime, "_post", post)
    monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)
    return seen


def test_start_returns_the_realtime_block(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch, desk={**DESK, "voice": VOICE})
    body = start(rig).json()
    rt = body["realtime"]
    assert "realtime_error" not in body
    assert rt["client_secret"] == "ek_abc123secret"
    assert rt["expires_at"] == 1234567890
    assert rt["ws_url"] == f"wss://api.openai.com/v1/realtime?model={rt['model']}"
    assert rt["voice"] in realtime.VOICES
    (tool,) = rt["tools"]
    assert tool["type"] == "function" and tool["name"] == "send_to_desk"
    assert tool["parameters"]["required"] == ["text"]
    assert body["call_id"].startswith("call_")


def test_instructions_are_built_from_the_roster(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    text = start(rig).json()["realtime"]["instructions"]
    assert "Atlas" in text and "Chief" in text
    assert "send_to_desk" in text
    assert "[Atlas update]" in text
    assert "never invent" in text.lower()


def test_mint_request_uses_the_key_and_the_same_config(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    rt = start(rig).json()["realtime"]
    (url, key, body), = fake
    assert url.endswith("/v1/realtime/client_secrets") and key == REAL_KEY
    s = body["session"]
    assert s["type"] == "realtime" and s["model"] == rt["model"]
    assert s["instructions"] == rt["instructions"]
    assert s["audio"]["output"]["voice"] == rt["voice"]
    assert s["tools"] == rt["tools"]


def test_the_real_key_never_leaves_the_box(tmp_path, monkeypatch, fake, capfd):
    rig = Rig(tmp_path, monkeypatch)
    r = start(rig)
    assert REAL_KEY not in r.text
    assert REAL_KEY not in (tmp_path / "messages.jsonl").read_text()
    assert REAL_KEY not in (tmp_path / "calls.json").read_text()
    out = capfd.readouterr()
    assert REAL_KEY not in out.out + out.err


def test_a_failed_mint_leaks_nothing_and_is_not_a_500(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)

    def boom(url, key, body):
        raise RuntimeError(f"401 for key {key}")

    monkeypatch.setattr(realtime, "_post", boom)
    rig = Rig(tmp_path, monkeypatch)
    r = start(rig)
    assert r.status_code == 201
    j = r.json()
    assert "realtime" not in j and j["realtime_error"]
    assert REAL_KEY not in r.text
    out = capfd.readouterr()
    assert REAL_KEY not in out.out + out.err


def test_no_key_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(realtime, "_post", lambda *a: pytest.fail("no key, no call"))
    rig = Rig(tmp_path, monkeypatch)
    r = start(rig)
    assert r.status_code == 201
    j = r.json()
    assert "realtime" not in j and "OPENAI_API_KEY" in j["realtime_error"]
    assert j["voice"] == {"id": "", "rate": 1.0}


def test_malformed_mint_reply_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)
    monkeypatch.setattr(realtime, "_post", lambda *a: {"nope": 1})
    j = start(Rig(tmp_path, monkeypatch)).json()
    assert "realtime" not in j and j["realtime_error"]


def test_voice_is_stable_per_desk_and_valid():
    a = realtime.pick_voice("atlas", None)
    assert a == realtime.pick_voice("atlas", None) and a in realtime.VOICES
    assert realtime.pick_voice("atlas", {"id": "x", "openai": "sage"}) == "sage"
    assert realtime.pick_voice("atlas", {"openai": "bogus"}) in realtime.VOICES


def test_the_desk_is_told_its_voice_is_relayed(tmp_path, monkeypatch, fake):
    rig = Rig(tmp_path, monkeypatch)
    start(rig)
    (line,) = rig.deck_lines()
    assert "relayed live" in line["text"]
    assert "`say`" in line["text"] and "progress" in line["text"]
