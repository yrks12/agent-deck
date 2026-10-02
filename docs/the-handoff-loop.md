# The handoff loop

**Date:** 2026-09-01 · **Module:** `server/handoff.py` · **Tests:** `tests/test_handoff.py`

## What it is for

An agent hits something it physically cannot do: a 2FA code that arrives on a
phone, a CAPTCHA, an SMS confirmation, a card payment, an `ssh` passphrase, a
`gh auth login` device code. There is nothing to approve. A human has to act,
and then the agent has to carry on.

This is **not** `server/autoreview.py`. That module answers *permission*: the
agent could do the thing, it needs a yes, and the yes becomes a durable rule
(spec §1, M2, M9). Here no rule helps — no amount of permission turns a text
message into something a process can read. Auto Review's built-in
`is_handoff()` floor is the *detector* for this class of call; this module is
what happens after it fires.

## The loop, end to end

1. **Block.** The agent cannot proceed. It calls
   `raise_handoff(path, agent=…, kind=…, needs=…, state=…, where=…, evidence=…)`.
   `state` is mandatory — see below. `evidence` is normally
   `evidence_from_transcript(tail_of_its_own_output)`.
2. **Store.** The handoff is appended to `~/.claude/agent-bus/handoffs.json`,
   atomically (tmp + `os.replace`, as `manager.py` and `roster.py` do), capped
   at the newest `MAX_HANDOFFS = 50`. It gets a 4-character id from an
   alphabet with no `0/O`, no `1/l/I` and no case — because the id is
   something Sam types with a thumb to answer a message.
3. **Buzz.** `compose(h)` renders the phone message. Pure function, no I/O.
4. **Act.** Sam does the step. He replies `done 87mp`, `skip 87mp`, or
   `take over 87mp`.
5. **Record.** `resolve(path, "87mp", "done"|"skipped"|"taken_over")` flips the
   status and persists it.
6. **Resume.** `resume_message(h, outcome)` is injected back into the blocked
   session as an ordinary user message, via the existing
   `manager.inject(pid, text)` path.
7. **Sweep.** Anything still `waiting` whose agent is no longer alive becomes
   `stale` via `mark_stale(path, alive=…, now=…)`, so the queue never fills
   with blocks nobody can clear.

## Why resuming works when answering a prompt does not

Spec finding **M1** is the load-bearing negative of this whole surface: the
Claude Code UDS protocol handles `auth`, `user` and `control` frames only, and
**no frame answers a permission prompt**. A prompt already drawn on screen can
never be dismissed from outside. That is why Auto Review has to decide in a
`PreToolUse` hook, *before* the prompt exists.

**None of that constrains this module,** and the next person will assume it
does unless told. Resuming an agent after a handoff is not a dialog answer —
it is a new instruction, which is exactly what the `user` frame is for and what
`manager.inject` already writes today. So the handoff loop closes end to end
with no missing primitive. Step 6 above is a solved problem; only the wiring is
outstanding.

Two caveats inherited from the write path, both already recorded in the spec:
injection depends on `crossSessionInbound: "accept"` (M7), and nothing is
written back on the socket, so a rejected write is invisible (M6). A resume
that silently fails to land looks identical to one that landed. That is a
property of the transport, not of this module.

## The three rules that make it not-harmful

### 1. A handoff that does not state the state of the work is a bug

The message Grok Bot sent its owner was:

> "Google Ads is stuck on *Confirm it's you* before I can publish the campaign.
> **Draft is £20/day, UK + Israel, Search only — not live.** Need you on that
> prompt."

The bolded sentence is the whole thing. Before Sam touches anything he already
knows nothing has gone live. Strip it and the message says only "I need you",
which reads as an emergency whatever the truth is.

So `state` is validated non-empty at `raise_handoff` (a blank one raises
`ValueError`) and always printed by `compose`, bolded, in third position.
Pinned by `test_a_handoff_without_state_is_rejected` and
`test_compose_always_states_the_state_of_the_work`.

### 2. `done` and `skipped` are different instructions, not different words

This is where the real bug lives.

- **`done`** — a human *says* they did it. That is a claim, not a fact: the 2FA
  may have timed out, the card may have been declined, the CAPTCHA may have
  been the wrong one. So the message orders a **re-check**: run the exact check
  that failed, read the result, and raise a *new* handoff if it still blocks.
- **`skipped`** — it will not happen. The message orders **abandonment**: do
  not retry, now or later; report what can no longer be finished, and continue
  with anything independent.

If those collapse into one string, a skipped handoff becomes an infinite retry
loop — an agent hammering a login screen until something locks it.
`test_done_and_skipped_are_materially_different_instructions` asserts the
distinguishing instruction is present in each, not merely that the strings
differ.

`taken_over` is a third instruction: **stand down and wait** — the human has
the keyboard, and two hands on the same screen is its own failure. It also
deliberately does not clear the handoff from the record, because the work is
not finished; it just stops appearing in `waiting()`, which is the "needs a
nudge" queue.

### 3. Nothing secret reaches the phone

Every string that can be rendered goes through `redact()`: URL `user:pass@`
credentials, secret-ish query parameters, labelled secrets (`password: …`,
`code: …`), `Bearer`/`Basic` headers, vendor-prefixed keys (`sk-`, `ghp_`,
`xox…`, `AKIA…`), 32+ hex digits (the `peerToken` shape), any 40+ character
opaque run, and bare 6–8 digit one-time codes. A one-time code sitting in a
chat that syncs to a second device is precisely the thing a secure handoff
exists to prevent.

Redaction is deliberately over-eager, but every shape rule requires a label, a
scheme, a vendor prefix or an implausibly long run, so ordinary prose survives:
`test_redact_leaves_an_ordinary_handoff_alone` pins that `"Campaign is a draft
at £20/day, UK + Israel, Search only, not live."` comes back byte-identical.
`192.0.2.10` and `£20/day` are untouched by the OTP rule for the same reason.

## Evidence

For a terminal agent, evidence is the tail of its own output —
`evidence_from_transcript(lines, limit=20)` keeps the **last** `limit`
non-empty lines (not the first), redacts each with the same rule, and caps the
result at 2000 characters. That is the honest equivalent of Grok Bot's
screenshot: the last thing printed before the agent stopped is almost always
the prompt it could not answer.

`capture_screen(dest)` is an optional macOS `screencapture` path, **off** unless
`DECK_HANDOFF_SCREENSHOT=1`. It needs Screen Recording permission that may
never have been granted, and when it has not been, macOS returns a blank or
desktop-only image rather than an error — worse than no evidence. No test calls
it, and nothing in the module depends on it.

Evidence is deliberately **not** inlined into `compose()`. It rides on the
deck's card; pasting a log tail into a phone message pushes the state line off
the screen, which defeats the point.

## What I assumed rather than measured

Named explicitly, because none of these were verified against a running system:

1. **That the Grok Bot message and card are shaped as described.** I copied
   them from the brief. I did not read xAI's docs or see the product. The
   three-button card and the bolded state sentence are taken on trust.
2. **That `manager.inject` will actually deliver a resume message to a blocked
   agent.** The mechanism is proven for ordinary messages, but I injected
   nothing into any real session — there are live sessions doing real work on
   this machine. I never observed a resume land. Steps 1–5 and 7 of the loop
   are tested; step 6 is reasoned, not run.
3. **That an agent obeys the resume instruction.** `resume_message` is a
   prompt. Nothing enforces that a model told "do not retry" does not retry.
   The test pins the *text*, which is all a pure function can pin.
4. **That the agent name in a handoff matches the names in the liveness set
   passed to `mark_stale`.** I chose roster desk names (`acme-growth`) because
   that is what the spec's roster uses, but no caller exists yet to prove the
   two namespaces line up. If a caller passes session ids instead, every
   handoff goes stale after 30 seconds.
5. **`STALE_GRACE = 30.0` seconds.** Picked, not measured. I did not time how
   long a new desk takes to appear in the deck's live set.
6. **The redaction patterns are heuristics.** They catch the shapes I could
   think of. I did not test them against a corpus of real credentials, and a
   novel token format will pass through.
7. **`PHONE_LIMIT = 600` characters / 12 lines fits a lock screen.** Chosen
   from the "under ~8 short lines" rule in the working agreement, not measured
   on a device.
8. **`screencapture` behaviour without Screen Recording permission.** Described
   from memory of macOS behaviour; not verified on this Mac.

## What is not built here

Wiring, on purpose — four other agents are in those files right now. There is
no HTTP endpoint, no board card, no WhatsApp send, no `PreToolUse` integration,
no bus event written to `events.jsonl` when a handoff is raised or resolved,
and no reply parser that turns `done 87mp` from a WhatsApp message into a
`resolve()` call. This module exposes clean, mostly pure functions and stops.
