import XCTest
@testable import DeckKit

/// **"Working in /home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17".**
///
/// That string is what this app puts in the slot where the reference product
/// puts a live picture of the agent's browser, captioned `COS's screen`. The
/// whole server side of that picture is already deployed — three routes on the
/// same bearer token this client already holds — and no line of this app had
/// ever called one.
///
/// Four facts this file measures rather than assumes.
///
/// 1. **A frame without an age is a lie.** `server/screen.py` says it in those
///    words: a checkout page looks identical a second old and twenty minutes
///    dead. So a frame carries `X-Frame-Age`, the client keeps ageing it after
///    it arrives, and a frame past the deck's own `stale_after` is never handed
///    to the view as live. A frame whose age never arrived is never live either.
/// 2. **A click is scaled back into display space.** The JPEG is drawn at
///    whatever size the panel has; `screen/input` takes coordinates in the
///    display's own 1280x800. Measured live against the box:
///    `{"action":"click","x":99999,"y":10}` answers
///    `400 bad_input — click (99999,10) is outside the 1280x800 screen`. The
///    deck refuses rather than clamps, on purpose, so that a client which
///    clamps hides its own arithmetic bug. This one does not clamp either: a
///    point outside the drawn image is not sent at all.
/// 3. **The poll runs only while somebody is looking.** One JPEG a second is
///    one `ffmpeg` grab inside the container, on the same machine as the
///    Chromium being watched. Hiding the panel, or the window losing key, stops
///    it — counted here, with the running case counted in the same test so the
///    harness proves it can see both.
/// 4. **`running: false` is a state, not an error.** Measured live against
///    `atlas` on the box: a 200 with the whole status body and
///    `computer.running: false`. The panel draws that as a sentence.
@MainActor
final class AgentComputerTests: XCTestCase {

    // MARK: - the wire, pinned to what the real deck actually answered

    /// Verbatim from `GET /v1/agents/atlas/screen` on the box at 10.99.0.1,
    /// with nothing edited, before any container had ever been started there.
    /// The state the panel most needs to draw, and for a long time the only one
    /// this app could see.
    static let atlasStatusJSON = """
    {"desk":"atlas","computer":{"running":false,"image":"agent-deck/desk-computer:1",
     "container":"deck-desk-atlas"},"display":":99","width":1280,"height":800,
     "stale_after":30.0,"frame_url":"/v1/agents/atlas/screen.jpg",
     "input_url":"/v1/agents/atlas/screen/input","generated_at":1788694331.4865828}
    """

    /// The same route with the machine actually up. **Measured**, verbatim off
    /// the same deck once a desk computer was running: the frame it describes
    /// came back a real `1280x800` baseline JPEG with `x-frame-age: 0.241`, and
    /// a click posted against it landed where it was aimed.
    static let runningStatusJSON = """
    {"desk":"atlas","computer":{"running":true,"image":"agent-deck/desk-computer:1",
     "container":"deck-desk-atlas"},"display":":99","width":1280,"height":800,
     "stale_after":30.0,"frame_url":"/v1/agents/atlas/screen.jpg",
     "input_url":"/v1/agents/atlas/screen/input","generated_at":1788712603.9547608}
    """

    func testTheStatusOfADeskWithNoMachineDecodesAsAStateAndNotAsAFailure() throws {
        let status = try DeckCoding.decoder.decode(
            AgentScreenStatus.self, from: Data(Self.atlasStatusJSON.utf8))

        XCTAssertEqual(status.desk, "atlas")
        XCTAssertFalse(status.isRunning,
                       "`computer.running: false` decoded as running. That is the "
                       + "one state the panel has ever been able to see live.")
        XCTAssertEqual(status.container, "deck-desk-atlas")
        XCTAssertEqual(status.image, "agent-deck/desk-computer:1")
        XCTAssertEqual(status.display, ":99")
        XCTAssertEqual(status.width, 1280)
        XCTAssertEqual(status.height, 800)
        XCTAssertEqual(status.staleAfter, 30.0)
        XCTAssertEqual(status.generatedAt.timeIntervalSince1970, 1788694331.4865828,
                       accuracy: 0.001)
    }

    func testAStatusWithAMachineUpReadsAsRunning() throws {
        let status = try DeckCoding.decoder.decode(
            AgentScreenStatus.self, from: Data(Self.runningStatusJSON.utf8))
        XCTAssertTrue(status.isRunning)
        XCTAssertEqual(status.width, 1280)
    }

    // MARK: - 1. a frame without an age is a lie

    private func frame(serverAge: TimeInterval?, at received: Date) -> AgentScreenFrame {
        AgentScreenFrame(jpeg: Data([0xFF, 0xD8, 0xFF]), serverAge: serverAge,
                         display: ":99", receivedAt: received)
    }

    /// The header is the age at the moment the bytes left the deck. The frame
    /// keeps getting older on this Mac afterwards, and a poll that stops
    /// answering leaves the last one on screen — so the age the panel says out
    /// loud is the deck's plus however long this client has been holding it.
    func testAFrameKeepsAgeingAfterItArrives() {
        let arrived = Date(timeIntervalSince1970: 1_000_000)
        let shot = frame(serverAge: 0.4, at: arrived)

        XCTAssertEqual(shot.age(at: arrived) ?? -1, 0.4, accuracy: 0.001,
                       "the age at the moment of arrival is the deck's own header")
        XCTAssertEqual(shot.age(at: arrived.addingTimeInterval(40)) ?? -1, 40.4,
                       accuracy: 0.001,
                       "a frame held for 40 seconds still reported the deck's age. "
                       + "A feed that died leaves the last frame on screen for ever "
                       + "and this is the only arithmetic that notices.")
    }

    /// `stale_after` is the deck's number, read off the status call, not a
    /// constant in this app.
    func testAFramePastTheDecksOwnStaleAfterIsNeverHandedOverAsLive() throws {
        let status = try Self.runningStatus()
        let arrived = Date(timeIntervalSince1970: 2_000_000)
        let shot = frame(serverAge: 0.2, at: arrived)

        let fresh = AgentScreenState.forFrame(
            shot, staleAfter: status.staleAfter, now: arrived.addingTimeInterval(5))
        XCTAssertEqual(fresh, .live(shot),
                       "a five-second-old frame is not live, so the panel can never "
                       + "show anything as current")

        let old = AgentScreenState.forFrame(
            shot, staleAfter: status.staleAfter, now: arrived.addingTimeInterval(31))
        guard case .stale(let kept, let age) = old else {
            return XCTFail("a frame 31.2s old, past the deck's stale_after of "
                           + "\(status.staleAfter), was handed over as \(old). That is "
                           + "a dead checkout page drawn as what the agent is doing now.")
        }
        XCTAssertEqual(kept, shot, "the stale state must keep the frame — it is still "
                       + "worth seeing, it just is not now")
        XCTAssertEqual(age, 31.2, accuracy: 0.001)
    }

    /// The other half of the same rule, and the one a client is likeliest to
    /// get wrong: no header at all must not read as age zero.
    func testAFrameThatNeverSaidHowOldItIsIsNotDrawnAsLiveEither() {
        let arrived = Date(timeIntervalSince1970: 3_000_000)
        let shot = frame(serverAge: nil, at: arrived)

        XCTAssertNil(shot.age(at: arrived),
                     "a missing X-Frame-Age was turned into a number. Defaulting to "
                     + "zero is exactly the lie the header exists to prevent.")
        XCTAssertEqual(
            AgentScreenState.forFrame(shot, staleAfter: 30, now: arrived),
            .ageUnknown(shot),
            "a frame with no age was drawn as live")
    }

    // MARK: - 2. the coordinate transform, swept

    /// Every scale the panel can be drawn at, and both extremes of the display.
    /// The thumbnail is ~240pt wide, the opened view is whatever the sheet is,
    /// and a Retina backing store makes neither of them 1280.
    func testAClickIsScaledBackIntoDisplaySpaceAtEveryScale() throws {
        for (viewW, viewH) in [(1280.0, 800.0),    // 1:1
                               (640.0, 400.0),     // a half
                               (320.0, 200.0),     // the thumbnail
                               (240.0, 150.0),
                               (2560.0, 1600.0),   // bigger than the display
                               (337.0, 210.625)] { // a size nobody chose
            let fit = try XCTUnwrap(
                ScreenFit(displayWidth: 1280, displayHeight: 800,
                          viewWidth: viewW, viewHeight: viewH),
                "no fit for \(viewW)x\(viewH)")

            XCTAssertEqual(fit.displayPoint(atViewX: fit.drawnX, y: fit.drawnY),
                           DisplayPoint(x: 0, y: 0),
                           "the top-left of the drawn image at \(viewW)x\(viewH) did "
                           + "not map to the top-left of the display")

            let lastX = fit.drawnX + fit.drawnWidth - 0.001
            let lastY = fit.drawnY + fit.drawnHeight - 0.001
            XCTAssertEqual(fit.displayPoint(atViewX: lastX, y: lastY),
                           DisplayPoint(x: 1279, y: 799),
                           "the last drawable point at \(viewW)x\(viewH) did not map "
                           + "to the last pixel of the 1280x800 display. Off by one "
                           + "here is a 400 from the deck, which refuses rather than "
                           + "clamps.")

            let middle = try XCTUnwrap(
                fit.displayPoint(atViewX: fit.drawnX + fit.drawnWidth / 2,
                                 y: fit.drawnY + fit.drawnHeight / 2))
            XCTAssertEqual(Double(middle.x), 640, accuracy: 1,
                           "the centre at \(viewW)x\(viewH) landed at x=\(middle.x)")
            XCTAssertEqual(Double(middle.y), 400, accuracy: 1,
                           "the centre at \(viewW)x\(viewH) landed at y=\(middle.y)")
        }
    }

    /// A fitted image letterboxes. The bars are not the agent's screen, and a
    /// click on one is not a click the deck should ever be asked to make.
    func testAClickOutsideTheDrawnImageIsNotAPointAtAll() throws {
        // 1280x800 drawn into a square: bars top and bottom.
        let fit = try XCTUnwrap(ScreenFit(displayWidth: 1280, displayHeight: 800,
                                          viewWidth: 400, viewHeight: 400))
        XCTAssertEqual(fit.drawnWidth, 400, accuracy: 0.001)
        XCTAssertEqual(fit.drawnHeight, 250, accuracy: 0.001)
        XCTAssertEqual(fit.drawnY, 75, accuracy: 0.001, "the image is not centred")

        for (x, y, place) in [(200.0, 10.0, "the bar above the image"),
                              (200.0, 390.0, "the bar below the image"),
                              (-1.0, 200.0, "left of the view"),
                              (401.0, 200.0, "right of the view"),
                              (200.0, 74.9, "one point above the first row"),
                              (200.0, 325.1, "one point below the last row")] {
            XCTAssertNil(fit.displayPoint(atViewX: x, y: y),
                         "a click on \(place) produced a display point. Nothing "
                         + "outside the picture may be sent.")
        }
    }

    /// And the model must actually refuse to send one. A mapping that returns
    /// nil is worth nothing if the caller then sends a default.
    func testAClickOutsideTheImageNeverReachesTheDeck() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme", client: client)
        await model.refreshStatus()

        let fit = try XCTUnwrap(ScreenFit(displayWidth: 1280, displayHeight: 800,
                                          viewWidth: 400, viewHeight: 400))
        await model.click(atViewX: 200, y: 10, in: fit)
        XCTAssertEqual(client.inputs, [],
                       "a click on the letterbox was sent to the deck as \(client.inputs)")

        await model.click(atViewX: 200, y: 200, in: fit)
        XCTAssertEqual(client.inputs, [.click(x: 640, y: 400)],
                       "a click in the middle of the picture did not reach the deck "
                       + "as the centre of the display — so nothing above is testing "
                       + "a path that works")
    }

    /// The deck's refusal is a real one, measured. This client must not paper
    /// over it by clamping into range before sending.
    func testTheClientNeverClampsAPointIntoRange() throws {
        let fit = try XCTUnwrap(ScreenFit(displayWidth: 1280, displayHeight: 800,
                                          viewWidth: 320, viewHeight: 200))
        XCTAssertNil(fit.displayPoint(atViewX: 320, y: 100),
                     "the point one past the right edge was clamped to x=1279 instead "
                     + "of refused. Measured on the box: the deck answers "
                     + "`400 bad_input — click (99999,10) is outside the 1280x800 "
                     + "screen`, deliberately not clamped, so that a client's "
                     + "arithmetic bug is visible instead of clicking somewhere "
                     + "plausible.")
        XCTAssertEqual(fit.displayPoint(atViewX: 319.999, y: 199.999),
                       DisplayPoint(x: 1279, y: 799))
    }

    // MARK: - 3. the poll runs only while somebody is looking

    func testThePollRunsWhileVisibleAndStopsTheMomentItIsHidden() async throws {
        // Calibration: left alone, the poll polls.
        let busy = FakeScreenClient(status: try Self.runningStatus())
        let watched = AgentComputerModel(desk: "acme", displayName: "Acme",
                                         client: busy, clock: BudgetClock(budget: 6))
        watched.setWindowActive(true)
        watched.addWatcher()
        await watched.pollLoopFinished()
        XCTAssertEqual(busy.frameCalls, 6,
                       "an open panel asked for \(busy.frameCalls) frames in six "
                       + "ticks, so this test cannot see a poll at all and the "
                       + "assertion below is worthless")

        // The rule: hidden on the second tick, and it never asks again.
        let quiet = FakeScreenClient(status: try Self.runningStatus())
        let quietClock = BudgetClock(budget: 50)
        let hidden = AgentComputerModel(desk: "acme", displayName: "Acme",
                                        client: quiet, clock: quietClock)
        quietClock.onWait = { [weak hidden] tick in
            if tick == 2 { Task { @MainActor in hidden?.removeWatcher() } }
        }
        hidden.setWindowActive(true)
        hidden.addWatcher()
        await hidden.pollLoopFinished()

        XCTAssertEqual(quiet.frameCalls, 2,
                       "the panel was hidden after two frames and the poll went on to "
                       + "ask \(quiet.frameCalls) times. One JPEG a second is one "
                       + "ffmpeg grab inside the container, on the same machine as "
                       + "the Chromium being watched, on a laptop.")
        XCTAssertFalse(hidden.isPolling,
                       "the poll is still running with nothing on screen")
    }

    /// The window losing key is the same thing as the panel going away: he is
    /// looking at something else.
    func testThePollStopsWhenTheWindowIsNoLongerKey() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let clock = BudgetClock(budget: 50)
        let model = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: client, clock: clock)
        clock.onWait = { [weak model] tick in
            if tick == 3 { Task { @MainActor in model?.setWindowActive(false) } }
        }
        model.setWindowActive(true)
        model.addWatcher()
        await model.pollLoopFinished()

        XCTAssertEqual(client.frameCalls, 3,
                       "the app went to the background after three frames and kept "
                       + "polling to \(client.frameCalls)")
        XCTAssertFalse(model.isPolling)
    }

    /// Both halves of the gate, stated once so a future change to either one
    /// has somewhere to fail.
    func testNothingPollsUntilThePanelIsBothVisibleAndInTheKeyWindow() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: client, clock: BudgetClock(budget: 3))
        model.addWatcher()                        // on screen, window not key
        XCTAssertFalse(model.isPolling, "the panel polled a background window")
        model.setWindowActive(true)
        XCTAssertTrue(model.isPolling,
                      "the panel is visible in the key window and is not polling, so "
                      + "he is looking at a picture that never updates")
        await model.pollLoopFinished()
    }

    /// **Two things watch one machine, and neither can switch the other off.**
    ///
    /// The thumbnail sits in the inspector; pressing Open puts the take-over on
    /// top of it. On screen one is in front of the other, but in the view tree
    /// they are not nested — a sheet is its own window, so the panel underneath
    /// never disappears and never gets a second `onAppear` to turn itself back
    /// on with. A single boolean gate cannot express that, and the version of
    /// this panel that had one left the thumbnail frozen on whatever frame was
    /// showing when Open was first pressed, for the rest of the session.
    func testClosingTheTakeOverLeavesTheThumbnailBehindItStillPolling() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: client, clock: BudgetClock(budget: 200))
        model.setWindowActive(true)

        model.addWatcher()                        // the thumbnail appears
        XCTAssertTrue(model.isPolling,
                      "the thumbnail alone does not poll, so the rest of this test "
                      + "cannot tell a working gate from a stuck one")

        model.addWatcher()                        // Open pressed: the take-over too
        XCTAssertTrue(model.isPolling)

        model.removeWatcher()                     // the take-over closes
        XCTAssertTrue(model.isPolling,
                      "closing the take-over stopped the thumbnail that is still on "
                      + "screen behind it, so it freezes on its last frame for the "
                      + "rest of the session")

        model.removeWatcher()                     // the inspector closes too
        XCTAssertFalse(model.isPolling,
                       "the last watcher went away and the poll kept running. One JPEG "
                       + "a second is one ffmpeg grab inside the container, on the same "
                       + "machine as the Chromium being watched.")
    }

    /// A count that can go negative is a poll nothing can ever stop: two
    /// `onDisappear`s for one `onAppear` would park it at -1 and every later
    /// appear would leave it at zero with the panel on screen.
    func testTheWatcherCountNeverGoesBelowNone() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: client, clock: BudgetClock(budget: 200))
        model.setWindowActive(true)
        model.removeWatcher()
        model.removeWatcher()
        XCTAssertFalse(model.isPolling, "a panel nobody ever opened is polling")

        model.addWatcher()
        XCTAssertTrue(model.isPolling,
                      "the panel opened and did not poll, because the count had been "
                      + "driven below zero and one appear could not climb back to one")
        model.removeWatcher()
        XCTAssertFalse(model.isPolling)
    }

    // MARK: - 4. every state, in words

    func testADeskWithNoMachineSaysSoInWordsRatherThanAnErrorCode() async throws {
        let client = FakeScreenClient(status: try Self.atlasStatus())
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client)
        await model.refreshStatus()

        XCTAssertEqual(model.state, .noComputer)
        let shown = AgentScreenPresentation(desk: "Atlas", state: model.state)
        XCTAssertEqual(shown.caption, "Atlas's screen")
        XCTAssertEqual(shown.statusLine, "No computer yet")
        XCTAssertEqual(shown.spokenLabel,
                       "Atlas has no computer running yet, so there is nothing to watch.")
        XCTAssertFalse(shown.showsOpen,
                       "an Open button over a machine that is not running takes him to "
                       + "a black rectangle")
        XCTAssertFalse(shown.isFrameLive)
    }

    func testTheStatesEachSayADifferentThingOutLoud() {
        let arrived = Date(timeIntervalSince1970: 4_000_000)
        let shot = frame(serverAge: 0.3, at: arrived)

        let spoken = [
            AgentScreenState.connecting,
            .live(shot),
            .stale(shot, age: 245),
            .ageUnknown(shot),
            .noComputer,
            .unavailable(.noFrame("the capture failed")),
        ].map { AgentScreenPresentation(desk: "Atlas", state: $0).spokenLabel }

        XCTAssertEqual(Set(spoken).count, spoken.count,
                       "two different states are announced with the same sentence, so "
                       + "listening to this panel cannot tell them apart: \(spoken)")
        XCTAssertEqual(spoken[0], "Atlas's screen. Connecting.")
        XCTAssertEqual(spoken[1], "Atlas's screen, live. Captured just now.")
        XCTAssertEqual(spoken[2],
                       "Atlas's screen, last captured 4 minutes ago. This is not what "
                       + "Atlas is looking at now.")
        XCTAssertEqual(spoken[3],
                       "Atlas's screen. This picture did not say how old it is, so it "
                       + "may not be current.")
    }

    /// A stale frame must be marked on screen as well as spoken — he glances at
    /// this panel, he does not read it.
    func testAStaleFrameIsMarkedOldAndAFreshOneIsNot() {
        let arrived = Date(timeIntervalSince1970: 5_000_000)
        let shot = frame(serverAge: 0.1, at: arrived)

        let live = AgentScreenPresentation(desk: "Atlas", state: .live(shot))
        XCTAssertTrue(live.isFrameLive)
        XCTAssertNil(live.statusLine,
                     "a live frame is captioned as though something were wrong with it")
        XCTAssertTrue(live.showsOpen)

        let old = AgentScreenPresentation(desk: "Atlas", state: .stale(shot, age: 245))
        XCTAssertFalse(old.isFrameLive,
                       "a four-minute-old frame is presented as live. This is the exact "
                       + "failure X-Frame-Age exists to prevent.")
        XCTAssertEqual(old.statusLine, "Last seen 4 minutes ago")
        XCTAssertNotNil(old.frame, "the old frame was thrown away rather than marked")
    }

    /// `no_frame` and `computer_not_running` are different things to do
    /// something about, so they are never the same sentence.
    func testTheMachinesRefusalsAreDifferentSentences() {
        let sentences: [String] = [
            ScreenRefusal.noComputerYet("deck-desk-atlas is not up"),
            .noFrame("the capture failed"),
            .dockerUnavailable("Docker is not answering"),
            .notResponding("timed out"),
            .inputRefused("xdotool failed"),
            .badInput("click (99999,10) is outside the 1280x800 screen"),
            .unknownAgent("no desk named 'nope'"),
            .unauthorized,
        ].map(\.sentence)

        XCTAssertEqual(Set(sentences).count, sentences.count,
                       "two refusals read identically: \(sentences)")
        for sentence in sentences {
            XCTAssertFalse(sentence.contains("_"),
                           "\(sentence) still has a wire slug in it")
        }
    }

    /// Measured verbatim on the box for `atlas`, whose machine is down.
    func testTheDecksOwnRefusalBodiesMapToThoseSentences() {
        XCTAssertEqual(
            ScreenRefusal(status: 409, body: Data("""
                {"ok":false,"reason":"computer_not_running",
                 "detail":"deck-desk-atlas is not up"}
                """.utf8)),
            .noComputerYet("deck-desk-atlas is not up"))
        XCTAssertEqual(
            ScreenRefusal(status: 400, body: Data("""
                {"ok":false,"reason":"bad_input",
                 "detail":"click (99999,10) is outside the 1280x800 screen"}
                """.utf8)),
            .badInput("click (99999,10) is outside the 1280x800 screen"))
        XCTAssertEqual(
            ScreenRefusal(status: 404, body: Data("""
                {"ok":false,"reason":"unknown_agent","detail":"no desk named 'nosuchdesk'"}
                """.utf8)),
            .unknownAgent("no desk named 'nosuchdesk'"))
        XCTAssertEqual(
            ScreenRefusal(status: 401, body: Data("""
                {"ok":false,"reason":"unauthorized","detail":"bearer token missing or wrong"}
                """.utf8)),
            .unauthorized)
    }

    /// A machine that is down must not leave the panel spinning for ever: the
    /// status call is the authority and it answers 200.
    func testAMachineThatIsDownEndsTheConnectingStateRatherThanHangingOnIt() async throws {
        let client = FakeScreenClient(status: try Self.atlasStatus())
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas",
                                       client: client, clock: BudgetClock(budget: 3))
        XCTAssertEqual(model.state, .connecting,
                       "the panel opens on something other than a wait, so there is no "
                       + "first state to leave")
        model.setWindowActive(true)
        model.addWatcher()
        await model.pollLoopFinished()

        XCTAssertEqual(model.state, .noComputer)
        XCTAssertEqual(client.frameCalls, 0,
                       "the panel asked for \(client.frameCalls) JPEGs of a machine the "
                       + "status call said is not running. Each one is a subprocess on "
                       + "the box for a picture that cannot exist.")
    }

    // MARK: - the headers, exactly as the deck spells them

    /// **The deck sends `x-frame-age`. This client asks for `X-Frame-Age`.**
    ///
    /// Measured on the box, verbatim off the wire:
    ///
    /// ```
    /// cache-control: no-store
    /// x-frame-age: 0.927
    /// x-frame-display: :99
    /// ```
    ///
    /// Every header name comes back lower-cased. HTTP field names are
    /// case-insensitive and `HTTPURLResponse` is documented to match them that
    /// way, but the whole staleness story hangs off this one lookup: if it ever
    /// missed, every frame would arrive with no age, and a client that treats
    /// "no age" as "fresh" would draw a dead browser as live. That is too much
    /// to rest on a documented courtesy, so it is pinned in the spelling the
    /// deck actually uses.
    func testTheAgeIsReadFromTheHeaderSpelledTheWayTheDeckSendsIt() async throws {
        let performer = HeaderStubPerformer(headers: [
            "cache-control": "no-store",
            "x-frame-age": "0.927",
            "x-frame-display": ":99",
            "content-type": "image/jpeg",
        ])
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.local")!,
                                    tokens: tokens, performer: performer)

        let frame = try await client.screenFrame(agent: "atlas")

        XCTAssertEqual(frame.serverAge ?? -1, 0.927, accuracy: 0.0001,
                       "the age header spelled the way the deck really sends it was "
                       + "not read, so every frame would arrive ageless")
        XCTAssertEqual(frame.display, ":99")
        XCTAssertEqual(
            AgentScreenState.forFrame(frame, staleAfter: 30, now: frame.receivedAt),
            .live(frame),
            "a frame the deck says is under a second old is not live")
    }

    /// The other half: a deck that sends no age at all must not become one.
    /// This is what a proxy stripping the header looks like, and it is the case
    /// the `ageUnknown` state exists for — the live deck always sends it, so
    /// this path is unreachable against the real box and is held here instead.
    func testAFrameServedWithNoAgeHeaderStaysAgeless() async throws {
        let performer = HeaderStubPerformer(headers: ["content-type": "image/jpeg"])
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.local")!,
                                    tokens: tokens, performer: performer)

        let frame = try await client.screenFrame(agent: "atlas")

        XCTAssertNil(frame.serverAge,
                     "a response with no age header produced an age anyway")
        XCTAssertEqual(
            AgentScreenState.forFrame(frame, staleAfter: 30, now: frame.receivedAt),
            .ageUnknown(frame),
            "a frame that never said how old it is was drawn as live")
    }

    // MARK: - the frozen machine, measured on the box

    /// **A container that is up but wedged is the case this panel exists for.**
    ///
    /// Measured against `deck-desk-atlas` while it was paused: the status call
    /// still answers `"running": true` — Docker calls a paused container
    /// running — and the frame refuses with
    /// `409 no_frame — Error response from daemon: Container deck-desk-atlas is
    /// paused, unpause the container before exec`.
    ///
    /// So "the machine is fine" and "the picture stopped" arrive together, and
    /// the rule is time: one dropped grab out of a running feed is a blip and
    /// the last picture is still true, but a feed that has been failing long
    /// enough for that picture to go stale is not a blip, and then the refusal
    /// is the thing he needs to read. Getting this backwards leaves a dead
    /// browser on screen looking live for ever.
    func testAWedgedMachineKeepsTheLastFrameBrieflyAndThenSaysWhatFailed() async throws {
        let paused = "Error response from daemon: Container deck-desk-atlas is "
            + "paused, unpause the container before exec"
        let start = Date(timeIntervalSince1970: 7_000_000)
        let clockNow = Stamp(start)

        let client = FakeScreenClient(status: try Self.runningStatus())
        client.frameReceivedAt = start
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas",
                                       client: client, now: { clockNow.value })

        // One good frame, so there is something to keep.
        await model.pollOnce()
        guard case .live = model.state else {
            return XCTFail("the feed never started, so nothing below is testing a "
                           + "frame being kept: \(model.state)")
        }

        // The container is paused. The grab fails; the picture is 5s old.
        // Built here rather than read off the model: the panel has no test-only
        // accessor, and adding one would be a production seam existing purely
        // so a test can look through it.
        let kept = AgentScreenFrame(jpeg: Data([0xFF, 0xD8, 0xFF, 0xE0]), serverAge: 0.2,
                                    display: ":99", receivedAt: start)
        client.frameError = .noFrame(paused)
        clockNow.value = start.addingTimeInterval(5)
        await model.pollOnce()
        XCTAssertEqual(model.state, .live(kept),
                       "a single dropped grab threw away a five-second-old picture. "
                       + "The deck's own stale_after is 30s and this one is still "
                       + "inside it.")

        // Still failing, and now the picture is older than the deck's own line.
        clockNow.value = start.addingTimeInterval(31)
        await model.pollOnce()
        XCTAssertEqual(model.state, .unavailable(.noFrame(paused)),
                       "a feed that has been dead for 31 seconds is still drawing its "
                       + "last picture. That is a wedged browser on screen looking "
                       + "like what the agent is doing now.")
        XCTAssertTrue(
            AgentScreenPresentation(desk: "Atlas", state: model.state).spokenLabel
                .contains("taking a picture of its screen failed"),
            "the panel does not say what actually broke")
    }

    // MARK: - the input bodies

    func testEachGestureIsTheBodyTheContractDocuments() throws {
        func body(_ input: ScreenInput) throws -> [String: String] {
            let raw = try JSONSerialization.jsonObject(
                with: try DeckCoding.encoder.encode(input)) as? [String: Any] ?? [:]
            return raw.mapValues { "\($0)" }
        }

        // The plain click now states its button and its count, because the
        // stage can make the other ones. `TakeoverInputTests` owns the rest of
        // the vocabulary — right, middle, double, move, scroll and drag.
        XCTAssertEqual(try body(.click(x: 301, y: 301)),
                       ["action": "click", "x": "301", "y": "301",
                        "button": "1", "count": "1"])
        XCTAssertEqual(try body(.type("hunter2")), ["action": "type", "text": "hunter2"])
        XCTAssertEqual(try body(.key("ctrl+l")), ["action": "key", "key": "ctrl+l"])
    }

    /// He types passwords through this. A backtick, a `$` or a leading dash is
    /// a character, and this client must not helpfully escape or trim any of it.
    func testTypedTextIsSentExactlyAsItWasTyped() throws {
        let awkward = "  -n `whoami` $HOME \\ \"quoted\"  "
        let raw = try JSONSerialization.jsonObject(
            with: try DeckCoding.encoder.encode(ScreenInput.type(awkward))) as? [String: Any]
        XCTAssertEqual(raw?["text"] as? String, awkward,
                       "the client altered the text on its way to the agent's keyboard")
    }

    // MARK: - helpers

    static func atlasStatus() throws -> AgentScreenStatus {
        try DeckCoding.decoder.decode(AgentScreenStatus.self,
                                      from: Data(atlasStatusJSON.utf8))
    }

    static func runningStatus() throws -> AgentScreenStatus {
        try DeckCoding.decoder.decode(AgentScreenStatus.self,
                                      from: Data(runningStatusJSON.utf8))
    }
}

// MARK: - doubles

/// A deck that answers without a socket. Counts what was asked of it, which is
/// how the poll tests can say "and then it stopped".
final class FakeScreenClient: AgentScreenClient, @unchecked Sendable {
    private let lock = NSLock()
    private var _statusCalls = 0
    private var _frameCalls = 0
    private var _inputs: [ScreenInput] = []

    let status: AgentScreenStatus
    var statusError: ScreenRefusal?
    var frameError: ScreenRefusal?
    var frameAge: TimeInterval? = 0.2
    /// When the frame is stamped as having arrived. Injected so a test can age
    /// a picture without waiting for a real half-minute.
    var frameReceivedAt: Date?

    init(status: AgentScreenStatus) { self.status = status }

    var statusCalls: Int { lock.lock(); defer { lock.unlock() }; return _statusCalls }
    var frameCalls: Int { lock.lock(); defer { lock.unlock() }; return _frameCalls }
    var inputs: [ScreenInput] { lock.lock(); defer { lock.unlock() }; return _inputs }

    func screenStatus(agent: String) async throws -> AgentScreenStatus {
        lock.lock(); _statusCalls += 1; lock.unlock()
        if let statusError { throw statusError }
        return status
    }

    func screenFrame(agent: String) async throws -> AgentScreenFrame {
        lock.lock(); _frameCalls += 1; let age = frameAge; let at = frameReceivedAt
        lock.unlock()
        if let frameError { throw frameError }
        return AgentScreenFrame(jpeg: Data([0xFF, 0xD8, 0xFF, 0xE0]), serverAge: age,
                                display: ":99", receivedAt: at ?? Date())
    }

    func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        lock.lock(); _inputs.append(input); lock.unlock()
    }
}

/// A performer that answers with chosen headers, so the frame headers can be
/// tested in the exact spelling the deck puts on the wire.
final class HeaderStubPerformer: RequestPerformer, @unchecked Sendable {
    let headers: [String: String]
    init(headers: [String: String]) { self.headers = headers }

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200,
                                       httpVersion: "HTTP/1.1", headerFields: headers)!
        return (Data([0xFF, 0xD8, 0xFF, 0xE0]), response)
    }
}

/// A date a test can move, so a picture can be aged past the deck's own
/// `stale_after` without spending thirty seconds doing it.
final class Stamp: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Date
    init(_ value: Date) { _value = value }
    var value: Date {
        get { lock.lock(); defer { lock.unlock() }; return _value }
        set { lock.lock(); _value = newValue; lock.unlock() }
    }
}

/// A clock with a fixed number of ticks in it, so a poll loop under test ends
/// on its own instead of being raced against a real second.
final class BudgetClock: ScreenPollClock, @unchecked Sendable {
    private let lock = NSLock()
    private var _waits = 0
    let budget: Int
    /// Called with the tick number, before the wait returns. The poll tests use
    /// it to hide the panel from underneath the running loop.
    var onWait: (@Sendable (Int) -> Void)?

    init(budget: Int) { self.budget = budget }

    var waits: Int { lock.lock(); defer { lock.unlock() }; return _waits }

    func wait(_ seconds: TimeInterval) async throws {
        lock.lock(); _waits += 1; let tick = _waits; lock.unlock()
        onWait?(tick)
        // Let anything the hook scheduled actually run before the next fetch.
        await Task.yield()
        await Task.yield()
        if tick >= budget { throw CancellationError() }
    }
}
