# The vault — what the promise actually is

**Date:** 2026-09-01
**Code:** `server/vault.py`, tested in `tests/test_vault.py`
**Status:** functions only. Nothing is wired to `spawn.py`, `hire.py` or the board yet.

## The claim we are copying, and the one we can make

xAI's Grok Bot shows a field that takes a live credential — *"Stripe live secret
key — Live sk_live key so VideoVend can create a £99 payment link. Test keys will
not be used."* — with a **Save securely** button and the sentence **"Stored
securely, never shown to your Bot."**

That sentence is true for them and false for us, and the difference is
architectural, not a matter of effort.

| | Grok Bot | Shaliach |
|---|---|---|
| How the agent acts | calls tools through a broker | runs a real shell in a real cwd |
| Where the credential goes | into the broker, never the agent | into the spawned process's environment |
| Can the agent read it? | no — it never has it | **yes** — `env` prints it |

An agent that can run `env`, `cat`, or any program at all can read every
environment variable it was started with. There is no arrangement of files,
permissions or wrappers that changes this while the agent still runs arbitrary
commands, and the only alternative — building a broker so the agent never holds
the credential — is a different product, not a hardening of this one.

**So `server/vault.py` does not say "never shown to the agent", and no UI built
on it may say it either.** The docstring states this at the top, deliberately,
because a lie about a credential is worse than having no vault.

## The three things it does promise

1. **The value is never in a repo, a prompt, a charter or a transcript.** It
   lives in exactly one file — `~/.claude/agent-bus/vault.json`, mode `0600`,
   opened at that mode rather than chmod'ed afterwards so no world-readable
   window exists. It is not in `roster.json`, not in a desk's `mission` or
   `charter`, and it is never passed on a command line, where `ps` would show it
   to every process on the Mac.
2. **It reaches only the desks explicitly granted it.** `env_for(path, desk)` is
   the single function in the module that returns a value. A desk with no grant
   gets `{}` — nothing, not a blank. The grant list *is* the security model.
3. **It is redacted from everything a human or another agent reads.**
   `redactor(path)` returns a function that replaces each stored value with
   `[redacted STRIPE_LIVE_KEY]` — naming the key, showing none of it. Run it over
   harvested output, WhatsApp messages, handoff evidence and board fields.

`SecretMeta` has no `value` field and an explicit `__repr__`, so the object the
board and the API hand around cannot carry or print one even if a field is added
later by mistake.

## The threat model, stated plainly

**Defended:**

- a key pasted into a commit, a charter, a mission string or a system prompt;
- a key in `ps aux` output — it is never an argv element;
- a key in a screenshot, a phone message, or handoff evidence — `redactor`;
- a key in a transcript that another agent later reads, or in a log the deck
  files — `redactor`, provided the caller applies it;
- a key readable by another user account on the Mac — mode `0600`;
- a key that outlives its use — `forget()` erases the value from disk, and a
  rotation via `put()` replaces in place rather than keeping history.

**Not defended, and not claimed:**

- **The granted desk itself.** It can read its own environment, print the key,
  write it to a file, or send it anywhere. Granting a desk a key is trusting
  that desk with the key. Keep grant lists to one or two names.
- **Anything already spawned.** `revoke()` takes effect at the next spawn. A
  running desk still has the value in its environment and nothing can reach in
  and remove it — **revoking a live key means rotating it too.**
- **Disk-level attackers.** The file is plaintext JSON. `0600` stops another
  user on this Mac; it does not stop root, a stolen unlocked laptop, a Time
  Machine backup, or anything that can read the home directory. There is no
  encryption at rest and no Keychain integration.
- **A caller that forgets the redactor.** Redaction is opt-in per call site.
  Nothing forces `notify.py` or the harvester to use it — that wiring is the
  next task and until it exists, promise 3 is a capability rather than a fact.
- **Memory and core dumps.** The value is an ordinary Python string and an
  ordinary env var.

## Expiry, which shipped alongside this

The same screenshot shows an approval card reading **"Auto-review Paused This
Action"** with status **"Expired"**. `server/expiry.py` adds that to both pending
queues on one shared window (`DEFAULT_TTL = 4 hours`, justified in the file).

The load-bearing rule, and the one with a test named after it: **expiry is a
removal, never a decision.** When an ask times out, no rule is written and
`autoreview.evaluate` still answers `ask` for the same call — the agent's own
permission prompt is still on its screen, unanswered. An expired ask denies by
omission. Likewise `expired` is deliberately not in `handoff.OUTCOMES`: a £99
payment nobody confirmed did not happen, and must never read as `done`.

## Assumed rather than measured

- **Four hours** is a judgement about Sam's attention, not a measurement. No
  data was gathered on how long a real ask sits unanswered; if the board shows
  things expiring that he meant to answer, this is the number to change, and
  changing it in `server/expiry.py` moves both queues together.
- **Environment variables are how a desk receives a credential.** Assumed from
  how every CLI tool this project shells out to reads its config. Not verified
  against any specific tool's precedence rules (a tool that prefers a config
  file over `$STRIPE_LIVE_KEY` would silently ignore the grant).
- **The redactor's exact-substring approach is enough.** It catches the value
  verbatim. It will *not* catch a value that has been transformed on the way
  out — base64'd, URL-encoded, split across lines, or partially printed. The
  shape rules in `asking.redact` and `handoff.redact` are the complement that
  catches unknown keys by their form; neither is complete, and they are
  deliberately kept separate rather than merged.
- **A value shorter than 8 characters is not worth storing** and is skipped by
  the redactor, because replacing a 3-character string everywhere would shred
  the prose a human then has to read. Not measured against real credentials —
  no real credential was read at any point in this work.
- **Nothing was verified end to end**, because nothing is wired. No process has
  been spawned with a vault-supplied environment, and no message has been sent
  through the redactor in anger. The tests are hermetic (`tmp_path` only) and
  every value used is an obviously-fake `sk_live_FAKE…` string.
