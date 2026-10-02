# The box — running Shaliach when the laptop is shut

**Date:** 2026-09-02
**Artefacts:** `deploy/agentdeck.service`, `deploy/install-deck.sh`, `server/deploy_check.py`
**Tested by:** `tests/test_deploy_artefacts.py`, `tests/test_deploy_check.py`,
`tests/test_deploy_one_script.py`
**Status:** run. The deck is live on `10.99.0.1:7789` and the installer has been
re-run against it. Where a line below says *measured*, it was measured on that
box on 2026-09-02; where it says *assumed*, nobody has proved it.

## The gap this closes

Every routine, every watched session, every scheduled delivery runs inside a
process on Sam's Mac. Close the lid and all of it stops — silently, because
nothing is left running to notice. The box at `10.99.0.1` is already up (18
days) and already runs other_app's engine as `deckop.service`. Putting the deck
there is the difference between "the routine fires" and "the routine fires if
the laptop happens to be open".

## Updating the code: always `bin/deploy-box`

After the first install, code reaches the box in one way only:

```bash
DECK_BOX=deckop@10.99.0.1 bin/deploy-box --commit origin/main
```

Never rsync or copy a checkout over `/opt/agent-deck` by hand. Two sessions
doing that a minute apart rolled the box back to older code (measured
2026-09-30). The script:

1. refuses a dirty tree, and ships exactly the commit you name (`git archive`);
2. takes `flock` on `/opt/agent-deck/.deploy.lock` for the whole run, so a
   second deploy is refused, not interleaved;
3. refuses unless the commit is a descendant of the one recorded in
   `/opt/agent-deck/DEPLOYED`. To go back, pass `--force-rollback`;
4. backs up `server/ hooks/ bin/ docs/ web/ deploy/ VERSION` to
   `/opt/agent-deck/.deploy-backups/` (keeps 5), then rsyncs them;
5. restarts `agentdeck` and waits for `/healthz`. If it does not answer, the
   backup is restored and `DEPLOYED` is left as it was;
6. writes `DEPLOYED`: the sha, time, who, and a fingerprint of the shipped tree.

If someone changes the tree by hand anyway, the deck prints a `DEPLOY GUARD`
line at every start (`journalctl -u agentdeck`). deckdoctor also turns its
`code_tree` check red and says so once. Both come from `server/deploy_guard.py`.
Neither of them changes anything.

## What the deck is *not* allowed to disturb

`deckop.service` serves its own dashboard on **7788**. The deck takes **7789**
and never touches 7788 — not to probe it into use, not to "temporarily" borrow
it. `install-deck.sh` refuses to run rather than fight for that port, and
`preflight()` treats 7788 as reserved even during the seconds when a
`systemctl restart deckop` leaves it genuinely free.

## The commands, in order

Run them one at a time. Each line below says what it *proves* — a command whose
proof you did not see is a step that did not happen.

### 1. Reach the box

```bash
ssh deckop@10.99.0.1 'uptime; systemctl is-active deckop.service'
```

**Proves:** the WireGuard tunnel is up from the Mac (10.99.0.4), the login
works, and other_app is running — the thing you must not break.

### 2. Put the code there

```bash
ssh deckop@10.99.0.1 'sudo mkdir -p /opt/agent-deck && sudo chown deckop:deckop /opt/agent-deck'
ssh deckop@10.99.0.1 'git clone -b feat/agent-surface <repo-url> /opt/agent-deck'
```

**Proves:** the repo is at the exact path the unit's `ExecStart` names. If you
clone somewhere else, `install-deck.sh` stops and tells you — it greps the unit
for its own venv path rather than starting the wrong interpreter.

### 3. Install — the one command

```bash
ssh deckop@10.99.0.1 'sudo /opt/agent-deck/deploy/install-deck.sh'
```

That is the whole install. There is no second step, and nothing below has to be
done by hand. **Phases, in order — it stops at the first failure and says which:**

1. **Refusals.** 7788 is held by `deckop.service` and by nothing else; 7789 is
   free or already ours.
2. **Base tooling.** `python3-venv`, `iproute2` (`ss`), `openssl`, `curl`,
   `git`, `ufw`, `nodejs` — swept out of what the code actually shells out to
   (`server/office.py` runs `git rev-parse`, every hook runs under node). They
   are *checked* first, so a box that already has them never sees `apt` at all,
   and nothing is ever `upgrade`d.
3. **The `claude` CLI**, installed **as `deckop`** if absent. As root the
   credentials land in `/root`, where the service user cannot read them, and
   every spawn dies at the login prompt with the board showing WORKING.
   `codex` and `opencode` are **deliberately not installed** — see below.
4. **The login gate.** No credential for `deckop` and the script stops here,
   before enabling anything, and prints
   `sudo -u deckop ~/.local/bin/claude setup-token`. A script cannot log in and
   must not pretend it did.
5. **The token** at `/etc/agent-deck/agentdeck.env`, generated once, `0600`,
   root-owned.
6. **The venv**, with `fastapi` and `uvicorn` **pinned**.
7. **The firewall:** `ufw allow in on wg0 to any port 7789 proto tcp`. On `wg0`
   only — this box already publishes 80 and 443 for another site, and a bare
   `ufw allow 7789` would put the whole board, transcripts and spawn controls
   included, beside them. Never opened to the public internet.
8. **Docker** — only with `--with-docker`. See below.
9. **The unit**, `daemon-reload`, **preflight**, `enable`, `restart`.
10. **Verification.** Six checks against the service that is now running.

### 3a. What the verification actually proves

Each line prints `PASS` or `FAIL` with what it got, and a `FAIL` makes the whole
command exit non-zero. A step whose proof you did not see is a step that did not
happen.

| Check | What it would have caught |
|---|---|
| `/api/state` → 200 | the unit is green and the app never bound |
| `/v1/agents` → 200 **with the token**, and *not* 503 | no `AGENT_DECK_TOKEN`: every `/v1` route answers `auth_not_configured` and the box looks healthy while doing nothing |
| `cc-approve.js` round trip, read for its **reason string** | the hook posting into a closed socket. It always exits 0 and always prints a decision — that is deliberate, so a broken deck never hangs a session, and it means "the hook ran" proves nothing. `deck unreachable` is the failure; a rule verdict is the pass |
| `approval.deck_url()` matches the unit's `DECK_URL` | desks hired on the box reporting somewhere that is not the deck |
| `claude --version` as `deckop` | a CLI root can run and the service user cannot |
| `deckop.service` still active | the one way this script can do real damage |

### 3b. `DECK_URL`, and the defect that made this phase exist

**Measured on 2026-09-02, with the deck already live and green:**

```
DECK_URL=http://127.0.0.1:7789 -> "permissionDecisionReason":"deck unreachable"
DECK_URL=http://10.99.0.1:7789 -> "permissionDecisionReason":"no rule"
```

`server/approval.py` writes `DECK_URL` into the `--settings` file it generates
for every desk the deck hires. It read `--port` off the daemon's argv (7789,
correct) and hard-coded the host to loopback — but the deck binds `10.99.0.1`
and *only* that, so `curl http://127.0.0.1:7789/api/state` on the box is
`Failed to connect`. Every approval, permission request, elicitation and
compaction from a desk that box hired went nowhere, and the desk sat on a modal
nobody could clear, with the card still green.

It now reads `--host` as well, and `deploy/agentdeck.service` states
`Environment=DECK_URL=http://10.99.0.1:7789` outright as the belt to that brace.

### 3c. Why `codex` and `opencode` are not installed

`server/pretrust.py` returns `engine_not_covered` for both: the deck cannot
write either one's trust gate, so a desk opens and stalls on a prompt nothing
outside the process can answer. `server/spawn.py::spawn_background` raises
`background_unsupported` for any engine but `claude`, and the only other start
path is `spawn_terminal`, which needs `osascript` — macOS only. There is no code
path on this box that can drive either one. A binary on `PATH` that the board
can offer and never start is worse than an absent one. Install them when
pretrust covers them, not before.

### 3d. Docker — off by default, and why

`sudo /opt/agent-deck/deploy/install-deck.sh --with-docker`

Docker is not installed unless you pass that flag, because `dockerd` inserts its
own chains into `iptables` **ahead of ufw's**: a published container port is
then reachable on every interface regardless of the deny-incoming default — on a
box whose 80 and 443 already face the internet. It also enables `ip_forward` and
adds a `172.17.0.0/16` bridge to a machine that is a WireGuard endpoint. That is
a change to this box's network posture and it is Sam's call, not a side effect
of installing a dashboard.

With the flag it installs `docker.io`, `docker-buildx`, `docker-compose-v2` from
Ubuntu's own archive (no third-party apt source) and writes
`/etc/docker/daemon.json` with `{"ip": "127.0.0.1"}`, so `-p 5900:5900` binds
loopback rather than `0.0.0.0`. An existing `daemon.json` is never overwritten —
if one is there, check it yourself.

### 4. Prove it survives a re-run

```bash
ssh deckop@10.99.0.1 'sudo md5sum /etc/agent-deck/agentdeck.env'
ssh deckop@10.99.0.1 'sudo /opt/agent-deck/deploy/install-deck.sh'
ssh deckop@10.99.0.1 'sudo md5sum /etc/agent-deck/agentdeck.env'
```

**Proves:** the two hashes match, so the second run did **not** rotate the
token. A rotated token presents as "the deck stopped answering" on the Mac,
with nothing in any log saying why. This is the failure the guard exists for.

### 5. Prove it answers *you*, over the tunnel

```bash
TOKEN=$(ssh deckop@10.99.0.1 'sudo cat /etc/agent-deck/agentdeck.env' | cut -d= -f2)
curl -fsS http://10.99.0.1:7789/api/state | head -c 200
curl -fsS -H "Authorization: Bearer $TOKEN" http://10.99.0.1:7789/v1/agents
```

**Proves:** the board is reachable from the Mac, and the client surface is
*open* rather than answering 503. Without a token `/v1` returns
`auth_not_configured` and the box looks healthy while doing nothing useful —
which is exactly why a missing token is a preflight blocker and not a warning.

Never paste `$TOKEN` into WhatsApp, an issue, or a session transcript.

### 6. Prove `claude` is really logged in

```bash
ssh deckop@10.99.0.1 'claude -p "reply with the single word ok"'
```

**Proves** what preflight cannot. Preflight checks for a stored credentials
file, which is a **proxy** — it means somebody logged in on that box once, not
that the session is still live. This command spends a few tokens and is the
only real proof. Do it once, here, before you trust the box to spawn desks.

### 7. Prove it survives a reboot

```bash
ssh deckop@10.99.0.1 'sudo systemctl reboot'
# wait ~60s
curl -fsS http://10.99.0.1:7789/api/state >/dev/null && echo "deck is back"
curl -fsS http://10.99.0.1:7788/ >/dev/null && echo "other_app is back"
```

**Proves:** the unit is genuinely enabled at boot, it binds the WireGuard
address *after* the tunnel comes up, and other_app came back beside it. This is
the actual acceptance test for "routines fire when the laptop is shut" —
everything before it only proves the process started once.

## Rollback

Nothing here modifies other_app, so a rollback restores nothing — it only removes.

```bash
# stop and un-enable, leaving the token in place for a retry
ssh deckop@10.99.0.1 'sudo systemctl disable --now agentdeck.service'

# remove the unit
ssh deckop@10.99.0.1 'sudo rm -f /etc/systemd/system/agentdeck.service && sudo systemctl daemon-reload'

# confirm the rollback did not touch the neighbour
ssh deckop@10.99.0.1 'systemctl is-active deckop.service'
```

Stop after the first command if you intend to retry: it frees 7789 and leaves
the token, so re-running the installer is a one-step recovery.

Only if you are abandoning the box entirely:

```bash
ssh deckop@10.99.0.1 'sudo rm -rf /etc/agent-deck /opt/agent-deck'
```

That **destroys the token**. Every client holding it — the Mac, the iOS app —
must be given the new one after a future re-install.

## What was assumed, not measured

Nobody touched the box to write any of this. These are assumptions, and each is
a place the runbook can fail on first contact:

| Assumed | If it is wrong |
|---|---|
| the SSH user is `deckop` and it has `sudo` | step 1 fails immediately — cheapest possible failure, which is why it is step 1 |
| the WireGuard unit is `wg-quick@wg0.service` | the unit's `After=` matches nothing; it still starts, and `Restart=always` retries until the address exists |
| `10.99.0.1` is an address *on* the box, bindable locally | uvicorn exits with `EADDRNOTAVAIL`; visible in `journalctl -u agentdeck` |
| `deckop` home is `/home/deckop` | the bus directory and the `claude` PATH entry are both wrong; preflight blocks on the bus directory |
| `ss` (iproute2), `openssl`, `curl`, `git` and `python3-venv` are installed | the installer fails at that line; `set -euo pipefail` makes it stop rather than continue |
| the box has outbound internet for `pip` | venv build fails; preflight never runs |
| 7789 is free | the installer refuses and names the holder |
| the init system is systemd | none of this applies at all |
| `deckop.service` is the correct unit name for other_app's engine | the 7788 guard cannot recognise the legitimate holder and refuses a good box — annoying, not dangerous |

Two further things are **out of scope and not done by these artefacts**:

- **`hooks/cc-bus.js` and `cc-office.js` are still not in the box's own
  `~/.claude/settings.json`** (measured: it contains `theme` and `tui` and
  nothing else). The four hooks the deck *hires* with — `cc-approve`,
  `cc-permission`, `cc-elicit`, `cc-compact` — are covered, because
  `server/approval.py` generates a `--settings` file for each desk and that file
  now carries the right `DECK_URL`. What is not covered is a session somebody
  starts on the box by hand: it emits no bus events, so the board will not show
  it. Registering there would also put other_app's own sessions under the deck's
  hooks, which is a decision about another product's runtime and was not taken
  here.
- **Agents spawned on the box run as `deckop`, with that user's `claude`
  credentials and that user's filesystem access.** That has never been
  exercised. Expect the first spawn from the box to need a round of fixing.

## Why 7789, in one line

Because 7788 is other_app's and the cheapest way to never have that argument
again is to not start it.

## Health

**Artefacts:** `bin/deckdoctor`, `deploy/deckdoctor.service`, `deploy/deckdoctor.timer`,
`deploy/journald-cap.conf`, `deploy/agent-deck-log.logrotate` (all installed by
`install-deck.sh`). **Tested by:** `tests/test_deckdoctor.py` (hermetic).

### Why

A full disk or an expired login kills every desk at once and the board says
nothing. Measured 2026-09-30: `/` at 71% with 47 GB free, falling by about
21 GB/day on average (peak 39 GB), and `claude` login expiry has done this before.

### What it does

Every 10 minutes it prints one JSON line and exits 0 (green) or 1 (red):
`{"ok", "checks": {auth, disk_pct, daemon, stuck_messages, desks_asleep}, "alert"}`.

| Check | Red when |
|---|---|
| `disk_pct` | `/` is at or above `DISK_RED_PCT` (default 85) |
| `auth` | `claude auth status` is not logged in, or the live `claude -p` probe (once per 6 h) fails. `auth status` alone is not enough: an expired token still reads logged in |
| `daemon` | `GET $DECK_URL/api/state` fails |
| `stuck_messages` | an owner/routine/deck/engineer message to a desk is unacknowledged after 5 min (last 24 h only) |
| `desks_asleep` | never; informational count of `ASLEEP`/`OFFLINE` desks |

Red is spoken **once per check per 6 h**: one line into the chief's thread as
`deck` (marked `doctor`, so it never counts itself as stuck) and one WhatsApp when
`DECK_WA_URL` or `DECK_WA_SEND` reaches a bridge (the box has none today, so the
thread line is the alarm; the JSON says `whatsapp: "skipped: ..."`). A check that
recovers forgets, so its next failure speaks again. It never stops or deletes
anything, and never prints the deck token.

### Dry run (nothing posts)

```bash
DISK_RED_PCT=1 DECKDOCTOR_AUTH_PROBE=0 DECKDOCTOR_ALERT=file:/tmp/dd-alert.jsonl \
  DECKDOCTOR_STATE=/tmp/dd-state.json /opt/agent-deck/.venv/bin/python /opt/agent-deck/bin/deckdoctor
```

`DECKDOCTOR_ALERT=off` prints red and posts nothing at all. Run twice: the second
run must add no line (the 6 h rule).

### What it cannot fix: the disk eater is not the deck's

`/opt/studio/storage/assets` grew about 63 GB in 3 days (16 GB on 09-28, 39 GB on
09-29). Nothing under `/opt/studio` is ours: the doctor alarms, and only Sam
decides to stop or delete. Two things on this box also fill the disk and are not
the studio: the `deckop` watchdog logs `cleanup-failed:<id>:CalledProcessError` for
each of ~5,400 dead sessions on **every** tick (about 0.8 GB/day of syslog; git
says `fatal: '<workdir>' is not a working tree` because the worktrees are gone, so
the handles are never retired), and `/var/log/syslog` is only rotated weekly.

### Rollback

```bash
sudo systemctl disable --now deckdoctor.timer
sudo rm /etc/systemd/system/deckdoctor.{service,timer} /etc/logrotate.d/agent-deck \
        /etc/systemd/journald.conf.d/agent-deck-cap.conf
sudo systemctl daemon-reload && sudo systemctl restart systemd-journald
```

State left behind: `~/.claude/agent-bus/deckdoctor-state.json` (harmless; delete it
to reset the 6 h memory). Re-running `install-deck.sh` is safe: files are replaced
only when they differ.
