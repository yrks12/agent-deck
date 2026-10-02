# The Mac bridge

The Agent Deck Mac app is the bridge that lets desks on the server also work on the
owner's own Mac: shell, files, open, optional screenshot. The Mac **dials out** —
no VPN, no SSH, no port-forward, no inbound socket. It registers with the deck,
then long-polls it for jobs. Desks enqueue jobs through their `mac` MCP tools.

**Say it plainly:** *whoever controls the deck server can run code on every Mac
that has access turned on.* The Mac cannot tell a desk's request from a forged
one, because the desks run on that server. In `Full access` that is full control
of the user's account; in `Ask me` it is bounded by the Seatbelt profile, the
chosen folders and "Never touch". See [Security review](#security-review).

Code of record:

| Part | File |
|---|---|
| Store + state machine (MB2) | `server/mac_nodes.py` |
| Wire, node + owner routes (MB1) | `server/mac_api.py` |
| Desk tools, MCP server `mac` (MB3) | `server/mac_mcp.py` |
| Mac side (MB5) | `macos/Sources/DeckKit/MacBridge/` |

---

## MB1. Wire — node routes (`/v1`, own `APIRouter`)

All routes need the `/v1` bearer (`AGENT_DECK_TOKEN`). The Mac's own routes also
need `X-Deck-Node: <node_id>.<node_secret>`; the server stores `sha256(secret)`
only. Missing, wrong, or another Mac's header → `401 unknown_node`. Every refusal
is `{"ok": false, "reason": "<slug>", "detail": "<sentence>"}`.

**There is no HTTP route that creates a job.** Only a desk's `mac` MCP process on
the box can enqueue (`mac_nodes.Store.enqueue`). A leaked bearer token alone
therefore cannot run code on any Mac. `tests/test_mac_api.py` pins the exact
route set below.

| Method | Path | Auth |
|---|---|---|
| `POST` | `/v1/nodes` | bearer (+ optional `X-Deck-Node`) |
| `POST` | `/v1/nodes/{node_id}/poll` | bearer + node |
| `POST` | `/v1/nodes/{node_id}/jobs/{job_id}/events` | bearer + node |
| `POST` | `/v1/nodes/{node_id}/grants` | bearer + node |
| `GET` | `/v1/nodes` | bearer |
| `PATCH` | `/v1/nodes/{node_id}` | bearer |
| `DELETE` | `/v1/nodes/{node_id}` | bearer |
| `GET` | `/v1/nodes/{node_id}/jobs?limit=50` | bearer |

### `POST /v1/nodes` — register or refresh

```json
{"machine_id": "<UUID the app generated once, stored in Application Support>",
 "name": "Sam's MacBook Pro", "os": "macOS 26.0 (25A354)", "app_version": "1.5.0",
 "capabilities": ["run","read","write","list","open","screenshot"],
 "mode": "ask" | "full" | "paused"}
```

- New `machine_id`, **or** a known one without a valid `X-Deck-Node` (re-pair: the
  old secret stops working) → `201 {"node_id":"mac_<12hex>","node_secret":"<43 urlsafe>","name","primary"}`,
  and one owner-thread line from `deck`: *"A Mac connected as \*<name>\*"* /
  *"\*<name>\* re-paired"*.
- Known `machine_id` with a valid header → `200` with the same body minus
  `node_secret`. No owner line.
- The first Mac ever registered is `primary: true`; later ones never are
  automatically.
- `400 bad_input`: missing `machine_id` / `name`, `name` over 64 characters or
  with control characters, an unknown capability or mode.

### `POST /v1/nodes/{node_id}/poll` — long-poll claim, also the heartbeat

```json
{"wait": 25, "free_slots": 4, "running": ["mj_…"], "mode": "ask" | "full" | "paused",
 "grants": {"atlas": {"state": "hour" | "always" | "denied", "until": 1788222702.0 | null}}}
```

→ `200 {"jobs": [Job…], "cancel": ["mj_…"], "server_ts": float}`.

- Returns **at once** if anything is pending; otherwise waits up to `wait`
  seconds (clamped 0–25) and wakes early when a job for this Mac is queued.
- Hands out at most `free_slots` jobs, marking each `claimed`, and never more
  than 4 active per Mac or 2 per desk (a desk's 3rd job stays queued).
- `cancel` lists jobs the desk cancelled, and any id in `running` the deck has
  already settled (lost, expired, removed) — kill those now.
- `mode: "paused"` is a heartbeat that claims nothing.
- `grants` is informational (for the desk's `status`); **the Mac enforces**.
- Every poll by a Mac that is not paused is "back online": see MB3.

**Job**, as delivered:

```json
{"id": "mj_<12hex>", "desk": "atlas",
 "kind": "run" | "read" | "write" | "list" | "open" | "screenshot",
 "args": {…}, "timeout_s": 120, "background": false, "created_at": 1788222702.0}
```

`args` by kind:

| kind | args |
|---|---|
| `run` | `{"command": str ≤16384, "cwd": str\|null, "env": {str: str} ≤32 keys}` — `cwd` absolute or `~`-prefixed, never spliced into the command |
| `read` | `{"path": str, "offset": int≥0, "length": int, "encoding": "text"\|"base64"}` — `length` 1..64 000 for text, up to 5 MiB for base64 |
| `write` | `{"path", "content", "encoding": "text"\|"base64", "mode": "overwrite"\|"append"\|"create", "make_dirs": bool}` — decoded content ≤ 5 MiB |
| `list` | `{"path": str, "hidden": bool}` |
| `open` | `{"target": str}` |
| `screenshot` | `{}` |

`timeout_s` is 1..600 (default 120).

### `POST /v1/nodes/{node_id}/jobs/{job_id}/events` — the Mac reports

```json
{"events": [{"seq": 1, "type": "awaiting_grant"},
            {"seq": 2, "type": "started", "data": {"pid": 4123, "cwd": "/Users/y/w", "sandboxed": true}},
            {"seq": 3, "type": "stdout", "data": "ProductName:\tmacOS\n"},
            {"seq": 4, "type": "stderr", "data": "…"},
            {"seq": 5, "type": "result", "data": {
                "state": "done" | "failed" | "cancelled" | "timed_out" | "refused",
                "exit": 0 | null, "signal": null | "TERM" | "KILL",
                "reason": null | "<slug>", "detail": "", "duration_ms": 812,
                "payload": {…kind-specific…}}}]}
```

→ `200 {"ok": true, "cancel": bool}` — `cancel: true` means the desk cancelled;
kill now.

- `seq` is a positive integer, increasing per job. A `seq` already seen is
  ignored, so retries are safe (a retried `result` answers `200`).
- A stdout/stderr chunk is at most 65 536 characters.
- `404 unknown_job`; `403 not_your_job` if the job is another Mac's;
  `409 job_settled` for anything new after a `result`.
- The Mac posts at least every **2 s** while a job is live; an empty `events`
  list is a keepalive. 45 s with nothing → the job is `lost`.

`payload`: `read` → `{"text"|"base64", "size", "offset", "returned", "truncated"}`;
`write` → `{"bytes", "path"}`; `list` → `{"path", "entries": [{"name", "kind":
"dir"|"file"|"link"|"other", "size", "modified"}], "truncated"}`; `screenshot` →
`{"mime": "image/jpeg", "base64", "width", "height"}` (≤ 2 MiB); `run`/`open` → `{}`.

### `POST /v1/nodes/{node_id}/grants` — what the owner tapped

`{"desk": "atlas", "decision": "hour" | "always" | "deny" | "revoke", "until": float | null}`
→ `200 {"ok": true, "told": bool}`. The deck gives the desk one `sender="deck"` line:

| decision | line | wakes the desk |
|---|---|---|
| `hour` | *"[Agent Deck] Sam allowed you to use his Mac (\<name\>) for 1 hour. Retry what you were doing."* | yes (`mac`) |
| `always` | *"[Agent Deck] Sam allowed you to use his Mac (\<name\>) always. Retry what you were doing."* | yes |
| `deny` | *"[Agent Deck] Sam said no to you using his Mac (\<name\>). Do not ask again today; finish without it or tell whoever you report to what you could not do."* | yes |
| `revoke` | *"[Agent Deck] Sam took back your access to his Mac (\<name\>). Do not use it until he allows you again."* | no — queued only |

### Owner routes (bearer only)

- `GET /v1/nodes` → `{"nodes": [{"node_id", "name", "os", "online", "mode",
  "last_seen", "primary", "running"}]}`. Never carries a secret or its hash.
- `PATCH /v1/nodes/{id}` `{"name"?, "primary": true?}` → the node's row. Making
  one primary clears the others.
- `DELETE /v1/nodes/{id}` → `{"ok": true, "refused": [job ids]}`. Its unsettled
  jobs end `refused / unknown_mac`; its secret stops working. If it was primary,
  the oldest remaining Mac becomes primary.
- `GET /v1/nodes/{id}/jobs?limit=50` → `{"jobs": [{"id", "desk", "kind",
  "summary", "state", "exit", "reason", "created_at", "claimed_at", "started_at",
  "settled_at"}]}`, newest first. The server's audit copy: **no output**.

---

## MB2. Store and state machine (`server/mac_nodes.py`)

Stdlib only, one injected root (the deck uses `BUS_DIR / "mac"`):

- `nodes.json` — every paired Mac (secret as `sha256` only), its offline cards
  and the per-desk `no_mac` cards.
- `jobs/<id>.json` — the job row; `jobs/<id>.stdout`, `jobs/<id>.stderr` — its
  output; `jobs/<id>.payload.json` — its result payload.
- `.lock` — `flock` around every read-modify-write. Every write is
  `atomic.write_*` (temp file + rename, mode 0600).

Two kinds of writer share these files — the deck and each desk's `mac` MCP
process — the same pattern as `decisions.py`. Output files are appended by the
deck only (`record_events`).

| Constant | Value | Means |
|---|---|---|
| `ONLINE_WINDOW` | 40 s | since last poll → online |
| `CLAIM_WAIT` | 10 s | queued, and no poll since it was queued → `expired / mac_not_answering` |
| `LOST_AFTER` | 45 s | claimed/running with no event → `lost` |
| `KEEP_DAYS` | 7 | settled jobs purged on sweep |
| `OUT_KEEP` | 1 MiB | bytes kept per stream: first 64 KiB + the newest bytes; `dropped` counted |
| `PER_NODE_CONCURRENT` / `PER_DESK_CONCURRENT` | 4 / 2 | active jobs |
| `PER_DESK_PER_MIN` | 60 | enqueue rate → `rate_limited` |
| `QUEUE_MAX_PER_NODE` | 32 | queued jobs → `node_busy` |

Job `state`: `queued → claimed → [awaiting_grant] → running → done | failed |
cancelled | timed_out | refused | lost | expired`.

- A job is only ever accepted for a Mac that is online: `enqueue` refuses
  `mac_offline` / `mac_paused` at once (the refusal carries the node row), so
  nothing waits on a machine that is not there.
- A queued job held back only by the per-desk limit keeps waiting while the Mac
  keeps polling; it expires `node_busy` after `CLAIM_WAIT + timeout_s`.
- Cancelling a queued job settles it `cancelled` at once; it is never delivered.

Node selection when a desk names no Mac: the single online Mac; several online
→ the primary; none online → the primary (or only) Mac, so the offline answer
can name it; zero Macs → `no_mac`. A name matches case-insensitively; two Macs
with the same name → `ambiguous_mac`.

Presence: `paused` if the Mac last said it is paused; else `online` within
`ONLINE_WINDOW` of its last poll; else `offline`.

---

## MB3. Desk tools — the parts M1 owns

The desk tools (`mcp__mac__run`, `job`, `cancel`, `read`, `write`, `list`,
`open`, `screenshot`, `status`) live in `server/mac_mcp.py`. The rules the store
and router carry:

**Offline never hangs.** `mac_offline` / `mac_paused` / `no_mac` return in ≤ 2 s
and file **one** owner card — one waiting card per desk + Mac
(`Store.offline_card` / `set_offline_card`), and for `no_mac` one per desk per
24 h (`Store.no_mac_card` / `set_no_mac_card`):

```python
handoff.raise_handoff(handoff.DEFAULT_PATH, agent=desk, kind="other",
    needs=f"Open Agent Deck on {node_name} so {desk} can use it",
    state=f"Nothing ran on the Mac. {desk} wanted to: {summary[:80]}",
    where=node_name)
```

**Back online.** The first poll from a Mac that is not paused pops every stored
offline card for it (under the lock, so overlapping polls cannot both act),
resolves each still-waiting card `done`, and delivers
`handoff.resume_message(card, "done")` to its desk through `tell` (office line +
wake reason `mac`) — exactly once per card. A card the owner already closed
still gets the desk one *"\<name\> is back online. Retry what you were doing."*

---

## Wiring (M3)

```python
app.include_router(mac_api.build_router(
    store=mac_nodes.Store(BUS_DIR / "mac"),
    tell=lambda desk, text: _deliver_or_wake(desk, text, reason="mac"),
    owner_line=lambda text: office.send(office.OWNER_INBOX, text, sender="deck"),
    queue=lambda desk, text: office.send(desk, text, sender="deck"),
))
```

`queue` (office line, no wake) carries the `revoke` line; without it a revoke
is answered `told: false`. `handoffs_path` defaults to `handoff.DEFAULT_PATH`.
`mac_nodes.Store.sweep` runs inside every poll and every enqueue; M3 also calls
it from the existing 1 Hz tick.

---

## Security review

| Threat | Mitigation | Residual |
|---|---|---|
| Prompt injection into a desk (a web page says "run `curl … \| sh` on the Mac") — the likeliest real attack | Ask-mode scope + Seatbelt; "Never touch"; per-desk grants; activity log; in-use indicator + Stop; 600 s max; rate limits | In Full access, none beyond log + Stop. Stated in the confirmation sheet. |
| Compromised server / box | Nothing runs while the app is closed or paused; kill switch; the Mac-side policy is authoritative (the server can't grant); the log is on the Mac, not the server | A compromised server can use any grant any desk holds and forge desk names. Not fixable in this architecture. |
| Leaked bearer token (phone, logs) | No HTTP route creates jobs; node routes also need the per-node secret (stored hashed); a new/re-paired Mac posts an owner-thread line and is never auto-primary except the first | A token holder can register a fake "Mac" and receive what desks send it when targeted by name. |
| Sniffing / MITM on the way | Transport guard: no bridge over plain HTTP to a public host | The owner's WireGuard `http://10.99.0.1` is allowed (private). |
| One desk using another desk's grant | Desk bound in the MCP argv; tools have no desk field | All desks share one Unix user on the box and could write the job files directly. Same stance as `computer_mcp`: a sanctioned road, not a wall. |
| Desk edits its own grant / the policy | Policy + log dirs are non-removable "Never touch" in Ask mode | In Full access an agent can edit or kill anything, including the app. |
| Runaway process (fork bomb, disk fill, 100 % CPU) | Process-group kill on timeout/Stop/Quit; concurrency + rate limits | No CPU/memory/disk quotas on macOS processes. |
| Mac data landing on the server | Output kept ≤ 1 MiB per stream, purged after 7 days; file contents not logged server-side beyond the job result; the owner's audit route carries no output | Anything a desk reads it can repost anywhere. |
| TCC prompt appearing while he is away | `tcc_denied` reason with the exact System Settings path; the Full-access sheet offers Full Disk Access | A job can sit on a prompt until its timeout. |
| Local attackers on the Mac | The bridge opens **no listening socket**; it only dials out | — |

**Deliberately not done:** no GUI control of the Mac (mouse/keys); no clipboard;
no Keychain/password APIs; no `sudo`/admin elevation; no inbound port, relay
service or hole-punching; no execution while the app is quit; no per-command
classifier or allowlist (owner ruling: the controls are scope, log and Stop,
not a gate); no job signing (pointless — the signer would be the server); no
file sync or bulk transfer (5 MiB per call); no CPU/memory quotas; no
Windows/Linux node; no App Store build (Seatbelt-wrapping child processes and
arbitrary shell access are incompatible with the App Sandbox).

---

## Measured (M2, 2026-09-30)

On the box, claude **2.1.286**, a throwaway `claude --bg` session in its own
pretrusted workspace (no owner desk touched), with a stdio probe server that
logs every message it receives. Flags in the desks' order (`--mcp-config`
before the other flags: it is variadic and swallowed the prompt otherwise —
the session died "exit 1 before init").

| Question | Result |
|---|---|
| Does a long MCP tool call time out? | **No, not at 660 s.** A `tools/call` that answered after 660 s came back to the model as a normal result (`slept 660s`): no cancel sent, no error, not moved to the background. So the MB3 max (`run` 600 s; the tool's hard cap `timeout_s + 90` = 690 s) stands. The CLI binary puts its stdio *idle* limit at 1 800 000 ms (30 min, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`); a per-server `timeout` in the config, or `MCP_TOOL_TIMEOUT`, would lower the hard limit — the deck sets neither. |
| Does Esc send `notifications/cancelled`? | **Yes.** Esc in `claude attach` → `{"method":"notifications/cancelled","params":{"requestId":2,"reason":"AbortError: user-cancel"}}` reached the server **74 ms** later, while the call was in flight. The screen showed "Interrupted". |
| What does `claude stop` do to the server? | **SIGTERM, nothing first.** No `notifications/cancelled`, no stdin EOF: the probe's SIGTERM handler fired 0.6 s after the call started. |
| What does the CLI send first? | A `server/discover` request (with an id) before `initialize`; protocol `2025-11-25`. The server answers it `-32601` and carries on. |

What `server/mac_mcp.py` does with that:

- stdin is read on the main thread and each `tools/call` runs on its own
  thread, so a cancel arriving mid-call reaches the job at once
  (`Store.request_cancel`; the Mac's next events POST answers `cancel: true`).
  A cancelled call is never answered, as MCP requires.
- stdin EOF **and SIGTERM** cancel every foreground job this process started.
  A `background: true` job is meant to outlive the call and is left running.
- Every call is bounded: claim (the store's `CLAIM_WAIT` expiry) → grant
  ≤ 60 s → run ≤ `timeout_s + 15` s, and never past `timeout_s + 90` s.
- `mac_offline` / `mac_paused` / `no_mac` answer without waiting and file one
  card (per desk + Mac while it waits; `no_mac` once per desk per 24 h). The
  `no_mac` detail tells the desk that an owner who installed Agent Deck from
  the App Store needs the direct-download version of the app: the App Store
  build cannot give desks a Mac.
