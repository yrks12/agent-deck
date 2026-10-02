# Wake — a sleeping desk answers, as itself

**Contract:** K3 in `docs/plans/2026-09-30-overhaul.md`.
**Code:** `server/wake.py` (`ensure_awake`, `is_asleep`), `server/spawn.py`
(`resume_background`), wired in `server/app.py`.
**Tested by:** `tests/test_wake.py`, `tests/test_wake_resumes_same_session.py`;
live: `tests/live/test_overhaul_always_on.py` A-1..A-3.

## Why

Claude Code's background daemon retires an idle desk after about an hour:

```
[2026-09-28T07:52:27Z] [bg] bg retire 25adf776: idle-prompt, idle 61m
```

It skips the retire only for a session with work in flight, a session cron or a
routine, and there is no documented setting to turn it off. Before this, the
deck never brought a retired desk back: a message was queued for a socket that
no longer existed and waited until somebody restarted the desk by hand — and
that restart was a *new* session fed a capped replay of the conversation.

## What happens now

Every delivery that finds no live socket calls `wake.ensure_awake(name, reason=…)`:

| Door | Reason | Blocking? |
|---|---|---|
| `/v1` surface delivery (`app._deliver_or_wake`): his messages, the engineer's, deck notices, group fan-out | `owner_message` | no — runs on a thread; the POST returns at once |
| `app._deliver_routine` | `routine` | yes — the run records `woken` / `restarted` / `queued (<why>)` |
| deck MCP `message_desk`, decision answers, call start (later slices) | `peer_message`, `decision_answer`, `call` | caller's choice |
| the harvester's routine and hire receipts (`app._deck_tell`) | `deck_receipt` | no — runs on a thread |

Outcomes:

| State | Meaning |
|---|---|
| `live` | A session is seated, or the deck woke this desk under 30 s ago. Nothing done. |
| `woken` | `claude --bg --resume <last session id> "<seed>"` — **same session id, same memory**. No replay, no "This desk was restarted" line. |
| `restarted` | No resumable session (no job, no transcript, or the CLI refused the resume): today's `spawn.start` — replay plus the restart notice. Never opens a Terminal window: on the Mac it refuses with `terminal_channel` instead. |
| `refused` | No session can run. `detail` opens with the slug: `oauth_expired`, `disk_full`, `not_a_desk`, `terminal_channel`, or what `spawn.start` refused with. The message stays queued. |

Every outcome except `live` (and non-desk names) is appended to
`~/.claude/agent-bus/events.jsonl`:

```json
{"type": "wake", "event": "wake", "desk": "wake-probe", "state": "woken",
 "session_id": "de8b1457-…", "reason": "owner_message", "detail": "", "ts": 1790744168.4}
```

`GET /v1/agents` shows a desk with no live session and a resumable one as
`ASLEEP` (via `Surface(asleep=wake.is_asleep)`); `OFFLINE` now means never
started or not resumable.

## Measured, not assumed (box, claude 2.1.285, throwaway desk `wake-probe`)

1. **Flags fork.** `claude --bg --resume <sid> --name … --settings … <note>`
   printed *"background session de8b1457 keeps its own saved options, so the
   flags you passed started a copy as b3f54a1c"* — a new session id. With **no
   flags** it printed *"woke session de8b1457 with its saved options (--name,
   --settings, --mcp-config, --append-system-prompt, --model,
   --permission-mode)"* and the board showed the same id afterwards. So a wake
   passes only the session id and the seed. The consequence: a woken desk keeps
   the brief it was started with; a new brief still needs a start.
2. **The office hook does not deliver on the woken turn.** The queued message
   stayed `undelivered` and the desk answered the bare wake note. The hook finds
   mail by the session's name in `office.json`, which the collector had not yet
   published. So the waiting mail rides **in the seed**, framed exactly as the
   hook frames it (`office.attribute`), up to `SEED_MAX` characters; carried
   messages are acked, the rest stay queued for the next turn.
3. **Where the last session comes from.** Once retired, a session is gone from
   `office.json` and from `GET /v1/agents`. The CLI's job registry
   (`~/.claude/jobs/<short>/state.json`: `name`, `sessionId`, `cwd`,
   `createdAt`) is the record that survives. The job resumed is the one whose
   transcript was written **last** — not the one created last: ordering by
   creation woke a copy made and stopped an hour earlier instead of the desk's
   own session.
4. **Never resume a running session.** `--resume` on a session that is still
   running printed *"session 4ccfd598 is already running in the background, so
   this started a copy as 89bad41e"*. The board lags the CLI by a tick (and by a
   whole deck restart), so before resuming, the waker checks the CLI's own
   `~/.claude/sessions/<pid>.json` for that session with a live pid.
5. **A real idle-retire resumes the same way.** `bg retire de8b1457: settled,
   idle 61m` at 05:56:50; the flagless resume printed *"woke session de8b1457
   with its saved options"* and the desk answered with the codeword it had been
   given an hour earlier.
6. **Claude's own `SendMessage` cannot reach a sleeping desk.** To a stopped
   session it returned `{"success":false,"message":"No agent named 'wake-sink'
   is reachable."}`; after the same session was resumed it returned
   `success:true`. It errors, it does not queue — which is why peers need the
   deck's `message_desk` (K5), which wakes.

A pin reads the installed CLI bundle for the wording above
(`test_the_installed_cli_still_resumes_the_way_it_was_measured`), so a CLI
update that changes resume behaviour turns the suite red instead of silently
turning wakes into forks.

## Limits

- A resumed session re-reads its whole context when the prompt cache has
  expired; a long desk costs noticeably per wake.
- The desk cap (`hire.MAX_LIVE`) is a hiring rule and is not applied to wakes.
- If the hook *does* win the race on some turn, a carried message can be shown
  to the desk twice (seed and hook). It is never lost.
