# The agent's browser

**Date:** 2026-09-01
**Status:** built (`server/browser.py`, `server/browser_takeover.py`,
`server/browser_cdp.py`); not wired to the UI

An agent that cannot open a browser cannot log into anything, cannot fill a
form, and cannot publish. Every other module in the deck decides *what* a desk
should do. This is the first one that lets a desk actually do it on the open
web.

Note what this changes about the design of record: the agent-surface spec
(`docs/superpowers/specs/2026-09-01-agent-surface.md`) lists "Browser /
computer use" under **What we are NOT building**, named there so it would not
be mistaken for in-scope. That exclusion is now lifted for this layer, on the
owner's instruction. Nothing else in the spec moves — the three-CLI rule, the
ban on `--dangerously-skip-permissions`, and the audit-log requirement all
still bind.

---

## The architecture

**One Chrome, two drivers.**

```
        ┌──────────────── the box ─────────────────┐
        │                                          │
        │   Xvfb :99  (-nolisten tcp)              │
        │      │                                   │
        │      ├── Chrome                          │
        │      │     --user-data-dir=<bus>/browser/profiles/acme
        │      │     --remote-debugging-address=127.0.0.1
        │      │     --remote-debugging-port=9222   │
        │      │        ▲                  ▲        │
        │      │        │ CDP              │ X11    │
        │      │     CDPDriver          the human   │
        │      │     (the agent)      (takes over)  │
        │      │                                   │
        │      └── ffmpeg x11grab ──► screens/acme.mp4
        │                              "acme's screen"
        └──────────────────────────────────────────┘
```

A single Chrome runs on a virtual X display. The agent drives it over the
DevTools Protocol. Sam sees the *same* display in the panel and, when the
agent stops, uses it directly.

**Why one browser and not two.** Because it is literally one Chrome with one
profile directory, "take over, sign in, hand back" needs no session transfer at
all. The cookie the human's sign-in creates is already in the profile the agent
uses on its next command, and on its next run tomorrow. Every alternative
design — a headless agent browser plus a separate human one, cookie export,
Playwright storage-state files — has to *move credentials between two places*,
and moving them is the part that leaks.

**What lives where.**

```
~/.claude/agent-bus/browser/
  profiles/<desk>/       Chrome's --user-data-dir, persistent, per desk
  displays/<n>.lock      our claim on display :<n>, holds Xvfb's pid
  screens/<desk>.mp4     the x11grab capture the panel shows
```

Under the bus dir, deliberately: no branch switch touches it and no
`git add -A` reaches it. A profile inside a repo is a profile that
`git checkout` deletes — taking with it the cookies that are the entire point
of persistence — and that `add -A` commits, publishing the session tokens
inside them. `start()` refuses a root with a `.git` above it.

**Capture** reuses the approach proven in
`~/Projects/yair_os/deckop/recording/screen.py`: Xvfb, then `ffmpeg -f x11grab`
on the same display, H.264 / yuv420p so it plays in a browser. That code was
read, not imported — it is a different repo and the deck takes no dependency
on it.

### The command lines, verbatim

`chrome_argv` for desk `acme` at the Acme checkout:

```
google-chrome
--display=:99
--user-data-dir=/Users/samcarter/.claude/agent-bus/browser/profiles/acme
--remote-debugging-port=9222
--remote-debugging-address=127.0.0.1
--no-first-run
--no-default-browser-check
--disable-background-networking
--disable-component-update
--password-store=basic
--window-size=1280,800
--window-position=0,0
https://acme.initech.example/checkout
```

```
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp

ffmpeg -y -f x11grab -framerate 12 -video_size 1280x800 -i :99 \
  -codec:v libx264 -preset veryfast -pix_fmt yuv420p .../screens/acme.mp4
```

---

## What a live driver needs, and why the stdlib cannot do it

**The deck stays stdlib-only.** `server/browser.py` keeps that rule completely:
argv builders and `subprocess`, nothing else.

`server/browser_cdp.py` cannot. Half of CDP is plain HTTP and is done with
`urllib` — `/json/list` to find a page target, `/json/version` for the banner.
But **commanding** a page (`Page.navigate`, `Runtime.evaluate`,
`Page.captureScreenshot`, `Network.getCookies`) goes over the target's
`webSocketDebuggerUrl`, and it is `ws://` and only `ws://`. The standard
library has no WebSocket client — `http.client` does not speak the upgrade
handshake or the frame format, and writing one by hand for a security-relevant
channel is worse than taking the dependency.

So the dependency is quarantined:

| | |
|---|---|
| **Package** | `websockets` (>=12,<16) — `websockets.sync.client.connect` |
| **Why this one** | pure Python, no browser bundled, no native build, actively maintained, and it has a synchronous client, so nothing in the deck has to become async to use it |
| **Where it is installed** | an isolated venv **on the box that runs the browser**, e.g. `/opt/deck-browser/venv` — `/opt/deck-browser/venv/bin/pip install 'websockets>=12,<16'` |
| **Where it is NOT installed** | the deck's own venv (`~/Projects/claude-dashbaord/.venv`). Verified absent today, and the import guard test keeps it that way |
| **Explicitly rejected** | **Playwright** — it bundles its own browser binaries, which is the opposite of "one Chrome the human can also use", and it is a ~500 MB install for a WebSocket. **`websocket-client`** — sync but less maintained. Neither goes in the deck |

Three mechanisms make a missing package harmless:

1. the import is **lazy**, inside `CDPDriver._connect`, so
   `import server.browser_cdp` works on a Mac with nothing installed;
2. a missing package raises `BrowserError("driver_unavailable")` carrying the
   install line, not an `ImportError` from four frames down;
3. `tests/test_browser_live.py` carries one deliberately **not**-`live`-marked
   test asserting that import works — so the day someone hoists
   `import websockets` to the top of the module, the default suite says so.

---

## How a human takes over, and hands back

1. **The agent stops itself.** Before acting, it calls
   `browser_takeover.needs_human(driver.page_state())`. A non-`None` answer is
   one of `payment`, `2fa`, `captcha`, `login`, `other` — the classes an agent
   must never attempt (spec M10, plus the "confirm it's you" interstitial).
2. **It raises a handoff.** `raise_browser_handoff` writes into the *existing*
   `handoffs.json` through `handoff.raise_handoff`. Not a second queue: a
   second queue is a second place to look, and the one nobody looks at is the
   one with the blocked agent in it. So a browser block ages out on the same
   clock, goes stale when its desk dies, shows on the same board, and is
   answered by the same `take over / done / skip` replies.
3. **Sam gets a message.** `takeover_brief` — same shape as `handoff.compose`,
   which is already proven on a phone:

```
*acme's screen* stopped at a payment confirmation.

*State: nothing on this page has been submitted and no payment has been made at this step. The agent has stopped and will not touch the screen while you have it.*

You: take the browser over and do the payment confirmation yourself.

Where: https://acme.initech.example/checkout?step=pay&amount=3900
Site: https://acme.initech.example — acme's screen, display :99

Hand back when you are done. Whatever you sign into stays signed in for the agent — it is the same browser.
```

   **State before the ask**, deliberately. A message that says "I need you" and
   nothing about whether £39 has already left is a message that gets a laptop
   opened in a panic over a page that never submitted. That line is what
   decides whether this is urgent, so it is the second thing read and it is
   bold. The claim is narrow enough to be true every time — the function is
   only ever called *because* the agent stopped before acting.

4. **He does it on the same screen.** The panel is that X display; his clicks
   and keystrokes go to the same Chrome the agent was driving.
5. **He replies `done` / `skip` / `take over`.** The existing handoff resolve
   path runs, and `resume_brief` is injected back into the agent's session.
   `done` orders a **re-check** — a human saying "I did it" is a claim, not a
   fact. `skipped` orders **abandonment** — an agent that reads a skip as
   "retry later" hammers a login screen until the account locks. All three
   outcomes end with the same sentence: the sign-in the human just did is
   already the agent's, same Chrome, same profile, so do not repeat it.

### Why detection is structural and never textual

`needs_human` reads the DOM and the frame tree. It never reads
`page_state["text"]`, and `CDPDriver.page_state` does not even return it.

Text matching fails in **both directions at once**, and both failures are bad:

* it **misses** the real thing — a Stripe card field lives in a cross-origin
  iframe whose text the page cannot read at all, so the page about to take £39
  need not contain the word "card" anywhere;
* it **fires** on an article — a blog post titled "Why passwords are dead"
  carries every trigger word, and three false 2 a.m. handoffs teach Sam to
  ignore the queue, at which point the real one is ignored too.

The signals used instead:

| Kind | Structural signal |
|---|---|
| `payment` | a frame whose **whole origin** is in `PAYMENT_ORIGINS` (Stripe, PayPal, Adyen, Braintree, Klarna, Google/Apple Pay), or an input with `autocomplete` in `cc-number` / `cc-csc` / `cc-exp*` |
| `2fa` | an input with `autocomplete="one-time-code"` — the web standard, and what makes iOS offer the code from Messages |
| `captcha` | a frame origin in `CAPTCHA_ORIGINS` (reCAPTCHA, hCaptcha, Cloudflare Turnstile), or an element with `data-sitekey` |
| `login` | a **visible** `<input type="password">`, or `autocomplete` in `current-password` / `new-password` |
| `other` | CDP reported an interstitial on the tab |

Two details that matter more than they look:

* **Whole-origin matching, never substring.**
  `https://js.stripe.com.evil.example` *contains* the real origin and is a
  different site. `in` would trust it.
* **Visible fields only.** Password managers and framework autofill shims leave
  hidden password inputs on ordinary pages. Blocking on those fills the queue
  with handoffs for pages that have no visible form — the same cry-wolf
  failure, arrived at from the other side.

**Order is `payment > 2fa > captcha > login > interstitial`**, and it is
load-bearing on a checkout page carrying several at once. Money is the
irreversible one: the message read at 2 a.m. has to be about the £39 charge,
not the sign-in box next to it.

---

## Surface and origin

Every block records two things a rule could later be written from:

```python
{'surface': 'browser', 'desk': 'acme',
 'origin': 'https://acme.initech.example', 'kind': 'payment'}
```

**`surface`** exists because an action on the browser is not the same risk as
the same action on the Mac. The reference product's auto-review card makes the
same distinction, from the other end — *"Runs on Grok Bot's computer"*
distinguishes their cloud machine from the user's laptop. A rule granted for
the browser must not silently authorise a shell.

**`origin`** is scheme + host (+ port) and nothing else, because the origin is
the only part of a URL a *durable* rule may be written against. A rules file is
permanent, is read by every desk, and is not the vault — a session id or
one-time token stored there is a leak with a scope attached.
`origin_of` refuses `file:`, `data:`, `javascript:` and `chrome:` outright:
`file:` is exactly the scheme an escaped agent would want a standing rule on.

### The gap, named rather than bridged

**`server/autoreview.py` does not key on `surface` today.** A browser grant and
a local grant are currently indistinguishable to the rule engine. Nothing here
changes that — `autoreview.py` is out of this slice's scope, and quietly
half-bridging it would be worse than the gap, because it would look done.

Two smaller pieces of the same gap:

* `handoff.Handoff` has no `surface` or `origin` column, so
  `raise_browser_handoff` puts them in `evidence` as
  `surface=browser desk=acme origin=https://... display=:99`. A human reading
  the card can see it; a rule engine cannot key on free text. **When
  `autoreview` learns about surfaces, `Handoff` should get real columns and
  this string should go.**
* Nothing writes an `events.jsonl` line yet when a browser block is raised. The
  spec requires every decision to be written down (§"Hard constraints" 4);
  that belongs in the wiring, which is not this slice.

---

## Proven versus reasoned

**Proven** — 78 tests in the default suite, no X server, no browser, no socket:

* per-desk profile isolation, and a hostile desk name (`../../etc`, `a/b`,
  `/absolute`) refused rather than sanitised;
* the loopback bind is **present** in the argv — asserted as presence, because
  an argv that dropped the flag entirely would pass an "0.0.0.0 is absent"
  test and then inherit whatever Chrome's default is after the next upgrade;
* none of the four banned flags is emitted, swept from a list the test shares
  with the module;
* two desks starting at once get different displays, and a display already
  claimed by a real X server (`/tmp/.X99-lock`) is skipped;
* the default profile root is under the bus dir and not in a checkout;
* a real password field is caught **and** an article about passwords is not; a
  hidden password field does not fire **and** the same field shown does;
* a `sk_live_FAKE...` in a query string leaves as
  `[redacted STRIPE_LIVE_KEY]`, and a 6-digit code the vault never saw leaves
  redacted too;
* an origin never carries a query string or `user:pass@`;
* a browser block lands in the existing handoff queue and composes into a
  phone message through the existing path.

**Reasoned, and covered only by `-m live`** (`tests/test_browser_live.py`, run
on the box):

* that Chrome actually *honours* `--remote-debugging-address` — the pure test
  proves we asked; the live test connects to the box's routable address and
  expects a refusal;
* that a cookie set in one run is still there in the next;
* that `stop()` leaves no Chrome and no Xvfb behind;
* that a real Chrome's reported page state has the shape `needs_human` was
  written against.

**Not proven at all, and honestly so:**

* nothing has run against `acme.initech.example` — the Stripe origin list is
  from the vendors' documented iframe hosts, not from a captured checkout;
* `capture_argv` has never produced a file here; the approach is inherited from
  a path proven on the Ubuntu box, not re-proven;
* the "Teach a task" button, the file manager and the terminal in the observed
  dock are **not built**. This slice is the Chrome and the take-over loop only.

---

## The security boundary

What this layer is worth, stated precisely, because the vault module's own rule
is that a lie about a credential is worse than no vault.

**What it does defend against.**

* **The DevTools port is not reachable off this machine.** CDP has no
  authentication of any kind — reachability *is* the access control. Anything
  that can speak CDP to that port can `Page.navigate` to
  `file:///Users/samcarter/.ssh/id_ed25519` and read the response. Bound to
  `127.0.0.1` that is nobody on the LAN.
* **The X display is not a network service.** `-nolisten tcp` is the X-level
  twin of the same rule. Without it, anything that could reach the display
  could read every pixel of a signed-in browser and inject keystrokes into it.
* **The renderer sandbox is intact.** No `--no-sandbox`, no
  `--disable-setuid-sandbox`, no `--disable-web-security`. A page the agent
  visits does not get the agent's privileges.
* **Desks do not share a browser identity.** Signing the Acme desk into Stripe
  signs the Villas desk into nothing. This is the specific thing the reference
  product gets wrong — one VM, one browser session, one credential set for
  every agent.
* **The ambient environment does not reach the browser.** `chrome_env` is the
  desk's granted vault secrets plus `DISPLAY`, `HOME`, `PATH`, `LANG` — not
  `os.environ`. A key exported in the shell that launched the daemon has no
  business inside a process that runs untrusted JavaScript.
* **Nothing leaves unredacted.** Every URL and excerpt goes through
  `browser.safe` — the vault's exact values *and* the shape rules in
  `handoff.redact`. (Composing the two turned up a real bug: the shape rules
  ate the vault's named marker down to `[redacted] STRIPE_LIVE_KEY]`, throwing
  away the only useful part — which key leaked. Shape rules now run *between*
  markers.)

**What it does not defend against — the honest list.**

* **An agent with a shell.** A desk is a real Claude Code process with a real
  shell in a real working directory. It can read its own environment, so it can
  read the vault values it was granted; it can `curl` the CDP port itself,
  because the port is loopback and it is *on* loopback; it can read its own
  profile directory and every cookie in it; and it can start a second Chrome
  with any flags it likes, including `--no-sandbox`. **None of that is
  preventable from inside this module.** The grant list in the vault, and
  Auto Review deciding what the agent may run at all, are the actual security
  model. This layer keeps credentials out of *logs, messages, screenshots, the
  board and other desks* — it does not fence in the desk that was granted them.
* **Anything else running as this user on the box.** The loopback CDP port is
  reachable by every process this user runs. Loopback is a network boundary,
  not a process one.
* **A page the human signs into during a take-over.** He is signing into a real
  site in a real browser and the session is then the agent's, by design. That
  is the feature. It also means a take-over on the wrong desk's screen grants
  the wrong desk — which is why display allocation is a lock file and not a
  constant.
* **`type_text` typing something it should not.** There is no guard in the
  driver against typing into a password field. The guard is upstream:
  `needs_human` stops the agent before such a page is driven at all. If that
  detection misses, the driver will do as it is told.

---

## Not built in this slice

* Any wiring. `server/app.py`, `server/api.py` and `web/*` are untouched, so
  there is no "acme's screen" panel, no HTTP route, no live MJPEG/WebRTC feed —
  only the mp4 capture argv that would feed one.
* The dock from the observed product: file manager, terminal, "Teach a task".
* `events.jsonl` audit lines for browser blocks.
* `autoreview.py` keying on `surface` — named above as a real gap.
* Any install on this Mac or on any box. `websockets` was not installed
  anywhere; the live tests are written to be run once a box exists.

## Passkey challenges

A passkey lives on the owner's own phone or Mac, never in a desk's container,
so a passkey-first sign-in (Google `/challenge/pk`, GitHub `webauthn`,
Microsoft `fido`, Apple `passkey`) dead-ends at "No passkeys available".
`browser_takeover.is_passkey_challenge` spots these by whole host plus path
shape (never page text). `desk_computer.look` then runs, on one CDP
connection, `WebAuthn.enable` + an empty virtual authenticator (the request is
refused at once with `NotAllowedError`, so no OS passkey sheet stays on the
screen) and clicks the provider's "Try another way". Nothing is typed. If the
page is still a passkey page, one card says: "This site wants a passkey...
choose 'Try another way' -> password or phone prompt." No Chromium flag is
used: none that disables the platform passkey UI was verified.
