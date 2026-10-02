"""Spoken replies in the call's voice, and voice messages that are not a call.

Owner: "when I chat with voice it answers in voice automatically, but I want
to switch it off, and use the normal voice like in the call" and "I want to
just record a message and not call it".

Two routes, both on the box so the OpenAI key never reaches a phone:

* `POST /v1/speech` -- a reply's words -> audio in the desk's call voice
  (`realtime.pick_voice`), cached per text on disk.
* `POST /v1/threads/{id}/voice-notes` -- a recorded message -> transcribed
  here -> an ORDINARY owner message (it wakes the desk like typing does) that
  carries the audio's id, so his bubble can play it back via
  `GET /v1/voice-notes/{id}`.

OpenAI is faked at the two module seams (`voice._speech`, `voice._transcribe`).
Every test asserts the good signal: the bytes, the record, the delivery.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, realtime
from server import voice

TOKEN = "t-secret-not-a-real-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REAL_KEY = "sk-proj-REAL-KEY-MUST-NEVER-LEAK"
DESK = {"name": "atlas", "cwd": "/tmp/p", "engine": "claude", "mission": "run",
        "label": "Chief", "charter": "Own it.", "reports_to": None,
        "voice": {"id": "", "rate": 1.0, "openai": "sage"}}
AUDIO = b"\x00\x00\x00\x18ftypM4A fake recorded audio"
MP3 = b"ID3\x04fake mp3 frames"


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
        monkeypatch.setattr(office, "BUS_DIR", tmp_path)
        (tmp_path / "messages.jsonl").write_text("")
        (tmp_path / "roster.json").write_text(
            json.dumps({"version": 1, "agents": [DESK]}))
        monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
        monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)
        self.dir = tmp_path
        self.delivered = []
        self.spoken = []
        self.heard = []
        self.transcript = "תשלח לי את הדוח, and the numbers too"

        def deliver(name, text):
            self.delivered.append((name, text))
            return False  # a sleeping desk: the deck's deliver starts the wake

        def speech(key, body):
            self.spoken.append((key, body))
            return MP3

        def transcribe(key, data, filename, mime):
            self.heard.append((key, data, filename, mime))
            return self.transcript

        monkeypatch.setattr(voice, "_speech", speech)
        monkeypatch.setattr(voice, "_transcribe", transcribe)
        app = FastAPI()
        surface = api_mod.register(app, surface=api_mod.Surface(
            snapshot=lambda: {"generated_at": 1.0, "sessions": []},
            deliver=deliver,
            roster_path=tmp_path / "roster.json",
            prefs_path=tmp_path / "prefs.json"), background=False)
        app.include_router(voice.build_router(
            surface, roster_path=lambda: tmp_path / "roster.json"))
        self.client = TestClient(app)

    def records(self):
        return [json.loads(l) for l in
                (self.dir / "messages.jsonl").read_text().splitlines() if l.strip()]

    def speak(self, text="Done. The report is in your inbox.", agent="atlas", **kw):
        return self.client.post("/v1/speech", headers=kw.get("headers", AUTH),
                                json={"text": text, "agent": agent})

    def note(self, data=AUDIO, thread="direct:atlas", headers=None):
        return self.client.post(
            f"/v1/threads/{thread}/voice-notes", content=data,
            headers={**(AUTH if headers is None else headers),
                     "Content-Type": "audio/mp4"})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# --- spoken replies: the call's voice, from the box ---------------------------

def test_speech_returns_audio_in_the_desks_call_voice(rig):
    r = rig.speak()
    assert r.status_code == 200
    assert r.content == MP3
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-deck-voice"] == "sage"
    (key, body), = rig.spoken
    assert key == REAL_KEY
    assert body["model"] == voice.TTS_MODEL
    assert body["voice"] == realtime.pick_voice("atlas", DESK["voice"]) == "sage"
    assert body["input"] == "Done. The report is in your inbox."
    assert body["response_format"] == "mp3"


def test_an_unknown_speaker_still_gets_a_stable_call_voice(rig):
    r = rig.speak(text="Asking Atlas…", agent="agent deck")
    assert r.status_code == 200 and r.content == MP3
    assert rig.spoken[0][1]["voice"] == realtime.pick_voice("agent deck", None)


def test_the_same_words_are_fetched_from_openai_once(rig):
    first, second = rig.speak(), rig.speak()
    assert first.content == second.content == MP3
    assert len(rig.spoken) == 1
    assert first.headers["x-deck-cache"] == "miss"
    assert second.headers["x-deck-cache"] == "hit"
    cached = list((rig.dir / "tts").glob("*.mp3"))
    assert len(cached) == 1 and cached[0].read_bytes() == MP3


def test_different_words_or_voice_are_not_served_from_the_cache(rig):
    rig.speak(text="One.")
    rig.speak(text="Two.")
    rig.speak(text="One.", agent="someone else")
    assert [b["input"] for _, b in rig.spoken] == ["One.", "Two.", "One."]


def test_speech_never_returns_or_logs_the_key(rig, capfd):
    r = rig.speak()
    assert REAL_KEY.encode() not in r.content
    assert REAL_KEY not in json.dumps(dict(r.headers))
    for path in (rig.dir / "tts").iterdir():
        assert REAL_KEY.encode() not in path.read_bytes()
    out = capfd.readouterr()
    assert REAL_KEY not in out.out + out.err


def test_a_failed_speech_leaks_nothing_and_says_why(rig, monkeypatch, capfd):
    def boom(key, body):
        raise RuntimeError(f"401 for key {key}")

    monkeypatch.setattr(voice, "_speech", boom)
    r = rig.speak()
    assert r.status_code == 502
    assert r.json()["reason"] == "speech_failed"
    assert REAL_KEY not in r.text
    out = capfd.readouterr()
    assert REAL_KEY not in out.out + out.err
    assert not (rig.dir / "tts").exists() or not list((rig.dir / "tts").iterdir())


def test_no_key_is_a_clear_503_not_a_crash(rig, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    r = rig.speak()
    assert r.status_code == 503
    body = r.json()
    assert body["reason"] == "speech_unavailable"
    assert "OPENAI_API_KEY" in body["detail"]
    assert rig.spoken == []


def test_speech_needs_the_token(rig):
    assert rig.speak(headers={}).status_code == 401
    assert rig.spoken == []


def test_speech_needs_words(rig):
    r = rig.speak(text="   ")
    assert r.status_code == 400 and r.json()["reason"] == "empty_text"


# --- voice messages: recorded, transcribed on the box, sent as him ------------

def test_a_voice_note_reaches_the_desk_as_his_own_message(rig):
    r = rig.note()
    assert r.status_code == 201, r.text
    body = r.json()
    msg = body["message"]
    assert msg["role"] == "owner" and msg["author"] == "owner"
    assert msg["channel"] == "text"
    assert msg["text"] == rig.transcript
    note_id = msg["voice_note"]["id"]
    assert msg["voice_note"]["url"] == f"/v1/voice-notes/{note_id}"
    assert body["transcript"] == rig.transcript

    # queued as an ordinary owner message to the desk ...
    (rec,) = [r for r in rig.records() if r.get("from") == "owner"]
    assert rec["to"] == "atlas" and rig.transcript in rec["text"]
    assert rec["voice_note"] == note_id
    # ... and handed to delivery, which is what wakes a sleeping desk.
    (name, text), = rig.delivered
    assert name == "atlas" and rig.transcript in text
    assert "voice message" in text


def test_the_recording_is_transcribed_on_the_box_with_the_key(rig):
    rig.note()
    (key, data, filename, mime), = rig.heard
    assert key == REAL_KEY and data == AUDIO
    assert filename.endswith(".m4a") and mime == "audio/mp4"


def test_his_bubble_can_play_the_recording_back(rig):
    note_id = rig.note().json()["message"]["voice_note"]["id"]
    r = rig.client.get(f"/v1/voice-notes/{note_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["content-type"] == "audio/mp4"
    assert rig.client.get(f"/v1/voice-notes/{note_id}").status_code == 401


def test_the_thread_shows_the_note_without_the_desk_framing(rig):
    rig.note()
    page = rig.client.get("/v1/threads/direct:atlas/messages", headers=AUTH).json()
    (mine,) = [m for m in page["messages"] if m["role"] == "owner"]
    assert mine["text"] == rig.transcript
    assert mine["voice_note"]["id"]


def test_a_typed_message_carries_no_voice_note(rig):
    rig.client.post("/v1/threads/direct:atlas/messages", headers=AUTH,
                    json={"text": "typed"})
    page = rig.client.get("/v1/threads/direct:atlas/messages", headers=AUTH).json()
    (mine,) = [m for m in page["messages"] if m["role"] == "owner"]
    assert "voice_note" not in mine


def test_a_failed_transcription_queues_nothing_and_leaks_nothing(rig, monkeypatch, capfd):
    def boom(key, data, filename, mime):
        raise RuntimeError(f"401 for key {key}")

    monkeypatch.setattr(voice, "_transcribe", boom)
    r = rig.note()
    assert r.status_code == 502
    assert r.json()["reason"] == "transcription_failed"
    assert REAL_KEY not in r.text
    assert rig.records() == [] and rig.delivered == []
    out = capfd.readouterr()
    assert REAL_KEY not in out.out + out.err


def test_silence_is_not_sent(rig):
    rig.transcript = "  "
    r = rig.note()
    assert r.status_code == 422
    assert r.json()["reason"] == "nothing_heard"
    assert rig.records() == [] and rig.delivered == []


def test_an_empty_upload_is_refused(rig):
    r = rig.note(data=b"")
    assert r.status_code == 400
    assert rig.heard == []


def test_a_voice_note_needs_the_token(rig):
    assert rig.note(headers={}).status_code == 401
    assert rig.heard == [] and rig.records() == []


def test_an_unknown_note_id_is_404_and_ids_cannot_walk_the_disk(rig):
    assert rig.client.get("/v1/voice-notes/nope", headers=AUTH).status_code == 404
    assert rig.client.get("/v1/voice-notes/..%2Fmessages.jsonl",
                          headers=AUTH).status_code == 404


def test_the_deck_mounts_all_three_routes():
    """The daemon's own app answers each in the deck's refusal shape, rather
    than FastAPI's bare `{"detail": "Not Found"}` for a route never mounted."""
    from server import app as app_mod
    client = TestClient(app_mod.app)
    for method, path in (("post", "/v1/speech"),
                         ("post", "/v1/threads/direct:atlas/voice-notes"),
                         ("get", "/v1/voice-notes/vn_0123456789abcdef")):
        r = (client.post(path, json={}) if method == "post" else client.get(path))
        assert r.json().get("reason"), (path, r.status_code, r.text)
