# Agent Deck `/v1` — the client API

**Date:** 2026-09-01 · **Status:** contract of record · **Module:** `server/api.py`

This is what a native macOS / iOS client talks to. It is versioned and separate
from the deck's own `/api/*` on purpose: the board's endpoints change when the
board changes, and an app in the App Store cannot.

Everything below is real output from the running module, not a sketch. If the
Python and this document disagree, the tests in `tests/test_client_api.py` are
the tiebreak.

---

## 1. Conventions that hold everywhere

| Rule | Detail |
|---|---|
| Base path | `/v1`, on the same origin as the deck (`http://127.0.0.1:7788` today). |
| Content type | `application/json; charset=utf-8`. SSE is `text/event-stream`. |
| Timestamps | **Epoch seconds as a JSON float**, always. Never ms, never ISO-8601, never a string. Fields: `ts`, `last_ts`, `last_activity_at`, `created_at`, `generated_at`. |
| Missing time | `null`, never `0` and never `NaN`. |
| Strict JSON | No `NaN`, no `Infinity`, no `-Infinity`, ever. Non-finite floats are serialised as `null`. Parse with a strict parser; if you ever see one, that is a server bug and a test failure. |
| Errors | Every refusal is `{"ok": false, "reason": "<slug>", "detail": "<prose>"}`. **Branch on `reason`**, never on `detail` or on the HTTP status alone. |
| Unknown fields | Additive. Ignore fields you do not know; new ones will appear without a version bump. Removals and renames will not. |

### Error reasons you must handle

| Status | `reason` | Means |
|---|---|---|
| 503 | `auth_not_configured` | The server has no token set. `/v1` is closed. Not retryable by the client. |
| 401 | `unauthorized` | Missing or wrong bearer token. Response carries `WWW-Authenticate: Bearer`. |
| 400 | `bad_cursor` | A `since`/`before` value this API did not issue. |
| 400 | `bad_sender` | A send whose `as` is not `"sam"` or `"engineer"` (§6.4). |
| 400 | `bad_channel` | A send whose `channel` is not `"text"` or `"voice"`. |
| 400 | `missing_call_id` | A `channel: "voice"` send with no `call_id`. |
| 400 | `empty_text` | A send with no body. |
| 400 | `bad_reply_to` | A send whose `reply_to` is neither a message id nor an object with one. |
| 404 | `unknown_reply_to` | A send whose `reply_to` names a message that thread does not hold. |
| 404 | `unknown_agent` | No such agent on the roster or on the board. |
| 404 | `unknown_thread` | No such thread, or a malformed thread id. |
| 404 | `unknown_message` | `up_to` names a message that is not in that agent's threads. |
| 409 | `thread_is_read_only` | You tried to post into a peer (agent↔agent) thread. |
| 409 | `reports_to_is_not_a_setting` | You tried to change the org chart through the settings panel. |
| 409 | `not_a_desk` | You tried to set `label`/`charter` on a live session that has no roster desk. |
| 500 | `queue_failed` | The message could not be written to the office queue. |
| 400 | `missing_field` | A field the route cannot proceed without was empty. `detail` names it. |
| 400 | `unknown_engine` | An engine nothing on this machine can start. |
| 400 | `bad_reply` | An approval reply that is not `once` / `always` / `never`. |
| 400 | `bad_cron` | A cron spec or timezone that can never fire. `detail` names the field. |
| 404 | `unknown_ask` | No such approval. |
| 404 | `unknown_routine` | No such routine. |
| 409 | `name_taken` | A desk with that name already exists. |
| 409 | `no_such_boss` | `reports_to` names a desk that is not on the roster. |
| 409 | `too_deep` | The org is already three levels deep under that boss. |
| 409 | `too_many_live` | Under 1200 MB of RAM free for one more seated desk (ceiling: sixteen seated). An asleep desk holds no seat. A desk frees one with `mcp__deck__retire_desk`. |
| 409 | `no_such_cwd` | `cwd` is not a directory on this machine. |
| 409 | `has_reports` | You tried to fire a desk that still has people under it. |
| 409 | `already_answered` | That approval was settled; your reply changed nothing. |
| 409 | `expired` | That approval timed out. It granted nothing, and it cannot be answered now. |
| 409 | `always_not_available` | `always` on a credential, a payment or something irreversible. Offer `once`. |
| 409 | `next_run_at_is_computed` | You sent a routine's next-run. The server works it out from the trigger. |
| 409 | `osascript_failed` | The Terminal window could not be opened. |
| 503 | `docker_unavailable` | Docker is not running on the Mac, so no desk has a computer. Nothing to retry until it is started. |
| 504 | `computer_not_responding` | Docker took longer than 20 s to answer. The desk's machine is wedged, not merely busy. |
| 409 | `computer_not_running` | That desk has no computer up. Offer to start one; do not poll. |
| 409 | `no_frame` | The computer is up and the capture came back empty. The display is broken, not idle — say so rather than showing a blank panel. |
| 409 | `input_refused` | The gesture was well formed and the machine would not take it. |
| 400 | `bad_input` | The gesture itself is wrong: an unknown action, a click outside the screen, or a keysym that is really an option. `detail` says which. Also: a `terminal` call with no command. |
| 400 | `bad_path` | The `path` (or `cwd`) you sent is not a path inside the container: not absolute, or with a NUL byte in it. Fix the request; it will never succeed. |
| 404 | `no_such_path` | Nothing is at that path in the desk's computer. **Not** the same as an empty directory, which is a 200 with `entries: []`. |
| 409 | `not_a_directory` | You listed something that is a file. Read it instead. |
| 409 | `not_a_file` | You read something that is a directory (or a socket, or a device). List it instead. |
| 415 | `not_text` | That file is binary — NUL bytes, or not UTF-8. Nothing is returned rather than a screenful of U+FFFD. Offer a download, not an error. |
| 409 | `list_failed` | The path is a directory and listing it still failed — permissions, usually. `detail` carries what the container said. |
| 409 | `read_failed` | The path is a regular file and reading it still failed. Same shape as `list_failed`. |

**`blocked.reason` is a different vocabulary.** The five slugs in §3.1
(`lock_busy`, `unexpected_shape`, `write_failed`, `not_deck_workspace`,
`engine_not_covered`) never appear as an HTTP `reason` — they are carried
*inside* a 200 response, on a desk that exists. Do not route them through your
error handling; two of them are not errors at all.

**The refusal slugs from §11 are not interchangeable.** `name_taken`,
`too_deep`, `too_many_live`, `no_such_boss` and `has_reports` each need their
own sentence in the UI. "It didn't work" leaves the person holding the phone
with nothing to do next, which is the only failure mode that matters here.

---

## 2. Auth

**Every `/v1` route requires a bearer token.**

```
Authorization: Bearer <token>
```

The token is read from the environment variable **`AGENT_DECK_TOKEN`** on the
server. Comparison is constant-time.

**Two kinds of token are accepted on every `/v1` route**, with identical
behaviour: the master `AGENT_DECK_TOKEN`, and a per-device token (`adt_…`)
minted by pairing (§19). Wrong guesses are rate limited per client address
(§19.3).

**It fails closed.** If `AGENT_DECK_TOKEN` is unset or empty, `/v1` answers
**503** to *everything*, including a request with a correct-looking header.
There is no mode in which an unconfigured server serves the roster.

```console
$ curl -s -o /dev/null -w '%{http_code}\n' localhost:7788/v1/agents
401
```

```json
// 503, with AGENT_DECK_TOKEN unset
{
  "ok": false,
  "reason": "auth_not_configured",
  "detail": "AGENT_DECK_TOKEN is unset; /v1 is closed until it is set"
}
```

```json
// 401, with a token set but not presented
{"ok": false, "reason": "unauthorized", "detail": "bearer token missing or wrong"}
```

### How a phone is supposed to reach this — read this before you deploy

**The daemon binds `127.0.0.1` and this module does not change that.** A phone
cannot reach `127.0.0.1` on the Mac, and the fix is *not* to rebind to
`0.0.0.0`. The sanctioned routes, in order of preference:

1. **A private mesh** — Tailscale or equivalent. The Mac and the phone get
   stable private addresses; nothing is on the public internet.
2. **An SSH tunnel** — `ssh -L 7788:127.0.0.1:7788 <mac>` from a machine the
   phone already trusts, or an on-device SSH client.

**Never a public port, never a port-forward on the router.** The bearer token
is a second lock, not the first one: this API can read every message every
agent on the machine has sent and can inject text into a live session. The
network boundary is the control that matters; the token exists so that a
mistake in the network boundary is not immediately fatal.

Rotate the master token by changing `AGENT_DECK_TOKEN` and restarting the
daemon. A paired device has its own token, revoked on its own — see §19.

---

## 3. `GET /v1/agents` — the sidebar

**A desk's name can change exactly once, and only the agent itself can change
it.** A desk hired through the interview door (§11.1) starts under a generated
placeholder and replaces it with the name it picks for itself; nothing else on
this API can rename a desk, and `PATCH /v1/agents/{name}` still cannot. When it
happens the server carries the desk's thread, its read cursor, its unread count
and its org edge across, announces it as `agent_renamed` on the stream (§8),
**and `thread_id` moves from `direct:<placeholder>` to `direct:<new name>`** —
the old id keeps resolving to the same thread, so a request already in flight
gets the conversation rather than a 404. Treat `name` as stable everywhere else.

The one endpoint called on every app foreground. It is designed to be cheap:
it does no work proportional to message history and reads no transcripts.

**Request**

```http
GET /v1/agents HTTP/1.1
Authorization: Bearer <token>
```

**Response 200** — verbatim, for a roster of two desks where `hemingway` is
offline, `chief` has a session, and there is one agent-to-agent message and one
message from the owner:

```json
{
  "agents": [
    {
      "name": "chief",
      "label": "Negotiator",
      "section": "Work",
      "avatar": "chief.png",
      "state": "WORKING",
      "unread": 1,
      "last_activity_at": 1788222702.087623,
      "preview": "You: status please",
      "thread_id": "direct:chief",
      "pinned": true,
      "notifications": true,
      "desk": true,
      "project": "acme",
      "session_id": "sid-chief",
      "boss": null,
      "reports": ["hemingway"]
    },
    {
      "name": "hemingway",
      "label": "Researcher",
      "description": "Researches deals and drafts the counter-offers; never sends.",
      "section": "Personal",
      "avatar": null,
      "state": "OFFLINE",
      "unread": 1,
      "last_activity_at": 1756000050.0,
      "preview": "Message from chief: Draft the counter-offer, keep it under 200 words.",
      "thread_id": "direct:hemingway",
      "pinned": false,
      "notifications": true,
      "desk": true,
      "project": "",
      "session_id": null,
      "boss": "chief",
      "reports": []
    }
  ],
  "generated_at": 1788222702.097551
}
```

### Fields

| Field | Type | Notes |
|---|---|---|
| `name` | string | The identity, used in every other path. Unique. Changes only when an interviewed agent names itself — see the note at the top of this section and `agent_renamed` in §8. |
| `label` | string | The **title chip** (`Negotiator`, `Researcher`, `Designer`, `Email`, `Travel Scout`). May be `""`. |
| `description` | string | **What this desk is for**, in one or two plain sentences for the owner ("Runs the shop's Instagram: posts, replies and the nightly round."). Show it under the name in the desk's header and profile. `""` when nobody has written one — show nothing, never the charter. One line, at most 280 characters. Written by the hiring desk (required on `YOS_HIRE`), the desk itself and the chief of staff (`set_my_look`), and the owner (`PATCH`, §5). A change arrives as a same-name `agent_renamed` (§8). |
| `section` | string | The user-defined group heading. Defaults to `"Work"` — see §9. |
| `avatar` | string \| null | Opaque; whatever the client wrote via `PATCH`. **The server does not host images.** `null` means draw initials. |
| `avatar_look` | object \| null | The drawn character (K6): `{"shape": "blob"\|"hexagon"\|"wedge"\|"tablet"\|"pebble"\|"teardrop"\|"cloud"\|"squircle", "color": 0-11}`. `null` = derive it from the name. Set by `PATCH` with an `avatar` **object** (§5). |
| `voice` | object \| null | How the desk sounds on a call (K6): `{"id": "<AVSpeech voice identifier>", "rate": 0.1-2.0}`. `null` = pick one from the name. |
| `state` | string | One of `NEEDS_YOU`, `WORKING`, `DONE`, `SHELL`, `IDLE`, `DEAD`, `OFFLINE`, `ASLEEP`. `OFFLINE` = a roster desk with no process at it and nothing to resume. `ASLEEP` = no live session, but a last session exists and is woken by the next message: draw it as resting ("it wakes when you message it"), never as broken. Treat any unknown value as `IDLE`. |
| `unread` | int | See §7. |
| `last_activity_at` | float \| null | Newest message across all this agent's threads; falls back to the session's state timestamp; `null` if neither exists. **This is the timestamp the row shows.** |
| `last_activity_by` | string | Who spoke last in this desk's conversation: `"agent"`, `"owner"`, or `""` when nobody ever has. Always present. **`state` alone cannot say this** — see below. |
| `preview` | string | The one-line preview. See below. |
| `thread_id` | string | The agent's 1:1 thread with the owner. Always `direct:<name>`. Always openable, even with zero messages. |
| `pinned` | bool | For the pinned-favourites row at the top. |
| `notifications` | bool | The per-agent toggle. Default `true`. |
| `desk` | bool | `true` = a roster desk that outlives its process. `false` = a live session with no desk. |
| `cwd` | string | The desk's working folder. For an agent hired through the interview door with no folder stated, this is the workspace the deck allocated it (`~/.claude/agent-bus/workspaces/<name>/`). Show it as "working in …" rather than guessing. Allocated once: it does **not** move when the agent renames itself. |
| `project` | string | Basename of the working folder of the **seated session**. `""` for an `OFFLINE` desk — nobody is there, so there is no live folder. Use the settings payload's `cwd` (§4) if you need the desk's configured folder. |
| `session_id` | string \| null | `null` when offline. |
| `account` | string | The Claude account this desk runs under (`GET /v1/accounts`). Always an id, never `""`: the default account is `"main"`. |
| `running_account` | string \| null | The account its live session is on. `null` when nothing is running. It differs from `account` only while a move is under way. |
| `blocked` | object \| null | **Why this desk is dark.** Non-null only while `state == "OFFLINE"`, and only when the deck knows the reason. See §3.1 — render it; a `blocked` desk needs a person, an ordinary `OFFLINE` one does not. |
| `boss` | string \| null | Org chart. `null` = reports to the owner. Read-only here. |
| `reports` | [string] | Who reports to this agent. Read-only here. |

**Order** is roster order (desks first, in the order they were hired), then live
sessions with no desk. The client does the grouping by `section` and the
sorting inside a section; the server does not impose one.

### 3.0 `last_activity_by` — "Idle" on its own is misleading

`state` is the **session's** disposition. It is computed from what the process
is doing and knows nothing about the conversation, so `IDLE` covers three
situations a person would never call the same thing:

| `state` | `last_activity_by` | what it actually is |
|---|---|---|
| `IDLE` | `"agent"` | **It answered you and is waiting.** Say that, not "nothing is running". |
| `IDLE` | `"owner"` | You said something and it has not come back yet. |
| `IDLE` | `""` | Genuinely idle since it started — nobody has ever spoken to it. |

Read from the thread, not the session, so it survives a restart: a desk that
answered and was then re-seated still reads `"agent"` while `state` drops to
`OFFLINE`.

**It is not on the `agent_state` SSE frame** (§8), which still carries only
`{type, ts, name, state, blocked}`. A `state` change that should also change
this line needs the row — re-read `GET /v1/agents`, or take it from the newest
message in the thread you already have open.

### 3.0.1 A desk can be restarted underneath you

Same desk, same `name`, same `thread_id`, **new session**. It happens whenever
the deck re-seats a desk — a `start` call, a redeploy, a hire being re-briefed —
and the row's `session_id` is the only field that changes.

The new session is no longer blank: the deck replays the desk's conversation
into its opening prompt, capped, and a replay that had to be cut says so in the
text it hands over. The desk therefore still knows what it was told; what it
does **not** have is the previous session's working memory.

The conversation carries one message when this happens, in that desk's own
thread, `"role": "agent"`, opening with `[Agent Deck] This desk was restarted.`
Render it like any other message — the `[Agent Deck]` prefix is the deck
speaking about the desk, and it is there because a silent replacement is
indistinguishable from an agent that keeps forgetting.

### 3.1 `blocked` — a desk that needs a person at the keyboard

Two causes, one field, and both mean the same thing to a reader: this desk
cannot move and the app cannot move it.

**A desk that never started.** Before opening a session in a workspace it
created, the deck clears the two start-up dialogs Claude Code would otherwise
draw (§11.2). When that fails, the session still opens — it just opens *on the
dialog*, waits forever, and never runs its opening prompt. The desk then reads
`OFFLINE`, identical to one nobody has started yet.

**A desk that started and then froze.** A seated session sitting on a
permission dialog that the deck has no matching question for is stopped just as
hard, and until this was added the row said nothing at all — it looked like an
ordinary working desk. This is the common case for any desk outside the
permission hooks. See `dialog_unrelayed` below.

```json
"blocked": {
  "what": "workspace trust was not pre-accepted; the session is waiting on Claude Code's trust dialog",
  "reason": "lock_busy",
  "detail": "/Users/samcarter/.claude.json.lock held for more than 3.0s",
  "at": 1756000123.4
}
```

| Key | Type | Notes |
|---|---|---|
| `what` | string | A whole sentence, already written for a human. Safe to show verbatim if you have no sentence of your own for the slug. |
| `reason` | string | Machine-readable. The five values below. |
| `detail` | string | Specifics — a path, an exception, an engine name. Your fallback when `reason` is one you do not know. |
| `at` | float | Epoch seconds, when the attempt failed. |

| `reason` | Means | What the person can do |
|---|---|---|
| `lock_busy` | `~/.claude.json` was locked by another Claude Code process and stayed locked. | Retriable. `POST /v1/agents/{name}/start` tries the whole thing again. |
| `unexpected_shape` | `~/.claude.json` did not parse, or is not an object with a `projects` map. | Needs a human to look at that file. Nothing was written. |
| `write_failed` | The write itself failed — permissions, a full disk. `detail` carries the exception. | Needs a human. |
| `not_deck_workspace` | **Legacy** (before 2026-09-30): the desk's `cwd` was a folder the owner named. Since the owner ruling "agents never dead-end", such a folder is vouched for at start, and a stored block of this kind clears by itself once the folder is trusted. | Nothing; start or message the desk. |
| `too_broad` | The desk's `cwd` is `/`, the home directory or an ancestor of it. Trusting it would trust every folder below, so the deck will not. | Re-hire the desk into a project folder or a deck workspace. |
| `no_such_directory` | The desk's `cwd` does not exist. | Re-hire the desk into a folder that exists. |
| `engine_not_covered` | The desk's engine is `codex` or `opencode`, which have their own first-run gates the deck does not clear. | Informational. See §11.2. |
| `dialog_unrelayed` | The desk is **seated** (not `OFFLINE`) and sitting on a permission dialog in its own Terminal window, and no live question for it reached the deck. | Nothing in the app can answer this. The person has to go to that session's window. `detail` carries Claude Code's own notification text, which names the tool. |

**Rules.** `blocked` can be non-null in **two** states, and this changed:

* while `state == "OFFLINE"`, for every slug above except `dialog_unrelayed`;
* while the desk is **seated and frozen**, for `dialog_unrelayed` only.

> **Contract change.** This field was previously documented as non-null *only*
> while `state == "OFFLINE"`, and a client written against that rule will drop
> `dialog_unrelayed` on the floor — which is the exact stall it most needs to
> show. If you gate the badge on `state == "OFFLINE"`, remove that gate and
> render `blocked` whenever it is non-null. Nothing else about the shape moved.

Either way it **clears by itself** — the OFFLINE reason when the session sits
down, `dialog_unrelayed` the moment the dialog goes. Neither is stored against
the desk, so do not clear it yourself and do not cache it across a state
change. Render a *sentence per slug*, never the slug; for a slug you do not
recognise, fall back to `detail`, then to `what`. A new slug added to this API
will always be added to the table above, so a generic fallback rendering for a
specific cause means this doc was not read.

### The preview line

Most of what a working agent does is talk to **another agent**, so the preview
is built from that traffic, not only from the owner's own chat:

| Situation | Preview |
|---|---|
| Newest is the owner's message to this agent | `You: <text>` |
| Newest is in the 1:1 thread, from the agent | `<text>` |
| Newest is a peer message this agent **sent** | `Messaged <other>: <text>` |
| Newest is a peer message this agent **received** | `Message from <other>: <text>` |
| No messages at all | `""` |

Collapsed to one line and clipped to 140 characters with an ellipsis.

---

## 4. `GET /v1/agents/{name}` — the settings panel

**Response 200**

```json
{
  "name": "hemingway",
  "label": "Researcher",
  "description": "Researches deals and drafts the counter-offers; never sends.",
  "charter": "Own the words.",
  "mission": "Own the words.",
  "avatar": null,
  "voice": {"id": "com.apple.voice.premium.en-GB.Malcolm", "rate": 1.1},
  "avatar_look": {"shape": "hexagon", "color": 3},
  "section": "Personal",
  "boss": "chief",
  "reports": [],
  "notifications": true,
  "pinned": false,
  "state": "OFFLINE",
  "blocked": null,
  "unread": 1,
  "thread_id": "direct:hemingway",
  "engine": "claude",
  "cwd": "/Users/samcarter/Projects/acme",
  "created_at": 1755900500.0,
  "is_desk": true
}
```

`blocked` is the same object, with the same rules, as on the sidebar row —
see §3.1. It is carried here as well because this is the panel a person opens
when a desk looks dead, and making them go back to the list to find out why is
the whole failure this field exists to end.

The panel's four editable fields map as: **avatar** → `avatar`, **Name** →
`name` (not editable — see below), **Title** → `label`,
**Description** → `description`. `charter` is the desk's whole brief, written to the desk; edit it
only from an advanced view. `mission` is what the process is actually started with and is kept
equal to `charter`; ignore it unless you are showing what a running session was
briefed with.

`is_desk: false` means this is a live session with no roster entry: `charter`,
`engine`, `cwd` and `created_at` will be empty/`null`, and `PATCH` of `label`
or `charter` returns 409 `not_a_desk`.

404 `unknown_agent` for a name that is neither a desk nor a live session.

---

## 5. `PATCH /v1/agents/{name}` — change a setting

**Request** — send only the keys you are changing.

```json
{"label": "Designer", "section": "Personal", "notifications": false, "avatar": "hemingway.png"}
```

| Key | Type | Stored in |
|---|---|---|
| `label` | string | the roster desk |
| `description` | string \| null | the roster desk; whitespace collapsed to one line; `null` or `""` clears it; over 280 characters is refused 400 `bad_description` |
| `charter` | string | the roster desk (also sets `mission`) |
| `mission` | string | the roster desk |
| `model` | string | the roster desk |
| `avatar` | string \| null | client preferences (the picture) |
| `avatar` | object `{"shape", "color"}` | the roster desk, as `avatar_look` (K6) — read back as `avatar_look` |
| `avatar_look` | object \| null | the roster desk; `null` clears it |
| `voice` | object `{"id", "rate"}` \| null | the roster desk; `null` clears it |
| `section` | string \| null | client preferences |
| `notifications` | bool | client preferences |
| `pinned` | bool | client preferences |

**Response 200** is the full settings payload from §4, re-read after the write.

**`avatar` has two meanings, by type.** A string or `null` is the picture
preference it has always been — the installed app sends it on every save, so a
`null` there never touches the drawn character. An **object** is K6's drawn
character and is stored as the desk's `avatar_look`; it comes back under
`avatar_look`, **not** `avatar`, because the installed app decodes `avatar` as
a string and an object there would fail its whole settings decode. Clear the
character with `"avatar_look": null`. A bad shape/colour is `400 bad_avatar`,
a bad voice `400 bad_voice`; either on a live session with no desk is
`409 not_a_desk`.

**`reports_to` is refused.** Sending either `reports_to` or `boss` — *at all*,
even unchanged — is a **409**:

```json
{
  "ok": false,
  "reason": "reports_to_is_not_a_setting",
  "detail": "who a desk reports to is the org chart, not a preference; use the roster to change it"
}
```

Who reports to whom decides where work and escalations travel. It is not a
preference a phone toggles by accident, and a client that offers it as one is
wrong. Renaming an agent is likewise not here: `name` is the identity every
thread id, message and org edge is keyed on.

---

## 6. Threads and messages

### Thread ids

| Kind | Id | Read-only |
|---|---|---|
| `direct` — owner ↔ agent | `direct:<agent>` | no |
| `peer` — agent ↔ agent | `peer:<a>|<b>`, names sorted ascending | **yes** |
| `group` — owner ↔ several agents | `group:<group name>` | no |

A group thread's `participants` are the owner plus the group's members (§14).
Posting to it fans the message out to every member, once each, down the same
queue a direct message uses.

**A direct thread id follows a rename.** When an interviewed agent names itself
the thread becomes `direct:<new name>`; the old id still resolves to the same
thread and every response carries the canonical `thread_id`, so a client that
was mid-request is corrected rather than 404'd. Follow `agent_renamed` (§8) to
re-key your local store.

The sort is what makes one pair always one thread. Percent-encode the id when
you build a URL (`peer:chief%7Chemingway`); agent names must not contain `|`.

The owner is `"sam"` on the wire, in `participants` and in `author`.

### `GET /v1/threads`

```json
{
  "threads": [
    {
      "id": "direct:chief",
      "kind": "direct",
      "title": "chief",
      "participants": ["sam", "chief"],
      "read_only": false,
      "message_count": 1,
      "last_ts": 1788222777.0982761,
      "last_cursor": "001788222777098276-393395200ac2",
      "preview": "status please"
    },
    {
      "id": "peer:chief|hemingway",
      "kind": "peer",
      "title": "chief ⇄ hemingway",
      "participants": ["chief", "hemingway"],
      "read_only": true,
      "message_count": 1,
      "last_ts": 1756000050.0,
      "last_cursor": "001756000050000000-9f2a15f5b1bf-1756000050",
      "preview": "Draft the counter-offer, keep it under 200 words."
    },
    {
      "id": "direct:hemingway",
      "kind": "direct",
      "title": "hemingway",
      "participants": ["sam", "hemingway"],
      "read_only": false,
      "message_count": 0,
      "last_ts": 0.0,
      "last_cursor": "",
      "preview": ""
    }
  ],
  "generated_at": 1788222777.207811
}
```

Newest first. Every agent always has a `direct` thread, even an empty one.

**`title` for a peer thread is already `chief ⇄ hemingway`** (U+21C4), so the
two-party header draws without the client assembling it. `read_only` is stated
in the payload — **do not infer view-only from `kind`, from the participants,
or from anything else.** The footer reads "This chat is view-only" whenever
`read_only` is `true`.

### `GET /v1/threads/{id}/messages`

**Query parameters**

| Param | Default | Meaning |
|---|---|---|
| `since` | — | Return messages **strictly newer** than this cursor. Walks forward. |
| `before` | — | Return messages **strictly older** than this cursor. Walks backward. |
| `limit` | `50` | 1–200. Values outside that range are **clamped**, not refused; a non-numeric value falls back to `50`. |

`since` and `before` are mutually exclusive; if you send both, `since` wins.
With neither, you get the **newest `limit`** messages — which is what opening a
chat wants.

**Response 200** — always oldest-first, in every mode:

```json
{
  "thread_id": "peer:chief|hemingway",
  "kind": "peer",
  "read_only": true,
  "participants": ["chief", "hemingway"],
  "messages": [
    {
      "id": "9f2a15f5b1bf-1756000050",
      "cursor": "001756000050000000-9f2a15f5b1bf-1756000050",
      "thread_id": "peer:chief|hemingway",
      "author": "chief",
      "role": "agent",
      "ts": 1756000050.0,
      "text": "Draft the counter-offer, keep it under 200 words.",
      "attachments": [],
      "via": "transcript",
      "truncated": 0,
      "delivery": null
    }
  ],
  "has_more_before": false,
  "has_more_after": false,
  "next_since": "001756000050000000-9f2a15f5b1bf-1756000050",
  "next_before": "001756000050000000-9f2a15f5b1bf-1756000050"
}
```

| Field | Meaning |
|---|---|
| `id` | Stable for the life of the message. Use it for dedupe and for `up_to`. |
| `cursor` | Opaque, sortable, total-order key. See §6.1. |
| `author` | `"sam"`, `"routine"`, `"deck"`, `"engineer"`, or an agent name. Who **actually** sent it (§6.4). |
| `role` | `"owner"` (Sam typed it), `"system"` (a routine fire, the deck, or the engineer), or `"agent"`. A closed set; render an unknown value as `"agent"`. |
| `channel` | `"text"` or `"voice"`. Always present; `"text"` unless it was said on a live call. |
| `kind` | `"text"` or `"decision"`. Always present. A `"decision"` message also carries `decision` — see §6.5. |
| `attachments` | `[{"kind": "file"|"image"|"link", "value": "..."}]`, up to 10. **Derived from the text** — see §9. A path the deck holds (under `<bus>/attachments/`: his uploads and files a desk sent with `mcp__deck__send_file`) also carries `url` (`GET` it with the token; `Range` answers 206), `name`, `media` (`image`/`video`/`audio`/`pdf`/`file`), `mime`, `bytes` (absent if the file is gone) and, once made, `preview_url` (a JPEG poster frame or thumbnail). `kind` stays one of the three: old clients decode it as a closed enum. |
| `via` | `"office"` (the deck's message queue) or `"transcript"` (observed in a session transcript). Diagnostic; ignore it in UI. |
| `truncated` | `0` normally. Non-zero means the sender wrote **that many characters** and `text` is only the first 64,000 of them. **Show this.** An agent's 6,047-character report once arrived as 2,000 characters and read like a message that simply stopped; the number is here so a client never has to guess. The text also ends with `[Agent Deck cut this message]` for clients that ignore this field. |
| `reply_to` | Present only on a quote-reply: `{"id", "author", "excerpt"}` — the message this one answers, who wrote it, and the deck's own one-line excerpt of it (≤ 200 characters, `…` when cut). Draw it as a quote above the bubble; a tap scrolls to `id`. |
| `delivery` | What became of a message **the owner sent**, recomputed on every read. `null` on everything else — see §6.3. **Show this.** |
| `has_more_before` / `has_more_after` | Whether older / newer messages exist beyond this page. |
| `next_since` / `next_before` | The cursors to send to keep walking. Never build a cursor yourself. |

### 6.1 The pagination contract — read this one properly

**Cursors, never offsets.** A cursor names a *message*; an offset names a
*position*, and positions shift under an append. If you page backwards through
history with `offset`/`page`, and the agent sends three messages between two
requests, page N+1 **silently skips three messages**. On a phone this shows up
weeks later as "sometimes an old message is just missing", and it is
unreproducible. This API has no offset parameter for that reason.

A cursor is `<18-digit microsecond timestamp>-<message id>`, fixed width, so
lexical order is chronological order and the tiebreak on identical timestamps
is the id. That makes the order **total and stable**: two clients paging the
same thread see the same sequence.

Guarantees:

- **Backward paging never skips and never duplicates.** `before=<cursor>` is
  anchored to a message, so appends at the end of the thread cannot move the
  window. Loop until `has_more_before` is `false`.
- **Forward paging never replays.** `since=<cursor>` is exclusive. Messages
  arriving during the walk are picked up by the next request, in order.
- **Cursors are comparable as strings.** `a < b` means `a` is older. You may
  sort and deduplicate locally on `cursor` alone.
- **Cursors are opaque.** The format above is documented so you can *compare*
  them, not so you can construct them. Always echo back `next_since` /
  `next_before`, or a `cursor` off a message you were given.
- A cursor this API did not issue is a **400 `bad_cursor`**, never a 500 and
  never a silently empty page.

The recommended client loop:

1. Open the chat: `GET .../messages?limit=50`. Render oldest-first.
2. Scroll up: `GET .../messages?limit=50&before=<messages[0].cursor>`, repeat
   while `has_more_before`.
3. Catch up after a reconnect: `GET .../messages?since=<newest cursor you
   hold>`, repeat while `has_more_after`.
4. Live: take new messages off the SSE stream (§8), and reconcile by `id`.

**The one case this does not cover:** a message that arrives with a timestamp
*older* than a cursor you already hold will not be returned by a `since` walk
past it. That happens only when the underlying clock or a transcript replay
puts a record out of order. If you need to be certain after a long offline
period, re-walk backwards from the newest with `before` instead of forward with
`since`.

### 6.2 The relay — a desk's dispatch and the reply, inline

**`thread_id` on a message is not always the thread you asked for.** A direct
thread carries, in order, both what the desk says to the owner *and* the
messages that desk exchanged with another desk. The peer messages keep their
own `peer:` id; the rest carry the page's `direct:` id. That difference is the
entire wire attribution — there is no new field.

```json
// GET /v1/threads/direct:chief/messages  → messages, abridged
[
  {"author": "sam",      "thread_id": "direct:chief",           "text": "any updates?"},
  {"author": "chief",     "thread_id": "direct:chief",           "text": "Checking Harbor opens and Acme ads."},
  {"author": "chief",     "thread_id": "peer:chief|hemingway",   "text": "Status now: any new page_views since 307?"},
  {"author": "hemingway", "thread_id": "peer:chief|hemingway",   "text": "page_views new: 0  replies: 0  max: 307"},
  {"author": "chief",     "thread_id": "direct:chief",           "text": "Harbor just now: no new opens."}
]
```

**How to render it.** For each message, compare `message.thread_id` with the
page's `thread_id`:

| Test | Draw |
|---|---|
| equal | an ordinary bubble — the owner's, or the desk's answer to him |
| differs, and `author` is this desk | `to <name>` — a dispatch this desk sent |
| differs, and `author` is not this desk | `from <name>` — a reply that came back |

`<name>` is the participant of the `peer:` id that is not this desk. Parse it
the way §6 defines it (`peer:<lower>|<higher>`, and a name may not contain
`|`). Draw those two quieter than a plain bubble — smaller, indented, the
label in front. **Do not draw them as the desk talking to the owner:** they
are a worker's dense counts and ids, not an answer to him. Tapping one should
open the `peer:` thread the `thread_id` names — it is the full record.

**Which traffic is inlined: both parties, and nobody else.** A message appears
in the `direct:` threads of exactly the two desks that sent and received it. If
two of a manager's reports message each other, that never enters the manager's
thread — the manager's chat grows with what the manager itself said and heard.
That fence is what keeps the conversation readable on a board of eight: a
status sweep across three reports is six inline lines, not the subtree's whole
traffic. If a client wants the subtree, it reads the `peer:` threads.

**It is one record, not a copy.** The message returned by `direct:chief`,
`direct:hemingway` and `peer:chief|hemingway` has the **same `id`** and the
same `cursor` in all three. Dedupe on `id` as §6.1 already tells you to and you
will never show it twice; `POST /v1/agents/{name}/read` with `up_to` accepts it
from any of the three. `unread` counts it once.

**What has not changed.** The `peer:` thread is unchanged, still `read_only`,
and still refuses a post with `thread_is_read_only`. No message field was added
or removed. A client written before this section still works — it will simply
render the relayed lines as plain bubbles, which is the old "Messaged X: …"
preview problem moved rather than solved. Implement the table.

### 6.3 `delivery` — the tick, and the one that must not look like a tick

**The failure this exists for.** The box's Claude login expired and every desk
started and died instantly. He typed "hi?", "whats goingon?", "hello?", "?",
"hello" into his chief's thread over some hours and got nothing back. **The app
drew all five exactly like messages that had landed**, because this endpoint had
no field that said otherwise, and he ended up asking whether the deck was
offline. It had been, the whole time. His words: *"i cant see sent/read"*.

```json
"delivery": {
  "state": "undelivered",
  "reason": "no_session",
  "what": "Nobody is at hemingway's desk, so nothing has taken this. It is on the queue and is handed over the moment a session starts there -- but until one does, hemingway has not seen it."
}
```

`state` is a **closed set of three**. Branch on it:

| `state` | what the server actually knows | draw |
|---|---|---|
| `delivered` | a session **took** this message — either the deck pushed it down the desk's live socket, or the desk's session pulled it into a turn | the ordinary "sent" tick |
| `sent` | nobody has taken it yet, and a session is seated (or asleep and being woken) that will read it | a quieter, single tick — no alarm |
| `undelivered` | nobody has taken it and **nothing at that desk can** | **loud.** This is the state the whole field exists for |

**There is no `read` state and you must not invent one.** Both things that can
acknowledge a message write the same record, so the server genuinely cannot
distinguish "a session took it" from "the model read it". `delivered` claims
the first and only the first. A tick labelled "read" here would be a guess, on
the one screen this field exists to make honest.

**It is recomputed on every read, not stamped when he pressed return.** A
message sent while a desk was dark reads `undelivered`; when that desk comes
back and takes it, the same message on the same thread reads `delivered`. So
**re-render the bubble when the thread refreshes or a `message` frame arrives on
the stream** — a client that caches the first `delivery` it saw shows him a
permanent red mark on a message that arrived fine.

`reason` is `""` unless `state` is `undelivered` (or `sent` with `waking`), and is a **closed set**:

| `reason` | means | what he should do |
|---|---|---|
| `no_session` | there is no session at that desk at all — never started, or it died | start the desk; §11 `POST /v1/agents/{name}/start` |
| `waking` | (`state: "sent"`) the desk is `ASLEEP` and is being woken to read this | nothing; show the normal single tick |
| `desk_blocked` | a session is there and cannot take a message: it is stopped on a dialog that owns its keyboard. See §3.1 `blocked` on the agent row for the same fact | it needs hands at that machine |

`what` is a finished sentence addressed to the owner, always naming the desk,
safe to show verbatim. Prefer it to writing your own — the phone receipts use
the same string, so the two surfaces cannot drift.

**`delivery` is `null`, not absent, for:**

* anything a **desk** said — he is reading it; there is nothing to report.
  (`delivery` **is** computed for `role: "system"` messages addressed to a desk:
  a routine fire is still a delivery.)
* a **peer** line relayed into his thread (§6.2) — it was never his to deliver;
* a message in a **group** thread. One fan-out folds to one message here, and
  the per-member acknowledgements are not on it. The group's own POST (§14)
  still answers `delivery` with `reached` and `waiting`.

**Nothing was removed.** A client written before this field still works; it
just goes on drawing a dead desk's thread as though everything landed.

### 6.4 Who sent it — `author` and `role` are the truth

`role: "owner"` means **Sam typed it**, and nothing else. A schedule firing
is `author: "routine"`, the deck's own notices ("Hired: …") are `author:
"deck"`, and a message from the person building the deck is `author:
"engineer"`; all three are `role: "system"` — draw them as centred captions,
never as his bubbles. The sidebar `preview` reads `Routine: …` / `Deck: …` /
`Engineer: …`, they do not count as `unread`, and `last_activity_by` stays
`"owner"` for them (it is a closed `agent|owner|""` set).

### 6.5 Decisions — a desk asks him to pick

A desk that needs his call asks with the `ask` deck tool (§17), and it lands
in the desk's direct thread as an agent message with `kind: "decision"`:

```json
{
  "id": "4be1c0a9d2e3", "thread_id": "direct:atlas", "author": "atlas",
  "role": "agent", "channel": "text", "kind": "decision",
  "text": "Northwind waitlist copy?\n- Approve as-is\n- Rewrite",
  "decision": {
    "id": "dec_1a2b3c4d5e6f",
    "prompt": "Northwind waitlist copy?",
    "help": "Full draft: ...",
    "options": [
      {"label": "Approve as-is", "value": "Approve copy as-is", "style": "primary"},
      {"label": "Rewrite", "value": "Rewrite the copy", "style": "default"}
    ],
    "allow_custom": true,
    "state": "open",
    "answer": null
  }
}
```

2–4 options, labels ≤ 40 characters, `style` ∈ `primary|default|danger`.
`text` is the question plus one `- <label>` line per option, for a client that
does not draw cards. `decision.state` is a closed set: `open`, `answered`,
`skipped`. **It is the one part of a decision message that changes**, and it
is recomputed on every read, like `delivery`.

**`POST /v1/decisions/{id}`** `{"value": "<option value, or custom text>"}` →
`200`:

```json
{"ok": true, "decision": { ...state "answered", answer set... },
 "delivery": { ...as on a POST to the thread... }, "message": { ...his message... }}
```

It marks the card `answered`, then posts `value` into the same thread as **his
own message** (`author: "sam"`, `role: "owner"`) through the same door his
typing uses — so it is framed as him for the desk, and a sleeping desk is woken
to read it. Draw the card's buttons from `decision.options` and send the
chosen option's `value`, not its `label`.

| Status | `reason` | when |
|---|---|---|
| 400 | `empty_value` | `value` missing or blank |
| 400 | `not_an_option` | custom text on a card with `allow_custom: false` |
| 404 | `unknown_decision` | no such id |
| 409 | `already_answered` | the card is already answered — do not re-post |

**A later message of his in that thread marks every still-open card
`skipped`** (he moved on). A skipped card can still be answered — the tap is the
newest thing he said about it. Messages sent `as: "engineer"` skip nothing.

The stream announces a card when it is asked and again on every state change
(§8, `decision` frame), so a card flips to answered/skipped under a connected
client without a refetch.

### `POST /v1/threads/{id}/messages`

**Request**

```json
{"text": "ship it", "as": "sam", "channel": "text", "call_id": "call_ab12"}
```

`as` is `"sam"` (default) or `"engineer"`; **engineers and automation must
send `"as": "engineer"`**, or the desk is told the owner said it. Anything else
is `400 bad_sender`. `channel` is `"text"` (default) or `"voice"`; voice needs a
`call_id` (`400 missing_call_id`). The desk reads a voice message as
`<utterance>\n(said on a live call)`; the API returns the utterance without
that suffix. A group thread accepts only the default `sam` / `text`.

`reply_to` (optional) makes it a quote-reply: a message id from **this**
thread — his own line, the desk's, a relayed agent-to-agent line, or a group
message — or `{"id": "..."}`. The deck reads the author and the excerpt off its
own copy; anything else in the object is ignored. The desk is told, between
the deck's frame and his words, `Replying to your message: "<excerpt>"` (or
`Replying to <desk>'s message: …` / `Replying to his own earlier message: …`).
Unknown id: `404 unknown_reply_to`.

**Response 201**

```json
{
  "ok": true,
  "delivered": false,
  "delivery": {
    "state": "queued",
    "reason": "",
    "reached": [],
    "waiting": ["chief"],
    "what": "chief is not at a desk right now. This is waiting and will be read on the next turn there -- it is not lost, and sending it again would send it twice."
  },
  "message": {
    "id": "9e6da0f8c369",
    "cursor": "001788222777235606-9e6da0f8c369",
    "thread_id": "direct:chief",
    "author": "sam",
    "role": "owner",
    "ts": 1788222777.235606,
    "text": "ship it",
    "attachments": [],
    "via": "office",
    "truncated": 0,
    "delivery": {
      "state": "undelivered",
      "reason": "no_session",
      "what": "Nobody is at chief's desk, so nothing has taken this. It is on the queue and is handed over the moment a session starts there -- but until one does, chief has not seen it."
    }
  }
}
```

`delivered: true` means the bytes went into the agent's live session right
then. `delivered: false` means it is **queued**, not lost: the agent picks it
up on its next turn. Do not retry on `false` — you will send it twice.

**`delivery` is that same fact in a shape you can render, and you should.**
The boolean alone gave every client the same screen for a message that landed
and one that reached nobody, which is what the owner was reporting when he
asked why his messages were disappearing. Branch on `delivery.state`, a closed
set:

| `state` | what happened | show |
|---|---|---|
| `delivered` | every named desk took it down its live session | the message, plain |
| `queued` | none did — it is on disk, waiting | the message plus a *waiting* affordance |
| `mixed` | some did, some did not (groups only) | the message plus the `waiting` names |

`reason` is `""`, or `"waking"` when every waiting desk is `ASLEEP` and the
POST has just started waking it — then `what` reads "atlas was asleep and is
being woken to read this." instead of "not at a desk right now". The state
stays `queued`: it is on disk until the woken desk takes it.

`reached` and `waiting` are agent names. `what` is a finished sentence
addressed to the owner and is safe to show verbatim — it never says "failed",
because nothing failed. **Do not offer a retry on `queued`**: the record is
already on the queue and sending again delivers twice.

Same object on the group door (§13), so this is rendered once, not once per
endpoint.

**Do not build your bubble on this object.** `delivery` at the top level is the
*send's* answer — one fact, frozen at the moment of the POST, and it is the
only shape the group door can answer with. The bubble belongs to
`message.delivery` (§6.3), which is the same question asked of the thread and
**recomputed on every read**: it moves from `undelivered` to `delivered` when a
dead desk comes back and takes the message. Reopening the thread later used to
show the message with no marker at all, which is how five unanswered messages
to a dead box looked exactly like five that landed.

Top-level `delivery.state` (`delivered` / `queued` / `mixed`) is a different
vocabulary from `message.delivery.state` (`delivered` / `sent` / `undelivered`)
on purpose: the first is about a fan-out at one instant, the second is about
one message right now. **If you only implement one, implement §6.3.**

Text is clipped at 64,000 characters by the queue, and a clip is never silent:
the record carries `truncated` (the length you sent) and the stored text ends
with `[Agent Deck cut this message]`. It used to be 2,000 — a limit borrowed
from the messaging socket, applied to a path that never touches one, which is
how an agent's 6,047-character report reached the owner as 2,000 characters
with nothing saying so.

**Posting to a peer thread is a 409, never a silent drop:**

```json
{
  "ok": false,
  "reason": "thread_is_read_only",
  "detail": "this is a transcript of two agents talking; open the agent's own chat to say something"
}
```

A peer thread is a *transcript of two agents talking*. There is no frame in the
underlying protocol that inserts a third party into it, so the honest answer is
a refusal with a reason the client can act on — offer "open <agent>'s chat"
rather than an error toast.

---

## 7. `POST /v1/agents/{name}/read` — clear unread

**Request** — one of:

```json
{"up_to": "9f2a15f5b1bf-1756000050"}
```
```json
{"cursor": "001756000050000000-9f2a15f5b1bf-1756000050"}
```
```json
{}
```

`up_to` is a **message id** (what you have on screen). `cursor` is a cursor. An
empty body means "everything I can currently see for this agent".

**Response 200**

```json
{
  "ok": true,
  "name": "chief",
  "read_cursor": "001756000050000000-9f2a15f5b1bf-1756000050",
  "unread": 0,
  "was": 1
}
```

**How `unread` is defined:** messages in *any* of that agent's threads — its
1:1 with the owner **and** every peer thread it is in — that are **newer than
the read cursor** and **not authored by the owner**. So an agent's own outgoing
peer message does count: from the owner's side it is unread activity on that
desk that he has not looked at.

The read cursor is per **agent**, not per thread. Marking `chief` read clears
its 1:1 and its peer threads together. The total for the "+ More unreads"
affordance is just the sum of `unread` across `GET /v1/agents`.

404 `unknown_message` if `up_to` names a message that is not in that agent's
threads.

---

## 8. `GET /v1/stream` — SSE

```http
GET /v1/stream HTTP/1.1
Authorization: Bearer <token>
Accept: text/event-stream
```

Standard SSE: `data: <json>\n\n` frames, no event names, no ids. Auth is the
same bearer token; an unauthenticated stream is refused with 401 before the
stream opens.

**The first frame is always `hello`** — a full snapshot, so a client that has
just connected needs no second request:

```json
{"type": "hello", "ts": 1788222702.09, "agents": [ ... ], "threads": [ ... ]}
```

`agents` and `threads` are exactly the payloads of §3 and §6.

Then, incrementally:

```json
{"type": "message", "ts": 1788222777.3, "thread_id": "direct:chief",
 "read_only": false, "message": { ...the §6 message object... }}
```

The message on this frame carries `delivery` (§6.3), computed the same way as
on a page — so the tick a message arrives on is not the one tick it has no
state. **`delivery` is the one field on a message that changes after the frame
that announced it.** The stream sends no update for that on its own: re-read
the thread when a desk's `agent_state` says it came back, or on any refresh,
and re-render. Everything else about a message is immutable once you have seen
it.
```json
{"type": "agent_state", "ts": 1788222780.1, "name": "chief", "state": "NEEDS_YOU",
 "blocked": {"what": "this desk is stopped on a permission dialog…",
             "reason": "dialog_unrelayed", "detail": "…", "at": 1788222779.6}}
```
```json
{"type": "agent_state", "ts": 1788222784.7, "name": "chief", "state": "WORKING",
 "blocked": null}
```
```json
{"type": "unread", "ts": 1788222780.1, "name": "chief", "unread": 3}
```
```json
{"type": "agent_renamed", "ts": 1788222781.4, "old_name": "new-hire-7f3a1c",
 "agent": { ...the full §3 agent row, under the new name... }}
```
```json
{"type": "decision", "ts": 1788222790.2, "thread_id": "direct:atlas",
 "decision": { ...the §6.5 decision object, current state... }}
```
```json
{"type": "heartbeat", "ts": 1788222792.0}
```

**The heartbeat is the connection indicator.** One is emitted after every 15
seconds of silence. If you have not seen *any* frame in ~35 seconds, show
"Reconnecting", drop the connection and reopen it; on reopen, take the `hello`
snapshot and then catch up per-thread with `since` (§6.1) — the stream is not
replayable and has no `Last-Event-ID`.

Events are generated by diffing consecutive collector snapshots (~1 Hz), so
`ts` is when the change was *noticed*, while a message's own `ts` is when it
was *sent*. Sort by the message's `ts`/`cursor`, never by the event's.

**A slow client loses events, not its connection.** A backlogged subscriber has
its queue trimmed rather than being disconnected — so treat the stream as a
hint to refresh, and treat `since` paging as the source of truth. Never build
your message store from the stream alone.

`read_only` is repeated on every `message` event so a client can route a frame
without holding the thread list.

**`agent_renamed` carries the whole §3 row**, not just the new name, so the
sidebar swaps the row in place instead of refetching — a partial row is a
flicker, or a second entry for a desk that only ever existed once. It is
emitted **once**, on the tick the rename is noticed. `agent.thread_id` is the
new thread id; re-key any open thread from `direct:<old_name>` to it. A client
that missed the frame is not broken: the old id still resolves (§6).

**`agent_state` carries `blocked`** (§3.1), always — including `null` when
there is none. **Additive**, not a shape change: a client reading only
`{type, ts, name, state}` is unaffected. Before this, a desk that froze on a
permission dialog *while a client was already connected* changed `state` on
the wire with no `blocked` attached, so an already-connected client had no way
to show the reason until its next full `GET /v1/agents` or a reconnect — the
one moment `blocked` exists to cover, since freezing happens mid-session, not
at connect time. The frame is emitted whenever `state` **or** `blocked`
changes, not only on a `state` change: a desk can flip between an answerable
and an unanswerable dialog with `state` sitting at `NEEDS_YOU` throughout, and
`blocked` retracts to `null` on the same frame class the instant the dialog
goes — do not let a `blocked` badge outlive the `agent_state` frame that
cleared it.

No other field on the §3 agent row is diffed onto its own frame yet:
`section`, `avatar`, `pinned`, `notifications`, `label`, `desk`, `cwd`,
`project`, `session_id`, `boss`, and `reports` can all change under a live
connection (a `PATCH`, a restart, a re-parent) with no SSE frame at all, or —
for `cwd`/`project`/`session_id` on a restart — a frame that changes `state`
without carrying the new value. A connected client can hold a stale view of
any of those until its next `GET /v1/agents`; `agent_renamed` is the one
exception, because it already ships the whole row. Treat the stream as a hint
to refresh for anything outside `state`, `blocked`, `unread`, rename, and a
decision's state (the `decision` frame, emitted when a card is asked and on
every change after; update the card on the message with that `decision.id`) —
this is the same rule as "never build your message store from the stream
alone," above.

---

## 9. What was assumed, not measured

Named explicitly, because a client built on a wrong assumption here is a client
that gets rewritten.

1. **`section` is invented by this API.** The reference product groups agents
   under "Work" and "Personal"; Agent Deck's roster has no such concept. It is
   stored per-agent in a client-preferences sidecar and **defaults to `"Work"`
   for every agent that has never been assigned one**. Nothing on the server
   validates or enumerates sections — if the client wants a section list, it
   derives it from the agents it got.
2. **`pinned` is invented by this API**, for the favourites row. Same sidecar,
   defaults `false`. No cap on how many can be pinned; the reference product
   showed two or three.
3. **`notifications` is real, and it is not push.** It was recorded and
   consumed by nothing when this doc was written; it now silences a desk on
   the one channel the deck actually delivers on — the owner's WhatsApp desk
   pager. There is still no APNs registration and no server-side notion of
   "this bot finished", so do not ship a switch that implies device push;
   ship one that says *this desk stops messaging me*. See §9.1.
4. **`avatar` is an opaque client string.** The server stores whatever you
   PATCH and serves no images. Resolve it yourself; `null` means initials.
5. **Attachments are derived from message text by pattern match**, not stored.
   The deck's message log carries a string and nothing else. A URL becomes a
   `link`; an absolute or `~` path becomes a `file`, or an `image` if it ends
   `.png .jpg .jpeg .gif .webp .heic .svg`. **The server does not check that
   the file exists and does not serve it** — except a path under the deck's
   own attachment store, which it serves with its real content type and byte
   ranges (see the `attachments` row above). An agent puts a file there with
   `mcp__deck__send_file`.
6. **The owner is the literal string `"sam"`.** There is one human. There is
   no user table, no login, no per-device identity.
7. **A `direct` thread's owner-side messages are real; the agent-side ones are
   thin.** The office queue records what the owner sends. An agent's reply to
   the owner is only in the thread if it went back through the queue. A 1:1
   thread can therefore look one-sided in a way a chat client does not expect.
   This is a gap in the deck, not in this API.
8. **Peer-thread history is in memory and bounded.** It is reconstructed from
   the collector's `CommsIndex`, capped at 2000 edges across all agents, and
   **lost on daemon restart**. Direct-thread history is replayed from
   `messages.jsonl` on restart and capped at 4000 messages per thread in
   memory. A client must not assume it can page back to the beginning of time.
9. **Two agents that send two messages with the same opening 200 characters
   within 120 seconds collapse into one peer message.** That is the existing
   pairing heuristic in `sources/comms.py`, inherited, not introduced here.
10. **Titles for peer threads use the agent `name`, not the `label`.** The
    reference header read `Chief ⇄ Hemingway`; this returns whatever the names
    are, lowercase included. Capitalisation is a client decision.
11. **The master token is still one token.** Rotating it means changing the
    env var and restarting, which logs out every device that uses it. Paired
    devices have their own tokens and are revoked one at a time (§19).
### 9.1 What the server actually does with a preference

The four keys `PATCH /v1/agents/<name>` writes into `agent_prefs.json` are not
the same kind of thing, and the difference decides whether a switch is worth
putting on a screen. A switch the server ignores is worse than no switch: the
owner flips it, the product carries on, and he cannot tell a broken feature
from one he misunderstood.

| pref key | what the SERVER does with it |
| --- | --- |
| `avatar` | Acted on. `server/onboard.py` lets a desk name its own avatar when it introduces itself, and `server/roster.py` carries it; a PATCH from the client is the same field. |
| `notifications` | Acted on. `false` silences that desk on the owner's WhatsApp desk pager — `server/deskpage.py` refuses to page for a muted desk. Absent means ON: a desk with no pref row was never muted. |
| `pinned` | Nothing — **client presentation only.** Stored, defaulted to `false`, echoed back, and never read by the server. §3 already says the server imposes no order on `GET /v1/agents`; the favourites row is entirely the client's to build. |
| `section` | Nothing — **client presentation only.** Stored, defaulted to `"Work"`, echoed back, and never read by the server. Nothing validates or enumerates sections. |

`read_cursor` is in the same sidecar but is not patchable here — it is written
by `POST /v1/agents/<name>/read` and is what `unread` is computed from.

`tests/test_client_api_doc.py` sweeps this table against the source: it parses
the keys out of `Surface.patch_agent` and the consumers out of the `server/`
package, so a fifth preference, or a preference that grows its first consumer,
fails the suite until this table accounts for it.


---

## 10. Wiring (server-side, for whoever mounts this)

`server/api.py` does not touch `server/app.py`. Mounting is one call:

```python
from . import api

api.register(
    app,
    snapshot=lambda: _state,        # the collector tick app.py already computes
    comms=collector.comms,          # the existing CommsIndex, not a new one
    deliver=_try_inject,            # (name, text) -> True only if bytes went out
)
```

`register` mounts the router, installs the error handler, and starts **one**
shared background task that folds each snapshot forward and fans events out to
every SSE subscriber. One task total, not one per connected phone.

Without `deliver`, sends are queued only and always report
`"delivered": false` — correct, just less immediate.

State lives where it already lives: desks in `roster.json`, owner messages in
`messages.jsonl`, agent-to-agent traffic in the collector's `CommsIndex`. The
only file this module owns is `~/.claude/agent-bus/agent_prefs.json` —
avatar, section, notifications, pinned, and the per-agent read cursor.
Deliberately not on the roster: `roster.Desk` is the org chart, and a phone
toggling a switch must not be able to rewrite who reports to whom.

---

## 11. Hiring — the "+" button

### `POST /v1/agents` — create a desk

```json
{
  "name": "seeker",
  "label": "Researcher",
  "charter": "Find things and say what you found.",
  "cwd": "/Users/samcarter/Projects/acme",
  "engine": "claude",
  "model": "",
  "reports_to": "chief"
}
```

| Key | Required | Notes |
|---|---|---|
| `name` | yes | The identity. Every thread id and org edge is keyed on it. A desk created through *this* door keeps the name you gave it: only an agent that hired itself through §11.1 can rename itself, and only once. |
| `cwd` | yes | Must be a directory **on the Mac**. Refused `no_such_cwd` otherwise. **It is a request, not a guarantee** — read `seat` in the response for where the desk actually went. See §11.3. |
| `engine` | yes | `claude` \| `opencode` \| `codex`. Anything else is `unknown_engine`. |
| `label` | no | The title chip. |
| `charter` | no | What this desk owns, in prose. Also becomes `mission` — see §4. |
| `model` | no | Passed to the engine when the desk is started. |
| `reports_to` | no | The boss's `name`. `null` or omitted means it reports to the owner. |

**Response 201** — `agent` is exactly one element of §3's `agents` array, so the
client inserts it into the sidebar without a refetch:

```json
{
  "ok": true,
  "agent": {
    "name": "seeker", "label": "Researcher", "section": "Work", "avatar": null,
    "state": "OFFLINE", "unread": 0, "last_activity_at": null, "preview": "",
    "thread_id": "direct:seeker", "pinned": false, "notifications": true,
    "desk": true, "project": "", "session_id": null, "boss": "chief",
    "reports": []
  },
  "pretrust": { "ok": false, "reason": "not_deck_workspace", "detail": "…" },
  "seat": {
    "cwd": "/Users/samcarter/.claude/agent-bus/workspaces/seeker",
    "stated": "/tmp",
    "kind": "deck_workspace",
    "substituted": true
  }
}
```

**Read `seat`, not the `cwd` you posted.** See §11.3.

**A hire is not a spawn.** The new desk is `OFFLINE` until something is started
at it — see `POST /v1/agents/{name}/start` below.

**`reports_to` is accepted here and nowhere else.** Naming a new desk's boss is
what hiring *is*. Changing it afterwards is the org chart, and `PATCH
/v1/agents/{name}` still answers `reports_to_is_not_a_setting` (§5). The
asymmetry is deliberate: a phone must not be able to re-parent a desk by
toggling a setting, but it must be able to say who a new hire works for.

Refusals, each with its own `reason`, all `409` unless noted:

```json
{"ok": false, "reason": "name_taken", "detail": "a desk named 'chief' already exists"}
```

`no_such_boss` · `too_deep` (three levels is the cap) · `too_many_live` (RAM, sixteen at most
live sessions) · `no_such_cwd` · `400 missing_field` · `400 unknown_engine`.

**Two clients creating the same name produce one desk**: one `201` and one
`409 name_taken`. Never two desks, and never a half-written roster file.

### 11.1 `POST /v1/agents/interview` — hire by talking to it

The other door, and the one "+" should use. You do **not** state a name, a
title, a charter, or a project folder. You say roughly what you want; the deck
creates a provisional desk, opens a session, and hands that session a prompt
that tells it to ask what the job actually is and then **name itself**.

```json
{
  "role_hint": "handle my email",
  "reports_to": "chief",
  "cwd": "/Users/samcarter/Projects/acme",
  "engine": "claude",
  "model": ""
}
```

| Key | Required | Notes |
|---|---|---|
| `role_hint` | no | One sentence about what you want. Passed to the agent as a *hint, not a specification*; empty is fine and the agent will ask. |
| `cwd` | **no** | Omit it. With no folder the deck creates `~/.claude/agent-bus/workspaces/<placeholder name>/` at mode 0700 and the session runs there — the agent gets its own working directory and still reaches the rest of the Mac through the normal permission path. State one and it must be a real directory (`no_such_cwd`), and it is honoured only if work can happen there — see §11.3. |
| `engine` | no | Defaults to `claude`. Same three values as §11. |
| `reports_to` | no | The boss's `name`. `null` or omitted means it reports to the owner. |
| `model` | no | Passed to the engine. |

**Response 201:**

```json
{
  "ok": true,
  "provisional": true,
  "name": "new-hire-7f3a1c",
  "thread_id": "direct:new-hire-7f3a1c",
  "pretrust": { "ok": true, "reason": "trusted", "detail": "", "muted": ["docker-mcp"] },
  "seat": {
    "cwd": "/Users/samcarter/.claude/agent-bus/workspaces/new-hire-7f3a1c",
    "stated": "", "kind": "deck_workspace", "substituted": false
  },
  "agent": { ...exactly one element of §3's agents array... }
}
```

| `pretrust` key | Type | Notes |
|---|---|---|
| `ok` | bool | Whether the deck cleared the start-up dialogs for this workspace (§11.2). |
| `reason` | string | `trusted` · `already_trusted` on success; on failure one of the five slugs in §3.1, and the same value is on `agent.blocked.reason`. |
| `detail` | string | Specifics — a path, an exception, an engine name. Empty on success. |
| `muted` | [string] | The `.mcp.json` servers this new hire was **not** offered. **Always present, always a list**, empty when there were none — do not treat absent and empty as different. |

`ok: false` does **not** mean the hire failed. The desk exists and the window
opened; it is sitting on a dialog. Show the agent, and show `agent.blocked`
(§3.1) rather than an error.

Open `thread_id` immediately — that is the conversation. `provisional: true`
means the name and the title chip are placeholders the agent is about to
replace; show it as a new chat, not as a finished desk.

**This opens a real Terminal window on the Mac**, like
`POST /v1/agents/{name}/start`. A spawn that fails takes the provisional desk
back off the roster rather than leaving one nobody can talk to.

**Then it names itself.** The session prints one `YOS_DESK` line carrying its
chosen name, title and charter; the daemon reads it back off the transcript and
applies it. The desk's thread, read cursor, unread count and org edge all move
with it, `agent_renamed` goes out on the stream (§8), and the workspace
directory stays where it was allocated. A line naming a *different* desk is
refused and logged — an agent may describe only itself.

Refusals: `400 unknown_engine` · `409 no_such_boss` · `409 too_deep` ·
`409 too_many_live` · `409 no_such_cwd` (only when you stated one) ·
`500 no_workspace` (the deck could not create the directory).

### 11.2 The start-up gates — why a new desk can open and never begin

Claude Code draws a modal for anything it has not seen in a directory before. A
workspace the deck allocated seconds earlier is always one of those, so the
window opens, the modal waits, the opening prompt never runs, and the desk sits
at `OFFLINE` with the *default option being to quit*. Before spawning into a
folder **it created**, the deck therefore clears them — in one write, in the
`~/.claude.json` entry for that path.

| Gate | What it asks | Deck's answer |
|---|---|---|
| Workspace trust | "Quick safety check: Is this a project you created or one you trust?" | Accepted. The deck made the folder, so the deck vouches for it. |
| New MCP server | "New MCP server found in this project: `<name>`" — once per server declared in any `.mcp.json` above the workspace | **Continue without.** A new hire inherits no project MCP server unless the owner says so. The names are in `pretrust.muted`. |

Two boundaries the client should reflect rather than hide:

- **Only folders the deck allocated.** If you send a `cwd`, neither gate is
  cleared (`reason: not_deck_workspace`) and the person will meet both dialogs
  once. That is deliberate: accepting a security prompt on the owner's behalf
  for a directory *he* named would be worse than the stall.
- **Claude only.** `codex` and `opencode` were measured; `opencode` has no
  first-run gate, `codex` has its own trust model (`~/.codex/config.toml`) plus
  an "Update available" prompt. Neither is cleared
  (`reason: engine_not_covered`).

One per-project gate is known and **not** handled: Claude Code's CLAUDE.md
"external includes" approval. It fires only when a `CLAUDE.md` in scope imports
a file from outside the project. It has never fired on this machine, and the
deck deliberately does not pre-approve it — muting a server is conservative,
approving an external file include is not.

### `DELETE /v1/agents/{name}` — fire a desk

**Response 200** `{"ok": true, "name": "seeker"}`. The desk leaves the roster;
a process already sitting at it is *not* killed.

`404 unknown_agent` for a name that is not a desk. `409 has_reports` while
anyone still reports to it — firing a manager first orphans its whole team, so
re-assign or fire the reports first. `detail` names them.

### `POST /v1/agents/{name}/start` — put a session at the desk

**This opens a real Terminal window on the Mac.** There is no headless mode on
this route and no undo.

**Response 200**

```json
{
  "ok": true,
  "detail": "...",
  "pretrust": { "ok": true, "reason": "trusted", "detail": "", "muted": [] }
}
```

`404 unknown_agent`, or `409` with `osascript_failed` / `unknown_engine` and
the cause in `detail`.

`pretrust` is the same block §11.1 returns, and it is **new on this route**:
this door used to open a window without clearing the start-up dialogs at all,
so a desk started here could sit on "Is this a project you created or one you
trust?" while its row read `OFFLINE` with `blocked: null`. It now clears the
same gates the interview door does, and writes the same `agent.blocked` reason
(§3.1) when it cannot — and clears a stale one when it can.

`ok: false` does **not** mean the start failed. The window opened; it is
sitting on a dialog. Read `agent.blocked` and show that.

The same gate now also applies to the deck's own `POST /api/roster/{name}/start`,
whose response gained the identical `pretrust` block.

---

### 11.3 `seat` — where the desk actually went

**`cwd` on the way in is a request. `seat` on the way out is the answer.** Both
hiring doors return it, and it is beside the row rather than inside it, so
`agent` stays byte-identical to the element `GET /v1/agents` returns.

| Key | Type | Notes |
|---|---|---|
| `cwd` | string | Where the desk is really seated. Always equal to `agent.cwd`. |
| `stated` | string | The `cwd` you posted, verbatim. `""` when you posted none. |
| `kind` | string | `git_repo` · `deck_workspace`. Never `scratch` — a scratch seat is never kept. |
| `substituted` | bool | `true` when `cwd` is not what you asked for. |

A stated folder is honoured when work can actually happen in it:

* **`git_repo`** — the path is inside a git repository. The repo root, a
  directory under it, or a worktree (whose `.git` is a file). A desk hired onto
  `~/Projects/acme-lead` sits in `~/Projects/acme-lead`.
* **`deck_workspace`** — a direct child of `~/.claude/agent-bus/workspaces/`,
  which is the only thing the deck will pre-accept the trust dialog for (§11.2).

Anything else — `/tmp`, a `/var/folders` scratch directory, a bare home
directory — is a **scratch seat**, and the deck allocates
`~/.claude/agent-bus/workspaces/<name>/` at 0700 and seats the desk there
instead, with `substituted: true`.

**Why substitution and not a refusal.** MEASURED on the box, 2026-09-07: four
of six desks on the real roster were seated in `/tmp`, because the caller had
to put *something* in `cwd` and invented a scratch path. `/tmp` cannot be
pre-trusted, so all four opened on Claude Code's trust dialog and never ran a
turn — four cards reading *"Waiting for you: workspace trust was not
pre-accepted"* for a folder the owner never chose. Refusing the hire would
strand the caller with an agent it could not create at all; the intent ("give
this agent somewhere to work") is satisfied by moving the seat.

**A path that does not exist is still `409 no_such_cwd`.** That is a typo, not
a scratch seat: a caller that meant `acme-lead` and wrote `acme-led` is told,
rather than moved somewhere else while believing it got the repo.

`409 no_workspace` if the deck could not create the directory.

---

## 12. `GET /v1/approvals` — the card in the thread

An agent hit something its rules do not cover, so it stopped and asked. The
list is **newest first** and holds only live questions: one that timed out is
swept off before the response is built (§12.1).

```json
{
  "approvals": [
    {
      "id": "z47x8",
      "ts": 1788225548.976903,
      "agent": "chief",
      "asked_by": "8edb89dc-6be9-4daf-b90e-cf88c4630410",
      "desk_known": true,
      "tool": "Bash",
      "subject": "gh pr create --title 'ship the routes'",
      "cwd": "/Users/samcarter/Projects/acme",
      "cwd_short": "~/Projects/acme",
      "status": "pending",
      "options": [
        {"reply": "once", "available": true, "rule": null,
         "summary": "just this time"},
        {"reply": "always", "available": true,
         "summary": "allow `gh pr*` in ~/Projects/acme, never ask again",
         "rule": {"id": "ask-z47x8", "kind": "always_allow", "tool": "Bash",
                  "pattern": "gh pr*", "cwd": "/Users/samcarter/Projects/acme",
                  "note": "from chief on WhatsApp"}},
        {"reply": "never", "available": true,
         "summary": "refuse `gh pr*` in ~/Projects/acme from now on",
         "rule": {"id": "ask-z47x8", "kind": "deny", "tool": "Bash",
                  "pattern": "gh pr*", "cwd": "/Users/samcarter/Projects/acme",
                  "note": "from chief on WhatsApp"}}
      ]
    }
  ],
  "generated_at": 1788225548.998251
}
```

**`agent` is a DESK NAME, and it is the field you draw the card against.** The
permission hook does not send one — it carries a session id — so the deck
resolves it here, by the desk's name, its session id, or the folder the tool
call was made in. It used to publish the session id raw, and a client filtering
`agent == <the desk whose conversation is open>` therefore drew nothing at all,
for any desk, in any thread, while the question stalled the whole board.

`asked_by` is always what the hook actually recorded, unchanged — a session id
in the ordinary case. `desk_known` is `false` on the one question the deck
cannot place: no desk claims that session id and no desk works in that folder.
`agent` then still holds `asked_by`, so **check `desk_known` before filing the
card under a conversation** — a card attached to the wrong desk is worse than
one he has to go and find. Show those in a general tray rather than inventing
an owner for them; they are rare and they still block a desk.

**Show the `summary`, not the word.** `rule` is the *exact* rule that reply
would write — same tool, same folder, the argument widened only to its leading
verb — and `summary` is that rule as a sentence. A button reading "Always
allow" with nothing under it is how somebody grants far more than they meant
to.

`subject` is redacted on the way out: anything credential-shaped is replaced
with `[redacted]`. `rule.pattern` is not redacted — it is what would actually
be written, and showing a doctored version of it would be a lie.

`options` is always the three replies, in the order `once`, `always`, `never`.
`status` is `pending` for everything this route returns.

### "Always" is per desk, and can be missing

When the deck can name the desk that asked (the usual case — `desk_known:
true`), `always` is offered on **every** question, the handoff floor included,
and it means: *this desk* never asks again for *this class*, in this folder and
below. The option carries `rules` — the exact list that will be stored — and a
`summary` built from the same list:

```json
{"reply": "always", "available": true,
 "summary": "acme may `grep*`, `ls*`, `head*` in ~/…/palmread and below, never ask again",
 "rule":  {"id": "ask-rhg7b", "kind": "always_allow", "tool": "Bash",
           "pattern": "grep*", "cwd": "/home/deckop/…/palmread",
           "note": "acme said always to ask rhg7b", "desk": "acme"},
 "rules": [{"...": "one per verb in the command line"}]}
```

The class, per tool: **Bash** — one verb per command in the line (`git push*`,
`cat .env*`, `ls*`); a later line is allowed only when *every* command in it is
one of this desk's verbs, so `grep x; rm -rf .` is still asked. A line holding
`$(…)`, backticks or a here-doc is pinned to itself. **Read/Write/Edit** — the
file's folder and below. **WebFetch** — the site. **MCP tools** — the tool.
The rule is keyed by desk **name**: a wake changes the session id, not the
name, so it survives wakes and restarts.

The option is still missing for a credential, payment or irreversible question
the deck **cannot** pin on a desk:

```json
{"reply": "always", "available": false, "rule": null,
 "summary": "not available here (credential, payment or irreversible)"}
```

Draw that option disabled with its `summary` as the explanation. Sending
`always` anyway is a `409 always_not_available` that writes no rule and leaves
the question live, so he can still answer `once`. Never grey it out silently.

### `POST /v1/approvals/{id}` — answer one

```json
{"reply": "always"}
```

`reply` is `once` | `always` | `never`. Anything else is `400 bad_reply`.

**Response 200** — `rule` is what was just created, or `null` for `once`:

```json
{
  "ok": true,
  "ask": {"id": "z47x8", "ts": 1788225548.976903, "agent": "chief",
          "tool": "Bash", "subject": "gh pr create --title 'ship the routes'",
          "cwd": "/Users/samcarter/Projects/acme", "answered": "always",
          "expired_at": null, "status": "answered"},
  "rule": {"id": "ask-z47x8", "kind": "always_allow", "tool": "Bash",
           "pattern": "gh pr*", "cwd": "/Users/samcarter/Projects/acme",
           "note": "from chief on WhatsApp"},
  "resumed": true
}
```

`resumed` is **new** and a client should show it. `true` means the desk was
told, in words, that it may carry on with that exact action — the ask id, the
tool and the subject — and handed the message straight into its live session
if one was open. `false` means nobody was told, which is correct and expected
for `never` (a refusal grants nothing), and also happens when the ask has
already been resumed once. A card that says *"restarted"* rather than just
clearing is the difference between him knowing the work moved and him
wondering whether it did.

`404 unknown_ask`. `409 already_answered` (`detail` says what it was answered
with) and `409 expired` are **different reasons**, and neither is a success: a
reply that lands on a settled question must not look like it decided
something.

### 12.1 What answering does *not* do

**It does not click the button in the agent's terminal.** That session drew its
own permission prompt and is still sitting on it; nothing outside the process
can dismiss it. What a reply buys is the **next** time: `always` writes a rule
that answers this class of call silently from then on, `never` writes a deny,
and `once` leaves a single unspent grant.

**It does now restart the desk, which it did not before.** A hired agent that
was denied is told "do not retry it in a loop … carry on with something else",
so writing the rule left it parked: measured three times in one run, the owner
tapped Approve and nothing happened until a human sent a chat message. A `once`
or `always` answer now also queues the desk a message naming the ask, the tool
and the subject, and telling it to do that action now. It fires at most once
per ask, ever — `resumed` says whether it fired.

**Expiry is a removal, not a decision.** After four hours an unanswered
question leaves the list. No rule is written and nothing is granted — the
opposite reading ("unanswered for long enough, therefore fine") would hand the
machine to whichever agent asked the most alarming question at 2am and then
waited.

`note` on a generated rule reads `from <agent> on WhatsApp` whatever channel
answered it. That string is the ask module's, not this API's; treat it as
provenance for the *agent*, not for the device.

The answer's response also carries `rules` — every rule it stored (one per
verb for a desk's `always`), `[]` for `once`.

### 12.2 `GET /v1/permissions` — what each desk may do without asking

Every standing per-desk permission an `always` left behind. `?desk=<name>`
narrows to one desk.

```json
{"permissions": [
  {"id": "ask-rhg7b", "kind": "always_allow", "desk": "acme", "tool": "Bash",
   "pattern": "grep*", "cwd": "/home/deckop/…/palmread",
   "cwd_short": "~/…/palmread", "note": "acme said always to ask rhg7b",
   "summary": "acme may run `grep*` in ~/…/palmread and below"}],
 "generated_at": 1790797448.4}
```

### `DELETE /v1/permissions/{id}` — take one back

`{"ok": true, "removed": "ask-rhg7b"}`. The next matching call asks again.
`404 unknown_permission` for an id that is not a standing per-desk
permission (hand-written global rules are configuration and are not touched
here).

---


## 12.5 `GET /v1/handoffs` — the step only a human can take

**This is not the approval layer, and the difference is not cosmetic.** §12
answers a *permission* question: the desk could do the thing, it needs a yes,
and the yes becomes a durable rule. A handoff is the case where no rule helps
because the desk **cannot act at all** — a 2FA code arriving on a phone, a
CAPTCHA, an SMS confirmation, a card payment, an `ssh` passphrase, a
`gh auth login` device code. There is no permission to grant. A human has to
physically do it. (`server/handoff.py` is the engine; read its module docstring
before changing anything here.)

Shaped deliberately like an approval so **one tray in the client can draw
both** and filter both on the same field: same `agent` / `asked_by` /
`desk_known` join as §12, and an `options` list whose entries carry their own
summary text.

```json
{
  "handoffs": [
    {
      "id": "h7c2",
      "ts": 1788712482.5,
      "agent": "acme-growth",
      "asked_by": "bfffdc59-5f34-4b6b-b304-300c80cb3c25",
      "desk_known": true,
      "kind": "sign_in",
      "needs": "Sign in to Microsoft 365 admin (initech.example), then hand back",
      "state": "The tenant is created and nothing has been billed yet",
      "where": "admin.microsoft.com",
      "evidence": "raised by acme-growth at 21:14",
      "status": "waiting",
      "options": [
        {"reply": "done",    "available": true,
         "summary": "I'm done, continue — the desk goes back and checks the step actually worked before carrying on"},
        {"reply": "skipped", "available": true,
         "summary": "Skip this step — the desk abandons that path for good and reports what it can no longer finish"}
      ]
    }
  ],
  "generated_at": 1788712490.1
}
```

**`state` is not decoration and a handoff without it is a bug.** It is
validated when the handoff is raised and always printed. Without it the phone
message says "I need you" and nothing about whether money is already being
spent — which is how somebody ends up opening a laptop in a panic over a draft
campaign that never went live. Draw it on the card.

### `POST /v1/handoffs/{id}` — his answer

One key, exactly as §12: `{"reply": "done"}` or `{"reply": "skipped"}`.

**`done` and `skipped` are different instructions, not different words.**

* `done` — a human says they did it. The desk must **re-check** that it
  actually worked before carrying on. It is not "dismiss this card".
* `skipped` — it will never happen. The desk must **abandon** that path and
  report what it can no longer finish.

Collapse the two and a skipped handoff becomes an infinite retry loop. There is
no third verb on this route: `taken_over` exists in the engine as the WhatsApp
verb for "I have the keyboard right now", and is deliberately **not offered
here**, because it leaves the work unfinished and the card on the board — a
button that changes nothing visible is a button that gets tapped twice.

`404 unknown_handoff`, `400 bad_outcome`, `409 already_resolved`. On success
the deck marks the row and drops it from `GET /v1/handoffs` on the same call —
unlike approvals, there is **no ~1 Hz collector lag here**, so a client does not
need to hold a settled-locally set to stop a card flipping back.

Everything textual on both routes goes through the engine's redactor. A
one-time code in a message that syncs to another device is the precise thing a
secure handoff exists to prevent.

**How this pairs with the desk's browser: §15.** The sign-in he performs there
stays signed in for the agent — one Chromium, one profile, and the profile
outlives the container.

### `POST /v1/logins` — a sign-in made on his Mac, for every desk

His passkeys are in iCloud Keychain, on his Mac and phone, never in a desk's
container — so a passkey-only sign-in cannot finish on a desk's screen. The
Mac app signs him in in a throwaway Chrome profile on the Mac ("Sign in on
this Mac" on a `login` card whose `where` is an https URL), reads that
profile's cookies over CDP and posts them here:

```json
{"source": "mac", "cookies": [{"name": "SID", "value": "…", "domain": ".google.com",
  "path": "/", "secure": true, "httpOnly": true, "sameSite": "None",
  "expires": 1790000000, "session": false, "size": 42}]}
```

`cookies` is the CDP cookie array exactly as Chrome reports it
(`Storage.getCookies`); read-only fields (`size`, `session`) are dropped,
a session cookie stays one, expired and nameless rows are dropped. The deck
merges them into its login vault (0700/0600 — future desks are seeded from
it) and writes them into **every** running desk's browser, the card's own desk
included.

```json
{"ok": true, "shared": true, "source": "mac", "cookies": 31, "desks": 5, "failed": []}
```

Counts only. **Write-only**: no route reads a cookie back, and no value is
logged. Refusals: `400 no_cookies` (not a list, or nothing usable),
`413 too_many_cookies` (over 5000), `409 isolated` (`[desks] shared_logins =
false`). Bearer as every `/v1` route. The app then answers the card `done`
on `POST /v1/handoffs/{id}` so the desk goes back and checks.

## 13. `GET /v1/routines` — the panel

A routine wakes a named agent with a prompt on a schedule.

```json
{
  "routines": [
    {
      "id": "551c873f92ca",
      "agent": "chief",
      "prompt": "morning check",
      "trigger": {"kind": "cron", "spec": "0 9 * * *", "tz": "Europe/London"},
      "next_run_at": 1788249600.0,
      "enabled": true,
      "runs": [
        {"ts": 1788163200.0, "ok": true, "detail": "delivered"},
        {"ts": 1788076800.0, "ok": false, "detail": "no session"}
      ]
    }
  ],
  "generated_at": 1788225549.0081658
}
```

`runs` is the **last three, newest first** — enough to answer "is this thing
working", not an audit trail. `ok: false` with `detail` is why one did not
land. `next_run_at` is `null` for any trigger that is not `cron`.

### `POST /v1/routines`

```json
{"agent": "chief", "prompt": "morning check",
 "trigger": {"kind": "cron", "spec": "0 9 * * *", "tz": "Europe/London"},
 "enabled": true}
```

`trigger` is `{"kind": "cron", "spec": <5-field cron>, "tz": <IANA zone>}`.
The spec is minute / hour / day-of-month / month / day-of-week, and `0` is
Sunday. Any other `kind` is stored and simply never scheduled
(`next_run_at: null`).

**Response 201** `{"ok": true, "routine": { ...the row above... }}`. The `id`
is minted by the server; do not send one.

`404 unknown_agent` for a name that is on nobody's board. `400 missing_field`
for an empty `agent`, `prompt` or `trigger`.

### `PATCH /v1/routines/{id}`

Send only what changes: `enabled`, `prompt`, `agent`, `trigger`. **Response
200** `{"ok": true, "routine": {...}}`, re-read after the write. Editing never
loses `runs`, and disabling never loses the schedule.

### `DELETE /v1/routines/{id}`

**Response 200** `{"ok": true, "id": "551c873f92ca"}`. `404 unknown_routine`.

### 13.1 `next_run_at` is computed, never sent

**Do not put `next_run_at` in a request body.** It is a
`409 next_run_at_is_computed` on both `POST` and `PATCH` — deliberately loud
rather than silently ignored.

The server recomputes it from the trigger on create and on **every** trigger
change. A next-run supplied by a client is how a routine ends up scheduled in
the past, and a routine that is due in the past is due on every scheduler tick,
forever, firing a message into a session that is trying to work.

A cron spec or timezone that could never fire is refused **at write time** with
`400 bad_cron`, naming the field, so a routine the scheduler can never run is
never written:

```json
{"ok": false, "reason": "bad_cron",
 "detail": "trigger.spec: cron spec must have 5 fields, got 2: 'every friday'"}
```
```json
{"ok": false, "reason": "bad_cron",
 "detail": "trigger.tz: 'No time zone found with key Mars/Olympus'"}
```

### 13.2 What a routine run actually is

The prompt goes into that agent's office queue, and is injected straight away
if the agent has a live session. A run recorded as
`{"ok": true, "detail": "queued"}` is waiting for the agent's next turn — not a
failure. An agent that is not running right now is not an error.

---

## 14. `GET /v1/groups` — several agents, one conversation

A group is a **named set of desks with a thread of its own**. It is not a desk,
it is not in the org chart, and it grants nothing: a member of a group has
exactly the authority its desk already had.

```json
{
  "groups": [
    {"name": "acme-launch",
     "members": ["chief", "hemingway", "seeker"],
     "thread_id": "group:acme-launch",
     "created_at": 1788222702.09}
  ],
  "generated_at": 1788222702.4
}
```

### `POST /v1/groups`

```json
{"name": "acme-launch", "members": ["chief", "hemingway", "seeker"]}
```

**Response 201** `{"ok": true, "group": { ...one row of the array above... }}`.

Members are desk `name`s. Duplicates are collapsed in the order first given —
a member listed twice must not be sent the same message twice.

Refusals: `400 missing_field` (no name) · `400 no_members` · `409
unknown_member` (`detail` names them — a typo in a member list is a member that
silently never gets anything) · `409 name_taken` · `409 too_many_members`
(sixteen).

### `DELETE /v1/groups/{name}`

**Response 200** `{"ok": true, "name": "acme-launch"}`. `404 unknown_group`.
The group goes; every desk in it is untouched.

### Sending to a group

There is no separate send route. Post to the group's thread exactly as you post
to a direct one:

```http
POST /v1/threads/group:acme-launch/messages
{"text": "ship the acme reader on friday"}
```

**Response 201:**

```json
{
  "ok": true,
  "delivered": ["chief"],
  "queued": ["hemingway", "seeker"],
  "delivery": {
    "state": "mixed",
    "reached": ["chief"],
    "waiting": ["hemingway", "seeker"],
    "what": "chief read this now. hemingway and seeker are not at a desk right now, so it is waiting and will be read on the next turn there."
  },
  "message": { ...the §6 message object, on thread group:acme-launch... }
}
```

**A group message is a fanout, not a new mailbox.** One copy per member goes on
the same office queue a direct message uses, so a member with nobody at its
desk gets it **queued for its next turn** rather than dropped — that is what
`queued` means, and it is not a failure. `delivered` is the members whose live
session took the bytes there and then.

Every member sees it once. The group's own thread shows it **once**, not once
per member. `404 unknown_group` if the group is gone; `400 empty_text` for an
empty body.

---

## 15. `<Agent>'s screen` — watching the agent's computer, and taking it over

Every desk can have its **own Linux computer**: a container on the Mac running
an X display, a Chromium the agent drives, and nothing else. This is the panel
in the product screenshot — the agent's live desktop, with an **Open** button
that hands the human the keyboard.

Three routes. The design and the security argument are
`docs/the-agents-computer.md`; this section is the wire.

**Nothing here is on `/api`.** The deck's own board (`/api/state`, the page at
`http://127.0.0.1:7788`) has no token on it — it is loopback-only and that is
deliberate. A route that photographs a signed-in browser and injects
keystrokes is not the board, so it lives on `/v1` behind the same bearer token
as everything else, and answers `401` without one and `503` with none
configured. If you are building a local web view, it needs the token too.

### `GET /v1/agents/{name}/screen` — is there a screen to look at

```json
{
  "desk": "acme",
  "computer": {
    "running": true,
    "waking": false,
    "image": "agent-deck/desk-computer:1",
    "container": "deck-desk-acme"
  },
  "display": ":99",
  "width": 1280,
  "height": 800,
  "stale_after": 30.0,
  "frame_url": "/v1/agents/acme/screen.jpg",
  "input_url": "/v1/agents/acme/screen/input",
  "stream_url": "/v1/agents/acme/screen/stream",
  "generated_at": 1756820000.0
}
```

`computer.running: false` is a **200, not a 404** — "this desk has no machine
yet" and "there is no such desk" are different things and the panel renders
them differently. A desk that is not on the roster is `404 unknown_agent`.

**Asking wakes it.** The deck stops idle desk browsers to keep the box alive
(`server/browser_reaper.py`: ASLEEP/OFFLINE desks, IDLE ones unused for
`DECK_BROWSER_IDLE_MINUTES`, and the least recently used past
`DECK_BROWSER_MAX_LIVE`, 3 by default). When this route finds the computer
down it answers `running: false, waking: true`, and starts it once the client
has kept polling it for about 5 seconds (`WAKE_DWELL`) — a glance while
clicking through desks starts nothing. Render "Waking browser…", keep polling
this route, and fetch frames once `running` is true. At the cap with no idle
browser to stop, the start is refused (`browsers_full`, a 409 to a desk's
computer tools) and this route keeps saying `waking`, retrying every
`WAKE_DWELL`. A container on no roster holds no slot and is swept after 10
minutes unused; the chief of staff and the desk being watched are never
refused -- the least recently used browser is stopped for them, after a
brief wait if every one is mid-action. Logins survive: the desk's home is persisted.

`width` and `height` are the display's, and they are what `screen/input`
coordinates are in. Read them here rather than from the JPEG: if the capture
is ever scaled, the image size and the click space stop being the same number.

### `GET /v1/agents/{name}/screen.jpg` — the frame

Returns `image/jpeg` bytes, captured at request time. Not base64 in JSON: this
is polled while the panel is open and base64 is a third more wire plus a
decode for something the platform's image loader already does.

| Header | Means |
|---|---|
| `X-Frame-Age` | Seconds between the capture and the response. **Show the frame as live only while this is under `stale_after`.** |
| `X-Frame-Display` | The X display it came from. |
| `Cache-Control` | `no-store`. This is a photograph of a signed-in browser; do not put it in a disk cache. |

**Poll about once a second while the panel is open, and stop when it closes.**
Each request runs one `ffmpeg` grab inside the container, on the same machine
as the Chromium being watched — a background poller that never stops starves
the thing it is looking at. There is no stream and no WebSocket, on purpose:
see the module docstring in `server/screen.py` for the three properties a
still frame has that a stream does not.

A frame is a **still**, so "nothing changed" and "the feed died" look
identical. That is what `X-Frame-Age` is for, and why `no_frame` is a distinct
slug from `computer_not_running`.

### `WS /v1/agents/{name}/screen/stream` — the screen, pushed as it changes

Prefer this to polling `screen.jpg`. MEASURED over WireGuard: a polled frame
costs 0.5-0.7 s (a fresh ffmpeg per frame), so polling tops out under 2 fps
and a keystroke takes ~0.9 s to show. The stream keeps one grab running per
watched desk and sends a frame only when the display changed (the whole
display: Chrome's own bar and native dialogs included).

**Auth:** the same `Authorization: Bearer` header, on the upgrade request.
A refusal closes before accepting, with 4000 + the HTTP status the route
would have answered (`4401` unauthorized, `4404` unknown desk, `4503` no
token configured, `4429` rate limited). Most WebSocket clients only see the
handshake fail; treat any handshake failure as "use polling".

Server → client:

* text `{"type":"hello","desk","width","height","display","fps","window"}`
  first. `width`/`height` are the click space, as in `GET .../screen`.
* **binary** frames: 8 bytes, then one JPEG. Bytes 0-3 are the sequence
  number and 4-7 the frame's age in milliseconds when sent, both big-endian
  unsigned. Show the newest; age it on your own clock as with `X-Frame-Age`.
* text `{"type":"tick","at"}` every 2 s with nothing new: the last frame is
  still the screen.
* text `{"type":"input","id","ok":true}` or `{..."ok":false,"reason","detail"}`
  for each input, with the slugs `screen/input` uses (`bad_input`,
  `input_refused`, `computer_not_running`, …).
* text `{"type":"error","reason","fallback":"poll"}` then close: `4409`
  with `computer_not_running` (the deck is waking it — poll
  `GET .../screen`, which says `waking`, and reconnect when it is running),
  or `1011` when the grab died. Fall back to polling either way.

Client → server:

* `{"type":"ack","seq":N}` after you have **decoded** frame N. At most
  `window` frames are in flight unacknowledged; the deck drops older frames
  for a slow client rather than queueing them. A client that never acks is
  sent one frame per 5 s.
* `{"type":"input","id":N, ...}` with exactly the body `screen/input` takes
  (`action`, `x`, `y`, …). `id` is yours and comes back in the reply.

### `POST /v1/agents/{name}/screen/input` — the Open button

One gesture per call. This is a real click and a real keystroke on the agent's
display, so it is exactly as privileged as sitting at that machine.

```json
{"action": "click",  "x": 301, "y": 301}
{"action": "click",  "x": 301, "y": 301, "button": 3}
{"action": "click",  "x": 301, "y": 301, "count": 2}
{"action": "move",   "x": 301, "y": 301}
{"action": "scroll", "x": 301, "y": 301, "dy": -3}
{"action": "drag",   "x": 10, "y": 20, "to_x": 300, "to_y": 400}
{"action": "type",   "text": "hunter2"}
{"action": "key",    "key": "ctrl+l"}
```

**Response 200:** `{"ok": true, "action": "click"}`

* **Coordinates are in display space** — the `width`/`height` above, not your
  scaled view. Scale them back before sending. Off-screen is `400 bad_input`,
  never clamped: clamping would hide your arithmetic bug by acting somewhere
  plausible instead. On `drag`, **both** `x`/`y` and `to_x`/`to_y` are checked.
* **`click`** takes optional `button` (`1` left, `2` middle, `3` right;
  default `1`) and `count` (`1`, `2` or `3`; default `1`, so `count: 2` is a
  double-click). Bad values are `400 bad_input`.
* **`move`** places the pointer with no click — a hover a menu needs before it
  opens.
* **`scroll`** takes `dy` and/or `dx`, but **exactly one of them** — a
  diagonal scroll is two separate calls, not one with both set. Negative `dy`
  scrolls down, positive scrolls up; positive `dx` scrolls right, negative
  scrolls left. Magnitude is `1`..`10` in either direction (`dy: -10` is ten
  wheel clicks down in one call). Zero, both set, or neither set is
  `400 bad_input`.
* **`drag`** takes `to_x`/`to_y` for where the button is released, and an
  optional `button` (default `1`) held for the whole motion.
* **`text` is sent verbatim.** It is typed as a single argument, never through
  a shell, so a password containing a backtick, a `$` or a leading `-` arrives
  as those characters. Do not escape it, and do not trim it. Capped at 4096
  characters.
* **`key` is a keysym or a chord** — `Return`, `Tab`, `Escape`, `ctrl+l`,
  `shift+Tab`. Letters, digits, `_` and `+` only. Anything with a leading dash
  is `400 bad_input`, because `xdotool key --file` is a file read wearing a
  keystroke.
* **There is no `run` action, and there will not be one from this route.** The
  terminal and the file manager in the observed dock are a separate surface —
  §16 below.

### How this pairs with a handoff

When a desk stops at a login, a card field or a CAPTCHA it raises a **secure
handoff** — §12.5, a route family of its own, **not** an approval. The
difference is the whole point: an approval is a permission the desk could have
been granted, a handoff is a step the desk cannot take at all. The flow the
client should build is:

1. the handoff card arrives on `GET /v1/handoffs`, with `where` naming the
   site and `evidence` naming the desk and the origin. **There is no `surface`
   field** — a client branching on one branches on a key that never arrives,
   and the take-over it was meant to trigger silently never happens;
2. **Open** on that card opens this panel for that desk;
3. he clicks and types until the step is done;
4. he answers `done` or `skipped` on `POST /v1/handoffs/{id}`. **Not `skip`** —
   that is `400 bad_outcome` and the desk stays stopped.

The sign-in he just performed **stays signed in for the agent** — it is one
Chromium with one profile, and the profile outlives the container. That is the
whole reason the take-over is on the agent's own display rather than in a
second browser.

**Not guarded: both of them typing at once.** Nothing in these routes stops
the agent driving while the human clicks. The handoff loop is what keeps them
apart — the agent stops itself *before* asking — and if a client offers Open on
a desk that never stopped, two things will be using one keyboard.

---

## 16. The desk's terminal and its files

The other two thirds of the same computer. §15 is the screen; these four
routes are the shell and the file tree beside it.

**Everything here runs inside that desk's container, never on the Mac.** That
is the entire point of the desk's computer: an agent's shell is not the
owner's laptop, his keychain and his ssh agent. Same bearer token as the rest
of `/v1` — `401` without one, `503` with none configured — and for a stronger
reason than anywhere else in this document, because a shell on a machine is
the most privileged thing this daemon serves.

A desk with no computer up answers `409 computer_not_running` on all four.
Render that as a sentence and a button, not as a spinner; it does not clear by
itself.

### `POST /v1/agents/{name}/terminal` — run one line

```json
{"command": "ls -la ~/work | head", "cwd": "/home/agent"}
```

**Response 200:**

```json
{"exit": 0, "stdout": "total 12\n…", "stderr": "", "truncated": false}
```

* **A non-zero `exit` is still a 200.** `grep` finding nothing exits 1 and is
  a result the owner asked for. Show `exit`, show `stderr`; do not turn a
  failing command into an error page. The 4xx/5xx cases below are the
  *machine* failing, which is a different thing.
* **`command` is a shell line**, run by `bash -lc` inside the container, so
  pipes, redirection and globbing all work. It is one argument all the way
  down and is never re-split on the way. Empty is `400 bad_input`.
* **`cwd` is optional and must be absolute.** It becomes `docker exec
  --workdir`, its own argument — never a `cd` spliced into your command — so a
  directory called `a;id` is a directory, not a command. Relative or
  dash-leading is `400 bad_path`.
* **Both streams are capped at 64 000 characters** (`office.RECORD_MAX`, the
  same ceiling as a queued message). Past that, `truncated: true` **and** a
  visible `[Agent Deck cut this message]` line in the text itself. Do not rely
  on the flag alone; do not hide the mark.
* **20 seconds, then `504 computer_not_responding`.** This is a terminal for
  looking around, not a build server. A long job is one the agent should start
  detached and then tail through this same route — the timeout kills the
  `docker exec`, and a process already running inside the container keeps
  running.

### `WS /v1/agents/{name}/terminal/stream?window=deck&cols=80&rows=24` — a real terminal

bash in a persistent tmux session inside the desk's container, on a PTY: vim,
top, ssh and anything that asks a question work. The one-shot `POST` above
stays for clients that have not moved and for the old image (see
`terminal_unavailable`).

* **Auth and refusals** are the screen stream's (§15): the bearer token is
  checked before accept; a refusal closes with 4000 + the status (`4401`,
  `4404`, `4503`), and `4400` for a `window` that is not `deck`/`agent` or a
  size outside 1-500 cols, 1-200 rows. A computer that is down is woken, sent
  `{"type":"error","reason":"computer_not_running"}` and closed with `4409`.
* **`window=deck`** is the owner's own shell (`tmux new-session -A -s deck`):
  reconnecting, a second device and the xterm on the desk's desktop all attach
  to the same shell. **`window=agent`** is read-only (`tmux attach -r`): every
  Bash command the desk's agent runs, mirrored with its output. Switching is
  reconnecting with the other value.
* **Server to client:** text `{"type":"hello","desk","window","windows":["deck","agent"],"read_only","cols","rows"}`
  first; then **binary** messages of raw terminal output (a character or an
  escape sequence may be split across two); last, text
  `{"type":"exit","code":N}` (close 1000) or
  `{"type":"error","reason":"terminal_unavailable","fallback":"command"}`
  (close 1011) for a container from before the terminal existed — use the
  one-shot route until it restarts.
* **Client to server:** **binary** input bytes; text
  `{"type":"resize","cols":C,"rows":R}`; text `{"type":"ack","bytes":N}`, the
  cumulative output bytes processed. With more than 256 KiB unacked the deck
  stops reading the terminal until an ack arrives, so **ack at least every
  32 KiB**. A bad resize or an unknown message is ignored, not fatal. Input on
  `agent` is dropped.
* `GET .../screen` advertises it as `terminal_url`.

### `GET /v1/agents/{name}/files?path=/home/agent` — list a directory

`path` is optional and defaults to `/home/agent`, the agent's own home.

```json
{
  "path": "/home/agent",
  "entries": [
    {"name": "work",  "kind": "dir",  "size": 4096, "modified": 1756820002.0},
    {"name": "a.md",  "kind": "file", "size": 312,  "modified": 1756820001.5}
  ]
}
```

* **Sorted directories first, then by name**, by the server. Do not re-sort by
  mtime by default: `find` returns disk order and a tree that reorders itself
  on every refresh is unusable.
* `kind` is `dir` or `file` only, and symlinks are **followed** — a link into
  a project is a `dir` you can open, not a `file` that will not read.
* `size` is bytes, `modified` is epoch seconds as a float, like every other
  time in this document.
* **An empty directory is a 200 with `entries: []`.** A path that is not there
  is `404 no_such_path`. Those look identical in a panel and are opposite
  facts about whether the agent did any work.
* One level. There is no recursive listing and no `depth` parameter; walk it
  by asking again.

### `GET /v1/agents/{name}/files/read?path=/home/agent/x.txt` — read a file

`path` is **required** here.

```json
{"path": "/home/agent/x.txt", "text": "hello\n", "truncated": false}
```

* **64 000 bytes, then cut**, with `truncated: true` and the cut written into
  `text`: `[Agent Deck cut this message]: this is the first 64000 bytes of …`.
  Same ceiling and same mark as everywhere else on this machine.
* **A binary file is `415 not_text`, never mojibake.** NUL bytes or invalid
  UTF-8 and nothing is returned, because a JPEG decoded leniently is a
  screenful of U+FFFD that reads like a corrupt text file. Branch on 415 to
  offer a download or a preview, not an error.
* A character split by the byte ceiling is trimmed, not treated as binary — a
  truncated UTF-8 file still comes back as text.
* A directory is `409 not_a_file`. Use the listing route.

### `GET /v1/agents/{name}/files/download?path=/home/agent/x` — download a file

`path` is **required** here. Unlike `.../files/read`, this route returns raw
bytes, not JSON, and there is no ceiling short of `413` — a client asking to
download a file wants that file, not a truncated preview.

**Response 200:**

```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="x"
Content-Length: 1234
Cache-Control: no-store
```

* **`Content-Type` is always `application/octet-stream`, never guessed from
  the name.** This is `cat`, not a MIME sniffer. `.../files/read` already
  exists for a client that wants the server to reason about the content, and
  refuses with `415 not_text` when it cannot.
* **`filename` is the basename, sanitised for the header.** A Linux filename
  may legally contain a `"` or a newline — the same bytes `path` lets through
  everywhere else on this surface (see "What this is not", below) — and both
  are neutralised on the way into `Content-Disposition` so the header stays
  well-formed and cannot be used to inject a second header.
* **`Cache-Control: no-store`**, the same reason as `screen.jpg`: this may be
  a file from a signed-in session and has no business in a disk cache.
* **25 MiB (`sandbox.DOWNLOAD_MAX`), decided *before* the file is read.** The
  size comes from a `stat`, asked first; a file over the ceiling is
  `413 too_large` and `cat` is never run. This is the difference from
  `.../files/read`'s truncate-after-reading: an 80 GB file must not be an 80 GB
  copy held open on one of the daemon's threads just to find out it was too
  big.
* A directory is `409 not_a_file`, same as `.../files/read`. Use the listing
  route.
* There is no upload route. Getting a file *onto* the agent's computer is a
  separate, unbuilt surface — this is read-only, the same direction as
  `.../files/read`.

### What this is not

**Not a jail.** `path` is checked (absolute, no NUL, under `PATH_MAX`) and
otherwise passed through untouched, including `..`, and there is no
confinement to `/home/agent`. There would be no point pretending otherwise:
the terminal on this same surface is a shell in that container. **The
container is the boundary**, and the argument for why that boundary holds is
`docs/the-agents-computer.md`, not this section.

**Not escaped, deliberately.** `;`, backticks, `$(…)`, `|` and newlines are
legal bytes in a Linux filename, and every path here travels as one element of
an argv with no shell to interpret it. So send the name as it is: do not quote
it, do not escape it, and do not strip anything out of it before sending.

---

## 17. Deck tools — what a desk can do to reach him (server-side)

Not a client route: these are MCP tools inside every hired Claude desk
(`server/deck_mcp.py`, server name `deck`, so the model sees
`mcp__deck__say`, `mcp__deck__ask`, `mcp__deck__message_desk`). They matter to a
client because of what they put in threads. The desk's name is bound in the
spawn argv (one `--mcp-config` carrying both `computer` and `deck`); no tool
takes a desk field.

| Tool | Lands as |
|---|---|
| `say(text)` | An ordinary agent message, **now**, mid-turn — in the owner thread for the chief, in the peer thread with its boss for anyone else. At most 600 characters. Expect a short "On it…" line before the turn's final answer. |
| `ask(prompt, options[], help?, allow_custom?)` | A `kind: "decision"` message (§6.5). The chief only; a junior gets `ask_your_boss` back and says it to its boss instead. |
| `message_desk(name, text)` | A peer message (thread `peer:a|b`, relayed into both desks' direct threads, §6.2). Goes into the peer's live session at once when it has one, and otherwise **wakes** it (reason `peer_message`) — Claude's own SendMessage errors on a sleeping desk instead (`docs/wake.md` #6). |

A desk started before this shipped and then only **woken** keeps the tools it
was started with (a wake passes no flags — `docs/wake.md` #1); it gains `deck`
on its next fresh start.

## 18. Calls

`POST /v1/calls`, `POST /v1/calls/{id}/end` and the voice channel are
documented in **`docs/calls.md`**, as are a desk calling him
(`/v1/calls/incoming*`, `ringing` on `GET /v1/owner/alerts`, a `source: "ring"`
alert) and its settings (`GET`/`PATCH /v1/calls/settings`).

---

## 19. Pairing, device tokens and the rate limit

A deck installed from the easy-setup flow is paired, not configured: the owner
runs `deckctl pair` on the server, which prints a one-time code (the `ADK1.…`
text, also shown as a QR), and the app trades it for a token of its own.

### 19.1 Routes

| Route | Auth | Answers |
|---|---|---|
| `GET /healthz` | none | `200 {"ok": true}`. No version, no names. |
| `POST /v1/pair` | none — the code is the credential | see below |
| `GET /v1/version` | bearer (master or device) | `{"version": "0.9.0", "min_app": "0.9.0", "api": 1}` |

```
POST /v1/pair
{"code": "<22-character secret from the pairing code>", "device": "<name, 1-60 characters>"}
```

```json
// 200, Cache-Control: no-store. The ONLY response that carries a token; it is not shown again.
{"token": "adt_<43 characters>", "device_id": "d_1a2b3c4d",
 "deck": {"name": "Dan's deck", "version": "0.9.0"}}
```

Refusals use the usual `{"ok": false, "reason", "detail"}` shape:

| Status | `reason` | Meaning |
|---|---|---|
| 400 | `pair_malformed` | Body is not `{code, device}`, or the code is not 22 URL-safe characters. |
| 401 | `pair_unknown` | This server never made that code. |
| 409 | `pair_used` | The code was already redeemed. Codes are single use. |
| 410 | `pair_expired` | The code is older than its lifetime (15 minutes). |
| 429 | `rate_limited` | See §19.3. Carries `Retry-After` (seconds). |
| 500 | `pair_failed` | The server could not save the device. Nothing was burned client-side; ask for a new code. |

A refused attempt never burns a valid code, and every attempt counts against
the pairing budget *before* the code is looked at.

### 19.2 Using and losing a device token

Send it exactly like the master token: `Authorization: Bearer adt_…`. Every
`/v1` route accepts it (`tests/test_device_tokens_reach_every_v1_route.py`
sweeps all of them). The server stores only its SHA-256 in
`<state_dir>/devices.json`, so a stolen state file does not yield usable
tokens. The server-side CLI (`deckctl`, installer slice) lists devices and revokes one by id; the
next request with that token is a `401 unauthorized`, and the client must go
back to the pairing screen.

### 19.3 Rate limit and ban

Keyed by client address (the TCP peer, or — behind the public Caddy edge — the
last `X-Forwarded-For` hop it wrote):

* **10 wrong bearer tokens in 10 minutes** bans that address for **30 minutes**:
  every `/v1` answer, even with a correct token, is `429 rate_limited` with
  `Retry-After`.
* `POST /v1/pair` allows **5 attempts per address per 10 minutes**, whatever
  the outcome.
* A request with **no** token is logged but does not count: it is not a guess,
  and counting it would lock out an app that has not been paired yet.
* **The deck's own machine is exempt.** A caller on loopback that carries no
  `X-Forwarded-For` (`bin/deck`, session hooks, the board on the same Mac) is
  never counted or banned; it still needs a valid token. Only the public proxy
  sets that header, so this cannot be claimed from outside.
* Each failure writes one journal line,
  `deck-auth-fail ip=<ip> route=<path> reason=<reason>`, never the token.
  Bans live in memory: a daemon restart clears them.

A client seeing `429` should stop retrying, show the wait from `Retry-After`,
and not treat it as a bad token.

## 20. The Mac bridge — `/v1/nodes`

The routes the Mac app dials out to so desks can work on the owner's Mac
(register, long-poll, job events, grant answers) and the owner's view of his
Macs. Contract of record: [docs/mac-bridge.md](mac-bridge.md), MB1. No route
creates a job; the Mac enforces every grant.

## 21. Claude accounts — usage per account, and moving a desk

Plan of record: `docs/plans/2026-10-01-two-accounts.md`. Every field here is
additive; a deck with one account answers with one entry, `main`.

**`GET /v1/usage`** keeps its top-level fields, which are the default account's
meter. It adds:

| Field | Type | Meaning |
|---|---|---|
| `unofficial` | bool | Always `true`. The numbers come from an undocumented Anthropic endpoint, so label the meter as unofficial. |
| `accounts` | [object] | One meter per account, `main` first: `{id, label, kind, plan, available, stale, reason, windows, extra_usage, fetched_at, refresh_expires_at, desks}`. `windows` has the same shape as the top level. `desks` lists the roster desks on that account. |
| `policy` | object \| null | `{mode, threshold_pct, failover_allowed, default, failover_order, api_account}`. `mode` is `fixed`, `failover` or `failover_api`. It is `null` when deck.toml cannot be read. |

Each account's `reason` is one of the C4 slugs, or one of these:

- `idle_token`: nothing has run on that account for about 8 hours, so its access token lapsed. The login itself is still good until `refresh_expires_at` (epoch seconds), and the next desk that runs there refreshes it.
- `meter_off`: deck.toml has `claude.usage_meter = false`, so no request was sent.
- `api_account`: a Console (API-key) account, signed in with `deckctl login --account <id> --console`. It is billed per token and has no plan window. With `failover_api`, it is where desks go when every subscription is spent.

**`GET /v1/accounts`** returns `{"accounts": [{id, label, kind, plan}]}`.
`kind` is `subscription` or `api`.

**Signing an account in from the app.** The deck runs the CLI's own `claude auth login` for that account, as the service user. The CLI keeps and refreshes the login, and the deck stores no token. It works for a new account and for re-signing `main`.

1. `POST /v1/accounts/login {"id": "work", "label": "Work"}` returns `{login_id, account, label, status: "waiting_code", url, expires_at, detail}`. Open `url` in a browser that is signed in to *that* Claude account and approve; Claude then shows a code. (MEASURED, claude 2.1.286: the link is `https://claude.com/cai/oauth/authorize?...`.)
2. `POST /v1/accounts/login/{login_id}/code {"code": "..."}` hands the code straight to the CLI. The deck never echoes, logs or keeps it.
   - It answers `status: "done"` once `claude auth status` confirms the login in that account's directory, and the account is then registered.
   - A code Claude does not accept is refused with `login_failed`. The CLI has exited, so the way on is a new sign-in.
   - An existing account id, `main` included, means "sign in again".
3. `GET /v1/accounts/login/{login_id}` returns the same object. `status` is `waiting_code`, `done`, `failed` or `expired`. A sign-in expires 10 minutes after it starts, and the CLI is stopped.

| Status | `reason` | Meaning |
|---|---|---|
| 400 | `bad_account` / `bad_code` / `bad_request` | Not an account id, the code is not one printable line, or the body is missing. |
| 404 | `unknown_login` | No such sign-in. |
| 409 | `login_in_progress` | That account already has a sign-in waiting for its code. Only one runs at a time. |
| 400 | `login_failed` | Claude did not accept the code. Start a new sign-in. |
| 409 | `not_waiting` | This sign-in already finished. |
| 410 | `expired` | Ten minutes passed. Start again. |
| 502 | `login_failed` | The CLI did not start, printed no link, or stopped listening. |

**`PUT /v1/accounts/policy`** takes `{"mode": "fixed" | "failover" | "failover_api"}`.
It is the auto-switch toggle, and it answers with the effective `policy` object.

- The mode is stored in a file the deck owns, and that file overrides deck.toml.
- It never goes past `failover_allowed`, which only deck.toml grants.
  - Asking for an automatic mode where that is false answers 403 `failover_not_allowed`.
  - Turning the toggle off always works.
- An unknown mode answers 400 `bad_mode`.

While the mode is automatic, the deck checks every 5 minutes. It moves an idle desk off an account whose 5-hour or weekly window has reached `threshold_pct`. The desk goes to the next account in `failover_order` that is under the threshold and signed in. A desk mid-turn is tried again on the next pass.

**`POST /v1/agents/{name}/account`** takes `{"account": "<id>"}`. The deck does
not move a desk in the middle of a turn. It moves a desk only when all of these hold:

- the desk has been idle for at least 20 seconds;
- no subagent is running;
- no ask or handoff is open.

A desk that is asleep can always move. The session keeps its id and its memory.
Its first turn after the move starts with a cold prompt cache.

| Status | `reason` | Meaning |
|---|---|---|
| 200 | — | `{ok, moved, desk, from, to, session_id}`. `moved: false` means it was already on that account. |
| 400 | `bad_request` | No `account` in the body. |
| 404 | `no_account` / `unknown_agent` | No such account, or no such desk. |
| 409 | `not_idle` | It is mid-turn or waiting on something. `detail` says which. Try again when it is idle. |
| 409 | `engine_not_supported` | Not a Claude desk. |
| 502 | `move_failed` | The move did not take. The desk was resumed on its old account and its row is unchanged. |
