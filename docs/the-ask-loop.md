# The ask loop

**Date:** 2026-09-01
**Modules:** `server/asking.py`, `server/notify.py`
**Status:** built and tested; **not wired into the daemon** (deliberately — that
is a separate task)

## What this is, and what it is not

It is **not** "answer the permission prompt from your phone". Two separate facts
kill that, and both are settled:

- A `PreToolUse` hook is synchronous and sits on the hot path of every tool
  call. It cannot wait for a human on WhatsApp.
- A permission prompt already drawn in a running session cannot be answered
  remotely. The session's socket protocol has no frame for it (spec M1), and
  `~/Projects/comunicate_with_me/src/inject.js` says the same thing from the
  other side: *"The injected turn is a user turn: permission prompts still gate
  every action it triggers. Nothing here may ever auto-approve anything."*

What it is:

1. `autoreview.evaluate` returns `ask`. The session draws its prompt and waits,
   exactly as today. The hook returns immediately.
2. **Separately, in the background**, one WhatsApp message goes out: which
   agent, which tool, the exact argument, the folder, three numbered replies.
3. Sam replies one word. `always` → permanent `always_allow` rules for **that
   desk, by name**: that tool, one verb per command in the line (or the file's
   folder, or the site), in that folder and below (`asking.desk_rules`). A wake
   changes the session id, not the name, so they survive it. `never` → a
   `deny`. `once` → nothing.
4. The **next** time that class of thing happens, the engine answers it silently
   and the agent never stops.

**The value is the second time, not the first.** The first question still costs
an interruption and a stall. What this buys is that it costs that once instead
of forty times.

## The bridge contract — measured, not assumed

Read on 2026-09-01 out of `~/Projects/comunicate_with_me` (running and connected;
nothing in it was modified).

### Outbound

`bin/wa-send.js` is the only entry point we use.

| Fact | Where |
|---|---|
| `node bin/wa-send.js --file <path>` sends the file's contents | `bin/wa-send.js` `parseArgs` / `main` |
| It POSTs `/send` to `127.0.0.1:7799` with a bearer token loaded from the project's own `.env` — **the caller never handles the token** | `bin/wa-send.js` `call()`, `src/config.js` |
| Exit codes are meaningful: `0` sent, `1` usage/send error, `3` daemon not running, `4` not linked to WhatsApp | `bin/wa-send.js` `EXIT` |
| Text is treated as **markdown** and rewritten into WhatsApp markup unless `raw: true`. `**x**` → bold. Over 4000 chars it is split into labelled `(1/3)` parts | `README.md`, `src/format.js` |
| Every send records a **return address** built from `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_SESSION_ID` in the *sender's* environment, plus its cwd and git branch. Absent vars → `socket: null` | `bin/wa-send.js` `buildSession` |

`server/notify.py` uses exactly that: writes the text to a temp file, runs
`node <script> --file <tmp>`, unlinks the file, and reports `ok` **only** on exit
0. Script and interpreter come from `DECK_WA_SEND` / `DECK_WA_NODE`, defaulting
to `~/Projects/comunicate_with_me/bin/wa-send.js` and `node`.

### Inbound — how a reply is routed

This is the half that matters, and the half with the problem.

1. `src/wa-client.js` emits an `inbound` event.
2. `src/inbound.js` (the orchestrator) authorises the sender, then calls
   `src/router.js#route(inbound, registrySnapshot, rosterSnapshot, now)` — a
   pure function.
3. `route` picks a target in this order:
   - **identity**: `sender_pn` → cached LID → `remoteJid`; anything that is not
     `RECIPIENT`, or is `fromMe`, is ignored;
   - **dedup** by message id;
   - **quoted reply wins outright**: `entries.find(e => e.msgId === quotedStanzaId)`
     — the registry keys every *outbound* message id to the session that sent it;
   - `sessions` / `s` / `list` → a numbered listing;
   - `N: text` (roster index) and `<name>: text` (roster name fragment);
   - bare `N` selecting from a live menu/listing;
   - **sticky focus** — the last session written to, unless another has pinged
     since;
   - otherwise: one live candidate → it; several → an ambiguity menu.
4. A resolved target becomes `{action: 'inject', target, text}`, and
   `src/inject.js` writes **one user turn** into that session's UNIX socket at
   `/tmp/cc-socks/<pid>.sock`, containment-checked, 2000 chars max, prefixed
   `[via WhatsApp — your reply goes to Sam's phone…]`.

**There is no HTTP callback into the deck anywhere in that path.** The only
things a reply can be routed *to* are: a live Claude Code session's socket, or a
text reply back to the phone. That is the whole target set.

## The gap I did not close, and did not paper over

`server/notify.py` shells out from the **deck daemon**, which has no
`CLAUDE_CODE_MESSAGING_SOCKET`. So `buildSession` records `socket: null`, and
when Sam quote-replies to an ask, `router.js#resolveTarget` returns:

> `that message came from a shell, not a session`

**So today a reply to a deck-sent ask reaches nothing.** The message goes out
fine; the word `always` comes back to a dead end. `asking.answer()` exists,
is tested, and is correct — nothing calls it yet.

Two routes back, neither of which I built, because both cross into files this
task was fenced out of:

- **A**: the deck exposes `POST /api/ask/<id>/<reply>` and something on the
  bridge side calls it. Requires changing `comunicate_with_me` — out of bounds,
  and it is their repo's call, not ours.
- **B** (no change to the bridge): `notify.send` sets
  `CLAUDE_CODE_MESSAGING_SOCKET` / `CLAUDE_CODE_SESSION_ID` in the child
  environment to **the asking session's** socket. `buildSession` then records a
  real return address, and a quoted reply injects the word `always` into that
  session as a user turn. The session is under the hook, so it can call
  `asking.answer()` itself. `notify.send` deliberately leaves the child
  inheriting `os.environ`, which makes this a one-line change when someone
  decides it.

**B is an assumption, not a measurement.** I did not send a message, did not
quote-reply to one, and did not observe an injection. What is measured is only
that `buildSession` reads those two variables and that `route` prefers a quoted
match over everything else.

### Other things assumed rather than measured

- **Agent name.** `Ask.agent` is whatever the caller passes. The deck's roster
  (`office.json`) and the bridge's `roster.js` both use names, and they mostly
  agree, but I did not verify that a roster desk name and a `PreToolUse` hook's
  session identity resolve to the same string. If they do not, `compose()` names
  something Sam does not recognise.
- **Markdown.** `compose()` emits `**agent**` for bold. A command containing an
  unpaired `*` (e.g. `chmod 755 *.sh`) passes through `src/format.js` and may
  render oddly. Sending with `raw: true` would fix the fidelity and lose the
  bold; I chose bold. Not tested against the live formatter.
- **Rate cap.** `src/inbound.js` references a `rateCap` on outbound replies. I
  did not measure its limit. `asking.suppressed()` is our own guard and does not
  depend on it.
- **Delivery.** Exit 0 from `wa-send.js` means the daemon accepted it, not that
  WhatsApp delivered it.

## The rule an answer creates

`asking.rule_from(ask, kind)` is the dangerous function and it is deliberately
stingy:

- **tool exact** — an answer about `Bash` says nothing about `Write`;
- **folder exact**, and `glob.escape`d — a directory literally named `a*b` must
  not turn "this folder" into "any folder";
- **argument widened to its leading verb only** — the first token, plus a second
  token when that reads as a bare subcommand, plus `*`.

```
git push origin main   →  git push*      (does NOT match `git commit -m x`)
gh pr create           →  gh pr*
npm --version          →  npm*           (a flag is an argument, not a subcommand)
pytest                 →  pytest*
/Users/y/p/README.md   →  /Users/y/p/README.md*
```

Keeping only the first token would make one answer about `git push` approve
`git commit`, `git reset` and everything else sharing that binary. Widening past
the verb would be worse still. `tests/test_asking.py::test_a_rule_from_one_command_does_not_cover_a_different_one`
pins all four axes: same-family allows, different verb asks, different tool
asks, different folder asks.

### The floor

Anything `autoreview.HANDOFF_PATTERNS` names — passwords, tokens, `.env`,
payment, `rm -rf`, force pushes, `drop table` — is asked every time **until the
owner says `always` for a desk the deck can name**. Then that desk's class (the
verbs on the card, that folder and below) is lifted for that desk only
(`autoreview.evaluate` step 0; 2026-09-30, after "why always allowed in the app
never works" — in bypass mode the floor is the only thing left that asks, and
refusing `always` on it made the button do nothing). With no desk to key it
to, `answer()` still downgrades `always` to `once`, writes **no** rule, and
`refusal(ask)` explains why. `never` on a handoff is still allowed.

### The flood guard

`suppressed(path, tool=…, subject=…, cwd=…, window=900)` — the same three fields
inside fifteen minutes sends once. An agent in a retry loop costs one message,
not forty. Matching is exact on all three: a *different* question is never
suppressed, because a swallowed question is an agent stuck forever with nobody
knowing why.

One known consequence: after Sam answers `once`, an identical call inside the
window is also suppressed. He is not re-asked on WhatsApp; the session still
draws its own prompt, so nothing is auto-approved — he just does not get the
second buzz.

### Redaction

`redact()` runs on every subject before it leaves the machine: URL userinfo,
known key prefixes (`sk-`, `ghp_`, `xox?-`, `AKIA`, JWTs), any 24+ character
mixed letter-and-digit run, and `token=` / `password:` / `Authorization:`
values. It fails closed — an over-eager mask costs a "what was that?", an
under-eager one puts a live token in a WhatsApp thread forever.

## What a message looks like

```
**acme-growth** wants to run gh pr create
in ~/Projects/acme

1 once — just this time
2 always — allow `gh pr*` there, never ask again
3 never — refuse it from now on

Reply with a number or the word. (ask k7f3q)
```

`parse_reply` accepts the word or the number, optionally preceded by the ask id
(`k7f3q always`, `k7f3q: 2`). It is strict: `never mind, I'll do it` parses as
**None**, not as a standing deny.

## What is deliberately not here

- No daemon wiring: nothing calls `record`, `notify.send` or `answer` yet.
  `server/app.py`, `server/collector.py`, `web/*`, `server/roster.py` and
  `server/hire.py` were not touched.
- No changes to `~/Projects/comunicate_with_me`.
- No `Ask` → `events.jsonl` audit line. The spec wants every decision on the
  bus; that belongs with the wiring, next to the existing `approval` event.
