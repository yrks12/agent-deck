# Shaliach for macOS

A native SwiftUI client for talking to a roster of AI agents — an iMessage-shaped
window over Shaliach `/v1` API. macOS 14+, SwiftPM, **no third-party
dependencies at all**: SwiftUI, AppKit, Foundation, Security, `URLSession`.

---

## Build

```console
$ swift build --package-path .
Building for debugging...
Build complete!
```

```console
$ swift test --package-path .
Executed 374 tests, with 0 failures (0 unexpected)
```

> `swift test` prints a trailing `0 tests in 0 suites` line from the
> swift-testing runner. That runner has nothing to run — every test here is
> XCTest. **The line that matters is the XCTest total.**

## Run

Two ways.

```console
# From the package, no bundle — quickest while developing.
$ swift run DeckApp

# As a real .app, with a Dock icon, menu bar and a stable bundle id.
$ Scripts/make-app-bundle.sh release
$ open "dist/Shaliach.app"
```

## Point it at a deck

Set `DECK_URL` and put a token in the Keychain.

```console
$ DECK_URL=http://127.0.0.1:7788 swift run DeckApp
```

Then open **⌘,** and paste the deck's bearer token. It goes into the login
Keychain as a generic password (service = the app's bundle id, `dev.agentdeck.app` by default / account `deck-api-token`). It is
never written to a file, never put in a launch argument, and never redisplayed
once saved — the field only ever tells you whether one is stored.

The token is whatever the daemon was started with in `AGENT_DECK_TOKEN`. With
**no** `DECK_URL`, the app runs on the fixture and never opens a socket.

**The deck binds `127.0.0.1`.** To reach it from another machine, use a private
mesh (Tailscale) or an SSH tunnel — never a public port. This API can read every
message every agent on that Mac has sent.

---

## Fixture-backed vs wired to a real server

The contract is `../docs/client-api.md`. Everything
below is written against it, and `Tests/DeckKitTests/ContractTests.swift` pins
its payloads verbatim.

### Wired to the real server (`HTTPDeckClient`)

| Route | What uses it |
|---|---|
| `GET /v1/agents` | the whole sidebar, in **one** request |
| `GET /v1/threads` | peer threads, fetched only when wanted |
| `GET /v1/agents/{name}` | the settings panel |
| `PATCH /v1/agents/{name}` | title, description, section, pinned, notifications |
| `GET /v1/threads/{id}/messages?since=&limit=` | opening a thread and catching up after a drop |
| `POST /v1/threads/{id}/messages` | sending |
| `POST /v1/agents/{name}/read` | clearing unread |
| `GET /v1/stream` | live messages, agent states, unread counts, `hello` |
| `POST /v1/agents` | the "+" at the top of the sidebar (§11) |
| `GET /v1/approvals` · `POST /v1/approvals/{id}` | the tool-call card in the thread, polled while one is open (§12) |
| `GET/POST /v1/routines` · `PATCH/DELETE /v1/routines/{id}` | the routines panel in the inspector (§13) |

The last three landed in the contract while this client was being written, so
their adapters are exercised by `StubPerformer` against §11–§13's payloads and
have **not yet been run against a live deck**. Nothing on the daemon at :7788
was written to.

Also real: bearer auth on every route; refusals read off `reason` and never off
the status alone; opaque cursors echoed back and never constructed; epoch-second
timestamps; a silence watchdog that reconnects when the deck's heartbeat stops.

### Fixture-backed (`FixtureDeckClient`)

Asked for by name with `DECK_FIXTURE=1`, never fallen into. Canned agents, threads and transcripts
shaped exactly like the wire — `direct:<name>` / `peer:<a>|<b>` ids, lowercase
names, `"owner"` as the owner, sortable cursors, server-formatted previews. It
exists so the entire UI can be built and reviewed with no deck running. Its
`events()` yields one heartbeat and then stays open, so the indicator reads
**Connected** rather than **Reconnecting**.

### The three rules the setup routes are held to

1. **No button grants a permission it has not named.** §12 sends each option's
   `summary` — the rule as a sentence — and the `rule` itself. An option
   arriving without a summary fails to decode; so does an *available* standing
   option with no rule attached, because its sentence would describe something
   this client never saw. Those approvals are counted into a visible notice
   rather than drawn as unlabelled buttons. "Always allow" reads, verbatim:

   > allow `gh pr*` in ~/Projects/acme, never ask again

   The option §12 marks `available: false` — credentials, payments,
   irreversible calls — is drawn **disabled with its explanation**, and is
   refused before the socket rather than after a 409.

2. **The client never sends `next_run_at`.** It states `{kind: cron, spec, tz}`
   and reads back whatever the deck computed. §13.1 makes sending one a
   `409 next_run_at_is_computed`; the reason it is loud is that a routine
   scheduled in the past is due on every tick, forever.

3. **A refusal is a sentence, not a status.** `POST /v1/agents` refuses with
   `name_taken`, `too_deep`, `too_many_live`, `no_such_boss`, `no_such_cwd`,
   `unknown_engine` and `missing_field`, and each one is a different thing for
   the user to change. The deck's own `detail` is kept, because that is where
   "there are already 8 agents running" lives. Same for §12's refusals:
   `already_answered` and `expired` remove the card *and say that this tap
   changed nothing* — neither is a success.

### Deliberately not built

- **The notification switch silences, it does not promise.** The deck pushes
  per desk over WhatsApp when one starts a job, reports, gets blocked or
  finishes, and the per-agent flag is what mutes that. What this build can send
  on is a single fact in `NotificationDelivery`, and the line under the switch
  is derived from it — so the panel cannot end up describing a delivery that
  changed. The weak part is that the fact is asserted at build time: point the
  app at a deck without that push with `DECK_URL` and the sentence is wrong
  again, until `GET /v1/agents` (or a capability route) says what it delivers.
- **No scroll-back.** `before=` paging and `has_more_before` are modelled and
  decoded, but the thread view only loads the newest page. Long histories stop
  at that page.
- **Most inline images will not draw.** The deck derives attachments from
  message text and serves no files. An image is drawn only when its path exists
  on this Mac; otherwise the path stays in the text, monospaced. Same rule for
  avatars.
- **`name` is not editable, and `reports_to` is only asked at hire time.** The
  name is the identity every thread id and org edge is keyed on. Naming a new
  desk's boss is what hiring is, so the "+" form asks; changing it afterwards
  through the settings panel is `reports_to_is_not_a_setting` by contract, and
  the panel does not offer it.
- **No plugins.** The sidebar footer carries the reference's Plugins row and
  says on its face that there are none: this deck has no plugin API, and a row
  that opened an empty sheet would be worse than one that admits it.
- **No firing and no starting a session.** §11 has `DELETE /v1/agents/{name}`
  and `POST /v1/agents/{name}/start`, and neither is wired here: the second
  opens a real Terminal window on the Mac with no undo, and both belong behind
  a confirmation this pass did not design.
- **Approvals arrive by polling, because the deck does not push them.**
  `GET /v1/stream` carries no approval frame — §8 lists every type it sends and
  an ask is not one — so an open conversation re-asks `GET /v1/approvals` every
  two seconds and stops the moment nothing is open. That is a request every two
  seconds for the whole time a thread is on screen, and it is the only way a
  tool call can reach a window that is already open. An `approval` /
  `approval_resolved` frame on `/v1/stream` would let this poll be deleted.
- **The deck publishes nothing about a tool call it did *not* stop on.**
  `/v1/approvals` holds live *questions* only. There is no route that lists what
  an agent actually ran, so the calls covered by an existing rule — which is
  most of them — cannot be drawn at all. The conversation shows the work that
  paused; the work that flowed is still invisible.
- **The peer transcript still does not say who is speaking.** A relayed line's
  `to <name>` / `from <name>` chip opens the `peer:` thread it came from, which
  is the full record — and once you are in there both agents draw as the same
  grey bubble with no author on it, because the bubble only distinguishes the
  owner from everyone else. Inline attribution in the desk's own conversation
  is what this pass built; the destination it now links to is unchanged and
  still reads as one voice.
- **No iOS target.** That needs signing decisions this repo does not make.

## Layout

```
Sources/DeckKit/   domain, transport, presentation. Imports no UI framework.
Sources/DeckUI/    SwiftUI views + DeckStore, the one observable object.
Sources/DeckApp/   the @main App and the fixture/HTTP switch.
Tests/DeckKitTests/ 374 XCTest tests. No network, no timers, no sleeps.
UITests/           XCUITest: drives the built app and reads the real screen.
```

`DeckKit` decides; `DeckUI` draws. That split is what makes the three rules
below testable without a running app.

### Driving the real app

The unit tests stop at the view models, so for a long time nothing in this
repo had ever observed the screen. `UITests/` closes that: XCUITest launches
the built `Shaliach.app`, clicks and types into it, and asserts on the
accessibility tree the running app actually publishes.

```console
$ xcodegen generate --spec UITests/project.yml --project UITests
$ xcodebuild -project UITests/DeckUITests.xcodeproj -scheme DeckUITests \
    -destination 'platform=macOS,arch=arm64' test
```

The app target there compiles the same `Sources/DeckApp` and links the same
package products, so there is no second copy of the app. The `.xcodeproj` is
generated and not committed. Anything that sends or closes a thread runs under
`DECK_FIXTURE=1`, so no test can write into a real session.

**Reading this app's accessibility tree from outside.** `AXWindows` only
reports windows on the **active Space**, and this Mac has 18 of them, so an
app on another Space answers "0 windows" to System Events. That is macOS, not
this app — a bare SwiftUI app and a plain AppKit app in hand-made bundles both
do it, and Terminal only looks immune because its windows are on the Space you
are asking from. Activate the app first, or read `AXMainWindow` /
`AXFocusedWindow`, which resolve from any Space.

### The things pinned hardest

1. **A reconnect loses nothing and duplicates nothing.**
   `ReconnectTests` drops the stream mid-conversation, has the reopened stream
   replay a frame and the refetch overlap at the cursor, and asserts every
   message appears exactly once in cursor order — plus that the refetch echoed
   back the cursor already held.
2. **The sidebar costs one request.** `SidebarTests` builds a 500-agent roster
   and asserts the call log is exactly `[.roster]`. `SidebarSnapshot.build` takes
   no client, so a per-agent request is not reachable from it.
3. **A view-only thread has no composer.** `ReadOnlyThreadTests` asserts the
   footer is what a peer thread presents and the text field is what a direct one
   presents — and that a two-participant thread with `read_only: false` still
   gets a composer, because the flag is the only authority.
4. **No button grants a permission it has not named.** `ApprovalTests` pins the
   sentence on every option, that an option without one cannot be constructed,
   that an *available* standing option with no rule is refused, and that either
   kind of unreadable request is dropped *and counted* rather than rendered.
5. **A search that matches nothing says so.** `SidebarSearchTests` asserts the
   empty-state sentence with the query quoted back — an empty list is
   indistinguishable from a roster that failed to load.
6. **A face is the same face at the next launch.** `AvatarLookTests` writes out
   the FNV-1a derived shape and tint, so swapping in Swift's per-process
   `hashValue` fails there rather than in a screenshot months later.
7. **A dispatch is never drawn as the desk answering him.** §6.2 inlines a
   desk's peer traffic into its own conversation and states the attribution
   with nothing but a differing `thread_id`. `RelayLineTests` pins all three
   render cases, the name coming off the `peer:` id rather than the author, a
   pair id naming a desk that has since renamed itself, an unparseable id —
   which must still never fall back to a plain bubble — and the field-absent
   case, where the id decodes to `""` and comparing it would relabel a whole
   conversation as overheard traffic. `RelayTests` reads the same three cases
   off the running app's accessibility tree.

### Accessibility

Semantic controls throughout (`List`, `Form`, `Toggle`, `TextField`, `Button`),
so keyboard traversal and focus rings are the system's. Sidebar rows collapse to
one spoken label (name, title, unread, full timestamp, preview) instead of five
fragments. The connection state is a word, not only a colour. Text fields carry
their captions on screen and as labels. Colours are Apple's semantic ones and
system materials, so light, dark and increased-contrast all follow the OS.
