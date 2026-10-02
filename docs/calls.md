# Calls — a live voice call with a desk

**Contract:** K6 in `docs/plans/2026-09-30-overhaul.md`.
**Code:** `server/calls.py` (own `APIRouter`, mounted in `server/app.py`).
**Tested by:** `tests/test_calls.py`, `tests/test_call_brain.py`,
`macos/Tests/DeckKitTests/CallBrainTests.swift`; live: `tests/live/test_overhaul_calls.py`.

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
(the ephemeral secret; the real key never leaves the box) and sends a
`session.update` pinning only the audio formats; `instructions` and `tools`
are baked into the secret. If the key is missing or the mint fails the call
still starts (201) with no `realtime` and a `realtime_error` string; the app
falls back to local speech. Model via `OPENAI_REALTIME_MODEL`; voice = roster
`voice.openai` if set, else a stable pick per desk name. Code:
`server/realtime.py`.

### The voice is a thin layer over the desk

Owner, 2026-10-01: "On the chat, Atlas performs way better than in a call."
Measured on the box: of 87 things he said across 16 Atlas calls, 29 reached
the desk; the Realtime model answered the rest itself from a one-paragraph
persona, and denied abilities the desk has. So:

* **Who answers.** Every question about work, facts, status, numbers, the
  team or what the desk can do, and every request, goes to the desk with
  `send_to_desk` in **his exact words** (`text`). Only greetings, thanks,
  small talk and "say that again" stay with the voice. The desk answers
  exactly as it would in chat (same tools, same checks); the start line asks
  it to open each reply with one or two spoken sentences.
* **What the voice knows.** `instructions` = the turn rules, then what chat
  Atlas is briefed with first: `capabilities.identity`, the charter,
  `capabilities.render` (the detected inventory), the team memory index
  (`learning.lessons`, titles and hooks, <= 2,400 chars), then the "What you
  remember" block. Whole string <= 21,000 chars, memory cut first. A
  capability probe that fails costs only that part.
* **The ack.** If the response that called `send_to_desk` already said
  something ("On it."), the app returns the tool output and asks for nothing
  more; if it said nothing, it asks for one response whose instructions allow
  only a two-to-four word ack. The wait is never filled with the model's own
  guesses.
* **Speaking the desk.** Each new desk line while the call is up is added as
  `[<Desk> update] …` and spoken at once by a `response.create` whose
  `response.instructions` say: only what it says, condensed to three short
  sentences, in his language, add nothing. A line that lands while the voice
  is talking waits for that response to end and keeps those rules.
* **Decision cards.** A `kind: "decision"` message from the desk mid-call is
  added as `[<Desk> asks you to pick] (card dec_…) <prompt> Options: A; B.`
  and read out. His answer comes back as the `answer_card` tool
  (`{"card_id","choice"}`); the app maps a label or value to the option's
  value (or his own words when the card allows them) and posts
  `POST /v1/decisions/{id}` — the same as a tap. An answered card is not read.

Tools (both in the minted session):

```json
[{"type":"function","name":"send_to_desk","parameters":{"type":"object",
  "properties":{"text":{"type":"string"}},"required":["text"]}},
 {"type":"function","name":"answer_card","parameters":{"type":"object",
  "properties":{"card_id":{"type":"string"},"choice":{"type":"string"}},
  "required":["card_id","choice"]}}]
```

`voice` is the desk's roster `voice`; a desk with none gets `{"id":"","rate":1.0}`
and the client picks a voice from the name. `400 missing_agent`, `404 unknown_agent`.

The desk is told with one office line, sender **`deck`** (a system voice, never
the owner):

> [Agent Deck] Sam started a live voice call. Work and answer exactly as you
> would in chat -- same tools, same checks, same care. Open every reply with
> the answer in one or two plain spoken sentences (no markdown, paths or code
> in those); detail for the chat can follow. Your voice is relayed live by a
> voice model that cannot do work itself and speaks only what you write; use
> `say` for progress and findings so the caller hears them, and for anything
> that will take more than a few seconds.

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

## A desk calls him (`server/ringing.py`)

A desk rings with `mcp__deck__call_owner(reason, urgency)`. The ring passes
his settings or is refused (a refusal that is not a repeat posts the reason to
his thread). A placed ring:

* is pushed once, at once (`PushPolicy.ring`): `GET /v1/owner/alerts` carries
  a `source: "ring"` alert (kind `needs_you`, so older apps still decode the page) with `ring_id`, and ntfy posts it at priority 5 with
  `click: agentdeck://call?ring=<id>&thread=direct:<desk>`;
* is listed in `ringing` on every `GET /v1/owner/alerts` page, and on
  `GET /v1/calls/incoming`, while it rings (30 s): `{id, agent, thread_id,
  reason, urgent, state, created_at, expires_at}`;
* `POST /v1/calls/incoming/{id}/answer` -> 201, the same body as
  `POST /v1/calls` plus `ring_id` and `opening` (the reason; the app speaks it
  as the call's first line). The desk is told he picked up and not to repeat it;
* `POST /v1/calls/incoming/{id}/decline`, or 30 s with no answer: the reason is
  posted to his thread as a line from the desk, and the desk is told.
  `404 unknown_ring`, `409 not_ringing`.

Settings: `GET` / `PATCH /v1/calls/settings`, `{who: chief|any, when:
off|urgent|anytime, quiet_hours: "22:00-08:00", tz: "America/New_York",
max_per_day: 3, call_me_now: bool}` (plus read-only `ring_seconds`). Public
default `when: off`. Quiet hours ring only urgent calls; "Call me now" lets any
allowed call ring for an hour (or one answered call) and tells the chief to
call. State: `<bus>/call_owner.json`, `<bus>/rings.json`.

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
