# The agent's computer

**Date:** 2026-09-02
**Status:** the screen surface is built and proven end to end on this Mac
(`server/sandbox.py`, three `/v1` routes, `docker/desk-computer/Dockerfile`).
The terminal and the file manager are **not built** and are scoped at the
bottom.
**Supersedes, in part:** `docs/the-browser.md` — see "Where reality has moved".

The owner sent two screenshots of the product we are matching: a panel
captioned **"COS's screen"** showing an agent's live desktop with an **Open**
button, and the same panel expanded into a full Chrome session with a dock at
the bottom — Chrome, a file manager, a terminal. His words: *"is this supported
by us? if not we need it implemented and proven e2e"* and *"its not only
browser its also a terminal and file system via the app of the agent's
computer"*.

It was not supported. Four modules existed — `browser.py`, `screen.py`,
`browser_takeover.py`, `browser_cdp.py` — **none wired and none ever run**, and
all four build command lines for a machine that does not exist here: Xvfb,
`google-chrome` on an X display, `ffmpeg -f x11grab`. macOS has none of that.

---

## The decision: the agent's computer is a container

The alternatives, and why each is wrong rather than merely worse:

* **Drive the owner's own Chrome on his own Mac.** The agent would move his
  mouse, steal his focus and share his Chrome profile — his cookies, his
  logins, his history. The requirement is the exact opposite: *nothing the
  agent does may seize his keyboard, his screen or his Chrome profile.* A
  design that fails a stated requirement is not a fallback.
* **Wait for the Linux box.** `10.99.0.1` is unreachable today, and a surface
  that cannot be run cannot be proven. "Proven e2e" was the ask.
* **A VM per desk.** Minutes to boot, gigabytes each, and a second operating
  system to keep patched, for a browser.

So: **one container per desk**, on this Mac today, on the box unchanged when
it lands.

### One per desk, not one shared

`docs/the-browser.md` makes a specific claim: *"Desks do not share a browser
identity. Signing the Acme desk into Stripe signs the Villas desk into
nothing."* Inside one shared container that claim is a **directory permission
in a box where both agents have a shell** — which is no claim at all. Per desk
it is a kernel namespace, and it is the same claim the reference product gets
wrong (one VM, one browser session, one credential set for every agent).

The cost is close to nothing. One image, N containers; the Xvfb and the
Chromium were per desk either way; the container itself is namespaces and a
bind mount. Measured on this Mac, from a cold home directory:

```
cold `docker run` -> Computer:      0.30 s
first photographable frame:         0.40 s (12 591 bytes)
```

The shared design also fails operationally: one desk's `stop()` kills
everyone's browser, and one desk's runaway page eats the memory limit for all
of them.

### Lifecycle — fired, rebooted, restarted

| Event | What happens | Why |
|---|---|---|
| Desk seated | `sandbox.start(desk)` — idempotent; already-up is success | The deck brings the machine up when there is someone to use it |
| Daemon restarts | Nothing. `is_up()` asks **Docker** | The container name *is* the registry. A sidecar JSON that says "running" about a container a reboot took away is how a panel shows black and calls it an idle agent |
| Mac reboots | The computer is **gone**, and stays gone | `--restart=no`, deliberately. `unless-stopped` would bring up, at 3 a.m., a Chromium still signed into whatever the owner signed it into during a take-over, on a desk nobody is watching |
| Desk fired | `sandbox.stop(desk)` removes the container. **The home directory stays** | It is the desk's work and its sessions. `stop` runs from teardown and error paths, and deleting a bind mount is the most expensive possible side effect of a restart. Deleting it is a person's decision: `rm -rf ~/.claude/agent-bus/browser/computers/<desk>` |

### Where its home lives

`~/.claude/agent-bus/browser/computers/<desk>` → `/home/agent`, a bind mount.

Under the bus dir for `browser.py`'s reason, one level up: a profile inside a
repo is a profile `git checkout` deletes — taking the cookies that are the
entire point of persistence — and that `git add -A` commits, publishing the
session tokens in them. `create_argv` refuses a home under a `.git` with
`home_in_repo`.

A **bind mount and not a named volume**, because a named volume is invisible
to the owner without `docker cp`, is not in any backup he has, and is
one `docker volume prune` from gone. A path is also portable: the same string
means the same thing on the box.

Proven: the Chrome profile is still on the Mac after `docker rm`
(`test_the_profile_is_on_the_mac_after_the_container_is_gone`).

### Portable to the box without a rewrite

`server/sandbox.py` has exactly **one** Docker-shaped function, `exec_argv`.
Everything else — the Chromium line, the x11grab line, the xdotool lines — is
`browser.chrome_argv` and `screen.snapshot_argv` **placed inside** the
container, unchanged. Swap those six words for `ssh box` and every other argv
in the file is byte-for-byte what runs on `10.99.0.1`.

That reuse is also a correctness property, not just tidiness: a second copy of
the x11grab line is a second answer to "which display do we grab", and one of
the two eventually reads `:0` — the owner's own screen, his mail, his
terminals, in a panel.

---

## How the deck reaches it, and why that is not a second door

**The container publishes nothing.** No `-p`, no `--publish`, no
`--network=host`. `sandbox.BANNED_RUN_FLAGS` sweeps for **publishing at all**
rather than for a port number, because an argv that published 9222 on some
other host port would pass a `"9222:9222" not in argv` test and be exactly as
open.

So Chromium's DevTools port — which has **no authentication of any kind**, and
on which `Page.navigate("file:///Users/samcarter/.ssh/id_ed25519")` is a
file read — lives in the container's own network namespace. Measured, live:

```
docker port deck-desk-livetest  ->  (empty)
connect 127.0.0.1:9222          ->  ConnectionRefusedError
```

**The only way in is `docker exec`,** over `/var/run/docker.sock`: a
root-owned unix socket, gated by group membership, not a port and not
listening on any network. And the socket is never mounted *into* a desk —
`_sweep` refuses any argv containing `docker.sock`, because a container with
the Docker socket is not a sandbox, it is root on the Mac.

**The only HTTP surface is `/v1`,** and every screen route goes through
`api._authorise`: bearer token, `hmac.compare_digest`, `401` without one and
`503` with none configured. Measured, live, against a real container:

```
GET /v1/agents/livetest/screen.jpg                      -> 401 unauthorized
GET .../screen.jpg  Authorization: Bearer wrong         -> 401 unauthorized
GET .../screen.jpg  Authorization: Bearer <token>       -> 200 image/jpeg 34943 bytes
```

**Nothing went on `/api`.** `server/app.py`'s surface has no token — it is the
loopback board and that is deliberate — and a route that photographs a
signed-in browser and injects keystrokes is not the board. That is the whole
reason there is no web panel in this change: the deck's own page cannot call
`/v1` without a token, and inventing an unauthenticated shortcut for it would
be the back door this section exists to prevent.

**So: what stops an unauthenticated caller getting a shell?** There is no
listening socket to call. The container has no published port; the display has
`-nolisten tcp`; the DevTools port is in a private netns; `docker exec` is a
root-owned unix socket; and the one network surface — `/v1` — is the same
bearer token that already gates the roster. There is also no `run` action and
there will not be one on this route.

### The hole I found, named rather than papered over

**Measured on this Mac, just now:**

```
docker exec <desk> bash -c 'exec 3<>/dev/tcp/host.docker.internal/7788
  && printf "GET /api/state HTTP/1.0\r\n\r\n" >&3 && head -c 120 <&3'
->  HTTP/1.1 200 OK ... content-length: 110147
```

A default-bridge container on Docker Desktop **can reach the daemon's
loopback-bound, unauthenticated `/api/state`**, through the VM gateway alias
`host.docker.internal` (192.168.65.254). The daemon binds `127.0.0.1`
believing that is a boundary; Docker Desktop's VM goes around it.

What is done about it here: `create_argv` carries
`--add-host host.docker.internal:127.0.0.1` and the same for
`gateway.docker.internal`, so the two names an agent would reach for resolve
to the container's own loopback.

**What that does not do:** the raw gateway address still works, and Docker
Desktop offers no container-side flag that closes it — you cannot drop the
default route without dropping the internet the browser needs, and
`--internal` networks have no egress at all. This is a **host-side** problem
and its real fix is that `/api` should not be readable by anything that is not
the owner. That is `server/app.py`'s, not this slice's, and half-bridging it
here would be worse than the gap because it would look done.

The honest scale of it: a desk is already a real Claude Code process running
as the owner on this Mac, and `docs/the-browser.md` already concedes
*"Anything else running as this user on the box"*. The container does not
*grant* the agent anything it did not have. But it was supposed to be a place
with **less** privilege than the Mac, and on this point it is not.

### Chromium keeps its sandbox, and Docker's hardening is what nearly took it

The one flag in `create_argv` that looks like a step backwards:
`--security-opt seccomp=unconfined`. It is there because of a measurement.

With Docker's **default** seccomp profile, `cap-drop ALL`, non-root and
`no-new-privileges`, Chromium prints and exits:

```
ERROR:zygote_host_impl_linux.cc(128)] No usable sandbox! If this is a Debian
system, please install the chromium-sandbox package... If you want to live
dangerously... you can try using --no-sandbox.
```

Docker's default profile blocks `clone(CLONE_NEWUSER)` without
`CAP_SYS_ADMIN`, so Chromium cannot build its **namespace sandbox**. The
tempting fixes are both bad: `--no-sandbox` is in
`browser.BANNED_CHROME_FLAGS` and hands every page the agent visits the
agent's own privileges; `--cap-add SYS_ADMIN` is a much larger grant than the
one being removed.

Unconfined at the Docker layer instead — and then, measured, per process:

```
109 --type=renderer  Seccomp: 2  userns=user:[4026532816]
118 --type=renderer  Seccomp: 2  userns=user:[4026532816]
init                             userns=user:[4026531837]
```

Every renderer — the process that actually runs hostile JavaScript — is in
`Seccomp: 2` (a seccomp-bpf filter, Chromium's own, tighter than Docker's
blanket one) and in a **user namespace that is not the container's**.

So the trade is: the browser *process* (uid 1000, no capabilities, no way to
gain any) loses Docker's syscall filter, and every *renderer* gains one. On a
process whose job is to execute code from the open web, that is the right way
round. It is asserted per renderer in
`test_chromium_keeps_its_own_sandbox`, not on the absence of an error line —
a Chromium started with `--no-sandbox` also logs nothing.

`chromium-sandbox` (the setuid one) is deliberately not in the image:
`no-new-privileges` makes a setuid binary useless, and it is the weaker
sandbox anyway.


---

## Update 2026-09-28: the desk drives it

The owner: *"it has delay, and i dont see whats the agents do there. do they
know they can use it?"* They did not. Measured on the box: atlas's computer
had been up 21 days and no transcript held one use of it; nothing in
`hire.brief` named it and nothing gave a desk a way in.

**The way in is an MCP server per desk** (`server/computer_mcp.py`), written
into the spawn argv as `--mcp-config` with the desk's name in *its* argv. No
tool takes a `desk` field, so no call the model makes can reach another
desk's container. A `YOS_*` line (the routines pattern) was rejected: it is
answered a turn later, which cannot carry navigate -> read -> click, and it
cannot return a picture. Tools: `navigate`, `read_page`, `screenshot`,
`click`, `type_text`, `press_key` (`mcp__computer__*`).

**How it reaches DevTools without publishing it** (`server/desk_computer.py`):
`docker exec --interactive` runs a constant bash line that dials
`127.0.0.1:9222` inside the desk's own container and relays bytes. Nothing is
published; every command still leaves through `sandbox.exec_argv`. Clicks,
keys and typing reuse `sandbox.send_input`, so the xdotool option guard holds.
A stdlib WebSocket client (~40 lines, size-capped) replaces the in-image
`websockets` plan in §3 above: the image has no Python, and rebuilding it
would recreate atlas's container.

**It stops at the owner's steps.** Before any click, key or typed text,
`browser_takeover.needs_human` reads the page; a login, 2FA, captcha or
payment is refused with an instruction to hand over.

**On first use, not for every desk.** atlas's container holds 673 MiB on a
7.9 GiB box already at load 10 on 4 cores; eight idle Chromiums would be
~5 GiB spent on desks that, so far, never browsed.

**What this does not close.** A desk's Claude runs as the box user, who is in
the `docker` group. A desk that shells out to `docker exec deck-desk-<other>`
is not stopped by any of this -- the MCP server is the sanctioned road, not a
wall. Closing it means taking `docker` away from desk sessions.

**Proven on the box:** after the deploy atlas was re-seated, asked to open a
random Wikipedia page, called `mcp__computer__navigate` on its own, answered
"Rupandehi 1", and the owner's panel frame showed that article.

---

## Update 2026-09-30: sign in once, every desk is signed in

The owner: *"why does each agent need me to log in on his computer if it's
the same server?"* The containers stay one per desk (a crash, a runaway page
or a `stop()` still touches one desk). What is now shared is the **logins**,
not the browser. That reverses the "signing Acme into Stripe signs Villas into
nothing" claim above, deliberately, under his full-access ruling.

**How** (`server/login_vault.py`). When a browser sign-in handoff is answered
`done`, the deck reads that desk's cookies over CDP (`Network.getAllCookies`),
merges them into one vault, and writes them into every other running desk's
browser (`Network.setCookies`), in a background thread so his tap does not
wait. A desk whose browser starts later is seeded from the vault at launch
(`desk_computer.ensure`, only when it launched the browser, so a desk's own
newer cookies are never overwritten on every tool call). One handoff shares
once: its id is recorded before anything is copied.

**Why this design, measured on the box (wake-probe plus a throwaway desk):**

| Option | Measured | Verdict |
|---|---|---|
| (a) one shared profile opened by every desk | the second container's Chromium exits **21**, "The profile appears to be in use by another Chromium process ... on another computer" | impossible while two browsers run |
| (a') copy the `Cookies` file into each profile | works only with Chromium stopped; a **session** cookie was gone after `Browser.close` (never on disk), a `Max-Age` one survived as a `v10` row | loses exactly the cookies most sign-ins use |
| (b) CDP export and import + a vault | `setCookies` into a live second desk took **0.14 s**; httpbin `/cookies` echoed the transferred session cookie on the other desk | **chosen** |
| (c) one browser container all desks share | not built: desks fight over one screen and one keyboard, one crash takes every desk down | rejected |

**What is not shared.** localStorage and IndexedDB (a site that keeps its
token there still needs a sign-in per desk). **Google is unmeasured:** proving
that a Google session survives transfer needs a real Google account signed in,
and the only one on the box is atlas's, which was not touched. Every desk runs
the same image on the same host and IP, which is the most favourable case, but
Google may still re-challenge a session that moves (device-bound session
credentials). If it does, the owner signs in on that desk once more and the
vault picks that sign-in up too.

**Security.** Every desk gets every login. The vault is
`~/.claude/agent-bus/browser/login-vault/` (directory `0700`, files `0600`,
owned by the deck user); it is outside `/opt/agent-deck`, so the deploy backup
does not copy it, and nothing in the deck sends it off the box. No cookie
value is logged or returned: the log line and the report carry counts and
desk names, and a failed CDP call is reported by its slug, never its message
(a `setCookies` error echoes its params, which are the cookies).

**The toggle.** `[desks] shared_logins = false` in `/etc/agent-deck/deck.toml`
keeps every desk's logins to itself. Default on.

---

## Update 2026-09-30: sign in with the Mac's passkey, every desk gets it

Owner, verbatim: *"i need to be able to login with the passkeys from my mac on
their computers"*. The desk's browser is Chromium in a Linux container; his
passkeys live in iCloud Keychain on his Mac and iPhone. A passkey-only
sign-in can never finish on a desk's screen.

**Design.** On a browser sign-in card the Mac app offers *Sign in on this Mac
(passkey)*. It launches Google Chrome on the Mac with a throwaway 0700
`--user-data-dir` and `--remote-debugging-port=0` on 127.0.0.1, at the site's
front door (`https://<host>/` — never the card's own challenge URL). He signs
in with Touch ID (or his iPhone by QR), presses *Share*: the app reads that
profile's cookies over CDP (`Storage.getCookies` on the browser target),
posts them to `POST /v1/logins`, which puts them into the login vault and every
running desk's browser (`login_vault.share_in`), then closes Chrome and deletes
the profile on every path. The card is answered `done` so the desk re-checks.

**Measured on his Mac (Chrome 154, macOS 26.6):**

* A fresh non-default profile launched with a debugging port reports
  `isUserVerifyingPlatformAuthenticatorAvailable() == true`,
  `passkeyPlatformAuthenticator: true`, `hybridTransport: true`
  (`PublicKeyCredential.getClientCapabilities()`), with no prompt and no
  sign-in. Chrome is signed with `com.apple.developer.web-browser.public-key-credential`
  and ships its iCloud Keychain integration (`device/fido/mac/icloud_keychain*`).
  Not measured: the passkey *sheet* listing his Google passkey — that needs
  his Touch ID, so it is his first live try. macOS may ask once to let
  Chrome use iCloud passkeys (System Settings > Privacy & Security >
  Passkeys Access for Web Browsers).
* Chrome refuses `--remote-debugging-port` on its default profile (136+), so a
  throwaway profile is the only shape anyway, and his own profile is untouched.
* Transfer: a session cookie and an HttpOnly/Secure persistent cookie set on
  httpbin.org in Mac Chrome 154, read with `Storage.getCookies`, written with
  `Network.setCookies` into a throwaway container from the desk image
  (Chromium 151, Linux UA) — `httpbin.org/cookies` on the box echoed both back.
  The real `ChromeSignInBrowser` (run off-screen) read the session cookie,
  closed Chrome and left no profile behind.

**Not measured, and the risk to watch: Google.** A Google session copied from
Mac Chrome to Linux Chromium changes device, OS, UA and IP at once. Google may
re-challenge, and where Chrome's device-bound session credentials (DBSC) are
active for his account the short-lived Google cookies are tied to a key on the
Mac and would stop refreshing on the desk. Only his live try tells. Sites
without device binding (most) carry over as httpbin did.

**Alternatives rejected.** `WKWebView`: WebAuthn/passkeys in a web view need the
site's associated domains in the app's entitlements, which no third-party site
grants. `ASWebAuthenticationSession`: its cookies never reach the app.
Safari: no cookie export API. Chrome remote debugging over a pipe
(`--remote-debugging-pipe`) would close the loopback port; it needs extra fds
through `posix_spawn` — a follow-up, the port lives only while the window does.

**Security.** Cookies travel only Mac → deck over the app's existing bearer
channel (pinned TLS), are never logged or returned, the route is write-only,
the vault is 0700/0600, and the Mac profile is deleted after every share,
cancel, or failure.

## The screen, and the take-over

Two surfaces, both `/v1`, both proven live. `docs/client-api.md` §15 is the
wire contract; this is why it is shaped that way.

### Seeing it

`GET /v1/agents/{name}/screen.jpg` runs one `ffmpeg -f x11grab -frames:v 1`
inside the desk's container and returns the JPEG. Stills, not video — the
three reasons are in `server/screen.py`'s docstring and they still hold: it
costs nothing when nobody is looking, it cannot wedge the event loop, and it
survives a restart. Nothing here beat them, so nothing here changed them.

`X-Frame-Age` rides with the bytes because *a frame without an age is a lie*:
a checkout page looks identical a second old and twenty minutes dead, and "the
agent is sitting still" and "the feed died" are opposite facts.

### Taking it over

`POST /v1/agents/{name}/screen/input` — `click`, `type`, `key` — injected with
`xdotool` on the same display. **This is the Open button.**

**Why input injection and not VNC.** noVNC or x11vnc would be a second
listening service inside the container, a port to publish, a WebSocket proxy
in a daemon that is deliberately stdlib-only, and a second authentication
story — in a design whose entire security answer is *"there is no listening
socket to call"*. Frames plus xdotool need none of that, are token-gated by
construction, and work on a phone, which VNC does not.

**The injection is argv, never a shell.** The owner types passwords through
this during a handoff. `xdotool type --delay 40 -- <text>`: the text is one
argument after `--`, so a leading `-` is text and a backtick is a character.
Proven live — `example.com` + backtick + `id` + backtick was typed and the
omnibox shows it **as text**:

```
POST /v1/agents/livetest/screen/input {"action":"type","text":"example.com`id`"}
-> the omnibox reads:  example.com`id`
```

`key` is validated against `[A-Za-z0-9][A-Za-z0-9_+]{0,39}` rather than
escaped, because the thing being protected is not a shell (there is none) but
**xdotool's own option parser**: `xdotool key --file /etc/passwd` is a file
read wearing a keystroke.

**The gap:** nothing stops the agent driving while the human clicks.
`browser_takeover.py`'s stop-hand-over-resume loop is what keeps them apart —
the agent stops itself *before* asking. A client that offers Open on a desk
that never stopped will have two things on one keyboard. Written into
`docs/client-api.md` §15 rather than guarded, because the guard belongs in the
handoff state machine and that is not this slice.

---

## Where reality has moved since `docs/the-browser.md`

Read that document first; it is still the design of record for the browser
itself. Where I agree, and where it is now wrong:

**Still right, and load-bearing.** One Chrome with one profile, so "take over,
sign in, hand back" needs no session transfer — every alternative moves
credentials between two places and moving them is the part that leaks. The
loopback CDP bind. `-nolisten tcp`. The banned-flag sweep. Profiles under the
bus dir. Structural rather than textual detection in `needs_human`. Whole-origin
matching. `payment > 2fa > captcha > login`. All of it survived contact.

**Wrong now, and changed here:**

1. **"Xvfb :99" is not a machine-wide resource any more.** The document
   allocates display numbers with a lock file so two desks do not both get
   `:99`. Per container, each desk's X server is alone in its own IPC and
   network namespace, so **every desk is `:99`** and `reserve_display` is not
   used. It stays in `browser.py` for the shared-box case; this is the one
   thing containers make simpler rather than harder.
2. **`google-chrome` is not the binary.** There is no `google-chrome` build for
   linux/arm64 — Debian's `chromium` is. And it must be called as
   `/usr/lib/chromium/chromium`: measured, `/usr/bin/chromium` is a shell
   wrapper that **appends flags of its own** (`--enable-remote-extensions`,
   `--load-extension=`, `--disable-dev-shm-usage`). A security boundary
   asserted on our argv is not a boundary if a wrapper edits it afterwards.
3. **The `websockets` dependency does not go where that document says.** It
   says "an isolated venv on the box that runs the browser, e.g.
   `/opt/deck-browser/venv`". But with a container, the CDP endpoint is inside
   the container's network namespace and the deck is outside it — the deck
   cannot reach it *at all* without publishing a port, which is the one thing
   this design will not do. So `CDPDriver` has to run **inside the desk's
   container**, and the dependency belongs **in the image**, versioned with
   it, never on the Mac. That is strictly better than the venv-on-the-box
   plan, and it is a real change to the plan of record.
4. **The three "not built" items are now two.** The Chrome and the take-over
   loop were the slice; the panel and its route existed nowhere. They exist
   now.

---

## What I cut, and where the edge is

The owner's order of value was **screen, then terminal, then files**, and his
instruction was *"one surface genuinely working end to end"* over three
half-built. So:

**Cut — the terminal.** It is the second-most valuable thing here and it is
the most dangerous route in the product: a terminal reachable over HTTP *is* a
remote shell, and it needs its own answer to what an agent may run, which is
`autoreview.py`'s question and `autoreview.py` does not key on `surface` yet
(a gap `docs/the-browser.md` already names). The image deliberately has no
`curl`, no `ssh` and no `git` so that the day it is built, the starting point
is a bare box. There is no `run` action on the input route and there must not
be one added there — it is a different surface with a different authorisation
story, not a fourth `action`.

**Cut — the file manager.** Cheapest of the three now, because the desk's home
*is already a directory on the Mac* (`~/.claude/agent-bus/browser/computers/
<desk>`). A file surface is a list-and-fetch over a path that is already
there; it needs no container work at all. It is cut for budget, not for
difficulty.

**Cut — wiring `browser_cdp.py`.** It is still the only module in the repo with
neither a caller nor a test. It is not wired here because **the screen surface
needs no CDP at all** — x11grab and xdotool are display-level and
protocol-free, which is a large part of why the screen was the right thing to
do first. Wiring it means a WebSocket client, and per §3 above that means an
in-container driver process, which is its own slice.

**Cut — the web panel.** For the reason in the security section: the deck's own
page is unauthenticated, `/v1` is not, and the shortcut between them is the
back door. The client contract is in `docs/client-api.md` §15 and the native
client is the surface.

**Cut — starting a desk's computer automatically.** `sandbox.start` is written
and proven, and nothing calls it on a hire or a seat. Wiring it into the desk
lifecycle touches `server/hire.py` and `server/onboard.py`, which another
agent owns right now.

### The honest edge — what is proven, and what is not

**Proven, on this Mac, today** (`-m live`, 7 passed in 38 s, plus a manual run
of the HTTP surface):

* a real container from a real image, cold-started in under two seconds;
* a real Chromium on a real Xvfb inside it, fetching `https://example.com`
  over the real internet, **with its sandbox intact** — renderers at
  `Seccomp: 2` in a private user namespace;
* a real 34 943-byte JPEG of that page, captured by ffmpeg inside the
  container and **served by the deck over HTTP** as `image/jpeg` with
  `X-Frame-Age: 0.215` and `Cache-Control: no-store`;
* the same route answering `401 unauthorized` with no token and with a wrong
  one;
* **a take-over that actually worked**: `POST .../screen/input`
  `{"action":"click","x":301,"y":301}` → `200` → the next frame is
  `iana.org/help/example-domains`. And `ctrl+l` plus a typed
  `` example.com`id` `` → the omnibox shows the backtick as text;
* `--file`, an off-screen click and an unknown action each refused `400
  bad_input` with **zero** commands executed;
* `docker port` empty and `127.0.0.1:9222` refused while all of that was
  running;
* the Chrome profile still on the Mac after `docker rm`.

**Not proven:**

* **Nothing has run against a real login, 2FA or checkout.** The take-over was
  proven on a link and an address bar. `browser_takeover.needs_human` is still
  covered only by hand-built page states — it has never seen a real Stripe
  iframe, which `docs/the-browser.md` already admits.
* **Nothing ran on `10.99.0.1`.** The box is unreachable (its ssh
  brute-force guard is tripped). The portability claim is an argument about
  `exec_argv` being the only Docker-shaped function, not a measurement.
* **No agent has driven this.** A human — me — sent the clicks. There is no
  caller: no desk starts a computer, and `CDPDriver` is still unwired, so the
  agent half of "one browser, two drivers" is the half that does not exist
  yet. **What works today is the owner's half**: he can see the screen and he
  can take it over.
* **Long-running behaviour is unmeasured.** Nothing has been up for an hour.
  Memory drift, `/dev/shm` pressure over a long session, and what a crashed
  Xvfb does to `is_up` are all untested.
* **Concurrency is unmeasured.** Two desks with computers at once is designed
  for (separate namespaces, both `:99`) and has never been run.
