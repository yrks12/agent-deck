# Calls — a live voice call with a desk

**Contract:** K6 in `docs/plans/2026-09-30-overhaul.md`.
**Code:** `server/calls.py` (own `APIRouter`, mounted in `server/app.py`).
**Tested by:** `tests/test_calls.py`; live: `tests/live/test_overhaul_calls.py`.

A call is the owner's Mac talking to a desk over the thread it already has.
The deck owns only the two edges; every utterance in between is an ordinary
message (K2).

## Start

`POST /v1/calls` `{"agent":"atlas"}` -> `201`

```json
{"call_id":"call_1a2b3c4d5e6f","thread_id":"direct:atlas",
 "voice":{"id":"com.apple.voice.premium.en-GB.Malcolm","rate":1.1}}
```

Plus, when the box has `OPENAI_API_KEY`, the desk's live voice (C3):

```json
"realtime":{"ws_url":"wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
 "client_secret":"ek_…","expires_at":1790000000,"model":"gpt-realtime-2.1",
 "voice":"marin","instructions":"You are Atlas, …",
 "tools":[{"type":"function","name":"send_to_desk","description":"…",
  "parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}
```

The app opens `ws_url` with subprotocol `openai-insecure-api-key.<client_secret>`
(the ephemeral secret; the real key never leaves the box), sends `session.update`
carrying `instructions`/`tools` if it wants (they are already baked into the
secret), and on a `send_to_desk` call posts `text` to the desk's thread, then
returns the function output. Desk `say` lines come back as
`[<Desk> update] …` conversation items for the model to read out. If the key is
missing or the mint fails the call still starts (201) with no `realtime` and a
`realtime_error` string; the app falls back to local speech. Model via
`OPENAI_REALTIME_MODEL`; voice = roster `voice.openai` if set, else a stable
pick per desk name. Code: `server/realtime.py`.

`voice` is the desk's roster `voice`; a desk with none gets `{"id":"","rate":1.0}`
and the client picks a voice from the name. `400 missing_agent`, `404 unknown_agent`.

The desk is told with one office line, sender **`deck`** (a system voice, never
the owner):

> [Agent Deck] Sam started a live voice call. Answer in one or two short spoken
> sentences — no markdown, no paths, no code. Your voice is relayed live by
> a voice model that cannot do work itself; use `say` for progress and findings
> so the caller hears them, and for anything that will take more than a few
> seconds.

A desk with a live socket gets it at once (and the queued record is acked); a
sleeping desk is woken with reason `call` (`docs/wake.md`) off the request
thread, and the wake seed carries the line.

## Utterances

`POST /v1/threads/direct:atlas/messages` with `channel:"voice"` and this
`call_id`. The desk reads `"<utterance>\n(said on a live call)"`.

## End

`POST /v1/calls/{call_id}/end` -> `200`
`{"ok":true,"call_id":…,"duration_ms":12345,"turns":2}`

Posts the closing deck line ("The call ended. This channel is closed from now
on, so anything still owed goes in the chat.") and records duration and
`turns` = voice-channel messages carrying that `call_id`. `404 unknown_call`,
`409 already_ended` (no second closing line).

## Memory and transcript (C6)

The mint's `instructions` end with a "What you remember" block (hard cap
12,000 chars, oldest dropped first, secrets redacted): the desk's last 20
direct messages with Sam (deck/`[Agent Deck]` lines excluded) and its last 3
calls (when, how long, summary). The session also enables caller transcription
(`audio.input.transcription = {"model":"gpt-4o-mini-transcribe"}`), so the app
receives `conversation.item.input_audio_transcription.completed`.

`POST /v1/calls/{call_id}/transcript` `{"lines":[{"role":"caller"|"agent","text":"…"}]}`
-> `200 {"ok":true,"call_id":…,"lines":<stored count>}`. Appends as utterances
complete (batch or one at a time). Kept: newest 60 lines / 8,000 chars; each
line clipped to 1,000 and redacted. `400 bad_lines`, `404 unknown_call`,
`409 already_ended`.

At `/end` a deterministic summary (first request + last lines, no LLM) is
stored as `summary` and appended to the ONE closing deck line ("… Call summary:
…"); a call with no transcript posts the closing line unchanged.

## State

`<bus>/calls.json` (`~/.claude/agent-bus/calls.json`), written atomically:
`{call_id: {agent, started_at, ended_at, duration_ms?, turns?, transcript?, summary?}}`. An open call
survives a deck restart. Same bearer token as the rest of `/v1`.

## Spoken replies and voice messages (not a call)

Both run on the box so the OpenAI key never leaves it (`server/voice.py`).

`POST /v1/speech` `{"text":"…","agent":"atlas"}` -> `200 audio/mpeg` — the
words in the desk's **call voice** (`realtime.pick_voice`, the same one the
live call speaks with), via `gpt-4o-mini-tts` (`OPENAI_TTS_MODEL`). Headers:
`X-Deck-Voice` (the voice used), `X-Deck-Cache: hit|miss`. Cached at
`<bus>/tts/<sha256(model,voice,words)>.mp3`, newest 400 kept. An unknown
`agent` still gets a stable voice. `400 empty_text`, `502 speech_failed`,
`503 speech_unavailable` (no `OPENAI_API_KEY`). The apps speak a reply this way
only when **Spoken replies** is on (a per-device switch in the thread header,
on by default) and he used his voice; if the deck cannot speak it they fall
back to the device's own voice and note `tts.fallback` in the voice trace.

`POST /v1/threads/direct:{agent}/voice-notes` — raw recording as the body
(`Content-Type: audio/mp4`, at most 20 MB) -> `201` — the same body as
`POST /v1/threads/{id}/messages` plus `"transcript"`. Transcribed here with
`gpt-4o-transcribe` (`OPENAI_TRANSCRIBE_MODEL`; Hebrew and English), then sent
as an ordinary **owner** message, `channel: "text"` — delivered, or the desk
woken, exactly like typing. The desk reads the transcript followed by
"(sent as a voice message; this is its transcript)"; the app never sees that
line. The message carries `"voice_note": {"id":"vn_…","url":"/v1/voice-notes/vn_…"}`
(absent on every other message). No call is started. `400 empty_audio`,
`400 direct_only`, `404 unknown_agent`, `413 too_long`, `422 nothing_heard`
(nothing is queued), `502 transcription_failed` (nothing is queued).

`GET /v1/voice-notes/{id}` -> `200 audio/mp4`, the recording, for his bubble's
play button. Stored at `<bus>/voice_notes/<id>.m4a`. `404 unknown_voice_note`.
