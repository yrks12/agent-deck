import XCTest
@testable import DeckKit

/// **"the agents desktop i see the chrome but i cant use it".**
///
/// The transport was finished and wired the whole time: the panel is mounted,
/// `HTTPDeckClient` conforms, and the stage already posted click, type and key.
/// What was missing is the *vocabulary*. One click gesture, no right button, no
/// double click, no wheel, no drag, and a keyboard that was a text field with a
/// Send button beside it. Nobody can use a computer through that.
///
/// Five things this file measures, none of which needs a window, an `NSImage`,
/// a layout pass or a container.
///
/// 1. **The scroll body is exactly the documented one.** `dy` and `dx` are
///    exclusive on the wire, so a scroll carrying both — or carrying a `dx: 0`
///    it never meant — is a `400` this client would have shipped.
/// 2. **The load-bearing one: every keysym this app can emit is legal.** The
///    deck matches `^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$` and answers `400
///    bad_input` to anything else. A table of the keys a person actually
///    presses, run through the real mapping, retires that whole class of
///    failure with no server in the room.
/// 3. **Printables are typed, not named.** `.key("a")` per letter is a keysym
///    round trip for every character, and `.key("A")` plus shift is a guess
///    about the agent's keyboard layout. Characters go down as text.
/// 4. **The open stage polls faster than the thumbnail.** One rate for both is
///    the defect he is describing: at 1 Hz he cannot see the result of his own
///    click, and at 4 Hz in every inspector thumbnail the box would spend its
///    day running `ffmpeg`.
/// 5. **A drag still refuses the letterbox — at both ends.** Nothing clamps.
@MainActor
final class TakeoverInputTests: XCTestCase {

    // MARK: - 1. the wire

    /// The documented body, compared as parsed JSON so key order is not what is
    /// being asserted.
    func testAScrollCarriesOneAxisAndNothingElse() throws {
        XCTAssertEqual(try Self.body(.scroll(x: 640, y: 400, dy: -3)),
                       ["action": "scroll", "x": 640, "y": 400, "dy": -3],
                       "the scroll body is not the one the route documents")

        XCTAssertEqual(try Self.body(.scroll(x: 12, y: 9, dx: 2)),
                       ["action": "scroll", "x": 12, "y": 9, "dx": 2],
                       "a horizontal scroll sent a dy the route did not ask for")
    }

    /// Right button, middle button and the double click — three gestures a
    /// desktop is unusable without, and three this app could not make.
    func testEveryGestureTheStageCanNowMakeIsTheDocumentedBody() throws {
        XCTAssertEqual(try Self.body(.click(x: 301, y: 301)),
                       ["action": "click", "x": 301, "y": 301,
                        "button": 1, "count": 1])
        XCTAssertEqual(try Self.body(.click(x: 8, y: 9, button: 3, count: 1)),
                       ["action": "click", "x": 8, "y": 9, "button": 3, "count": 1])
        XCTAssertEqual(try Self.body(.click(x: 8, y: 9, button: 2, count: 1)),
                       ["action": "click", "x": 8, "y": 9, "button": 2, "count": 1])
        XCTAssertEqual(try Self.body(.click(x: 8, y: 9, button: 1, count: 2)),
                       ["action": "click", "x": 8, "y": 9, "button": 1, "count": 2])
        XCTAssertEqual(try Self.body(.move(x: 40, y: 50)),
                       ["action": "move", "x": 40, "y": 50])
        XCTAssertEqual(try Self.body(.drag(x: 10, y: 20, toX: 30, toY: 40, button: 1)),
                       ["action": "drag", "x": 10, "y": 20,
                        "to_x": 30, "to_y": 40, "button": 1])
    }

    /// The wheel arrives as a continuous delta and the route takes notches in
    /// `1...10`. The translation is arithmetic, so it is measured as arithmetic.
    func testTheWheelBecomesNotchesTheRouteWillAccept() {
        XCTAssertEqual(ScreenScroll.notches(fromWheelDelta: 0), 0,
                       "a wheel that did not move sent a scroll anyway")
        XCTAssertEqual(ScreenScroll.notches(fromWheelDelta: 0.2), 1,
                       "the smallest real flick of the wheel did nothing")
        XCTAssertEqual(ScreenScroll.notches(fromWheelDelta: -0.2), -1)
        XCTAssertEqual(ScreenScroll.notches(fromWheelDelta: 900), 10,
                       "a fling sent \(ScreenScroll.notches(fromWheelDelta: 900)) "
                       + "notches; the route caps at ten and 400s above it")
        XCTAssertEqual(ScreenScroll.notches(fromWheelDelta: -900), -10)
    }

    // MARK: - 2. the keysyms

    /// **The whole `400 bad_input` class, retired without a server.**
    ///
    /// Every keysym this app can put on the wire has to match the deck's own
    /// `^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$`. Rather than trusting the table by
    /// reading it, the keys a person actually presses are pressed here and
    /// whatever comes out is matched.
    func testEveryKeysymThisAppCanEmitIsOneTheDeckWillAccept() throws {
        let legal = try NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$")
        var emitted: [String] = []

        for (label, stroke) in Self.keyboard {
            guard let input = ScreenKeys.input(for: stroke) else {
                XCTFail("\(label) produced no gesture at all, so that key does "
                        + "nothing on the agent's computer")
                continue
            }
            switch input {
            case .key(let keysym):
                emitted.append(keysym)
                let range = NSRange(keysym.startIndex..., in: keysym)
                XCTAssertNotNil(
                    legal.firstMatch(in: keysym, range: range),
                    "\(label) emits the keysym '\(keysym)', which the deck answers "
                    + "400 bad_input to. That key is dead in the take-over.")
            case .type(let text):
                XCTAssertFalse(text.isEmpty, "\(label) typed nothing")
            default:
                XCTFail("\(label) came out as \(input), which is not a keystroke")
            }
        }

        // Calibration: the table has to be exercising the named-key half, or
        // the regex above is matching an empty list and proving nothing.
        XCTAssertGreaterThanOrEqual(emitted.count, 20,
                                    "only \(emitted.count) keysyms came out of a table "
                                    + "of \(Self.keyboard.count) keys, so this test is "
                                    + "not looking at the mapping")
        XCTAssertTrue(emitted.contains("Return") && emitted.contains("BackSpace")
                        && emitted.contains("ctrl+c") && emitted.contains("shift+Tab"),
                      "the named keys and the chords are not both in \(emitted)")
    }

    /// Command stays with his Mac. Forwarding it would send ⌘Q to a container
    /// and take the app he is using out from under him.
    func testCommandIsNeverForwardedToTheAgentsComputer() {
        let quit = KeyStroke(keyCode: 12, characters: "q", unmodified: "q",
                             command: true)
        XCTAssertNil(ScreenKeys.input(for: quit),
                     "Command-Q was forwarded to the agent's computer")
    }

    // MARK: - 3. printables

    func testPrintableCharactersAreTypedRatherThanNamed() throws {
        let cases: [(String, KeyStroke)] = [
            ("a", KeyStroke(keyCode: 0, characters: "a", unmodified: "a")),
            ("A", KeyStroke(keyCode: 0, characters: "A", unmodified: "A", shift: true)),
            ("7", KeyStroke(keyCode: 26, characters: "7", unmodified: "7")),
            ("a space", KeyStroke(keyCode: 49, characters: " ", unmodified: " ")),
            ("$", KeyStroke(keyCode: 21, characters: "$", unmodified: "$", shift: true)),
            ("é", KeyStroke(keyCode: 14, characters: "é", unmodified: "é")),
        ]
        for (label, stroke) in cases {
            guard case .type(let text)? = ScreenKeys.input(for: stroke) else {
                XCTFail("'\(label)' did not go down as typed text; it came out as "
                        + String(describing: ScreenKeys.input(for: stroke)))
                continue
            }
            XCTAssertEqual(text, stroke.characters,
                           "'\(label)' was altered on the way to the keyboard")
        }
    }

    // MARK: - 4. the two rates

    /// The stage waits the fast interval and the thumbnail waits the slow one.
    /// Each frame is a fresh `docker exec ffmpeg`, so 4 Hz is a ceiling and not
    /// a target: fast under his hands, slow in a thumbnail nobody is driving.
    func testTheOpenStagePollsFasterThanTheThumbnail() async throws {
        let thumbnailClock = IntervalClock(budget: 3)
        let thumbnail = AgentComputerModel(desk: "acme", displayName: "Acme",
                                           client: try Self.client(),
                                           clock: thumbnailClock)
        thumbnail.setWindowActive(true)
        thumbnail.addWatcher(.thumbnail)
        await thumbnail.pollLoopFinished()
        XCTAssertEqual(thumbnailClock.waits, [2.0, 2.0, 2.0],
                       "the thumbnail polled at \(thumbnailClock.waits); every one of "
                       + "those is an ffmpeg grab in the container for a picture "
                       + "nobody is driving")

        let stageClock = IntervalClock(budget: 3)
        let stage = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: try Self.client(), clock: stageClock)
        stage.setWindowActive(true)
        stage.addWatcher(.takeover)
        await stage.pollLoopFinished()
        XCTAssertEqual(stageClock.waits, [0.25, 0.25, 0.25],
                       "the open take-over polled at \(stageClock.waits), so he cannot "
                       + "see the result of his own click")
    }

    /// Opening the take-over over a thumbnail that is already polling has to
    /// raise the rate then and there — not on some later tick, and not never.
    func testOpeningTheTakeOverRaisesTheRateOfThePollAlreadyRunning() async throws {
        let clock = IntervalClock(budget: 4)
        let model = AgentComputerModel(desk: "acme", displayName: "Acme",
                                       client: try Self.client(), clock: clock)
        clock.onWait = { [weak model] tick in
            if tick == 1 { Task { @MainActor in model?.addWatcher(.takeover) } }
        }
        model.setWindowActive(true)
        model.addWatcher(.thumbnail)
        while model.isPolling { await model.pollLoopFinished() }

        XCTAssertEqual(clock.waits, [2.0, 0.25, 0.25, 0.25],
                       "Open was pressed after the first tick and the poll stayed at "
                       + "\(clock.waits)")

        // And closing it puts the box back down to the thumbnail's rate.
        model.removeWatcher(.takeover)
        XCTAssertEqual(model.pollInterval, 2.0,
                       "the take-over closed and the box is still being grabbed four "
                       + "times a second for a thumbnail")
    }

    // MARK: - 5. the letterbox, at both ends of a drag

    /// A 400x400 slot draws a 1280x800 display as 400x250 with 75pt of nothing
    /// above and below. Those bars are not the agent's screen.
    func testADragThatStartsOrEndsOnTheLetterboxIsNotSentAtAll() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme", client: client)
        await model.refreshStatus()
        let fit = try XCTUnwrap(model.fit(inViewWidth: 400, height: 400))

        XCTAssertNil(fit.displayPoint(atViewX: 200, y: 10),
                     "the top bar is being read as a point on the agent's screen")

        await model.drag(fromViewX: 200, y: 10, toViewX: 200, y: 200, in: fit)
        await model.drag(fromViewX: 200, y: 200, toViewX: 200, y: 390, in: fit)
        XCTAssertEqual(client.inputs, [],
                       "a drag with an end on the letterbox went out as \(client.inputs); "
                       + "the deck answers 400 rather than clamping, on purpose")

        // Calibration: a drag entirely on the picture is sent, in display space.
        await model.drag(fromViewX: 0, y: 75, toViewX: 200, y: 200, in: fit)
        XCTAssertEqual(client.inputs,
                       [.drag(x: 0, y: 0, toX: 640, toY: 400, button: 1)],
                       "a drag across the picture itself did not reach the deck")
    }

    /// Right-click and double-click go through the same transform, so the same
    /// letterbox rule covers them.
    func testARightClickIsScaledIntoDisplaySpaceLikeAnyOtherClick() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme", client: client)
        await model.refreshStatus()
        let fit = try XCTUnwrap(model.fit(inViewWidth: 640, height: 400))

        await model.click(atViewX: 320, y: 200, in: fit, button: .right, count: 1)
        await model.click(atViewX: 320, y: 200, in: fit, button: .left, count: 2)
        XCTAssertEqual(client.inputs, [
            .click(x: 640, y: 400, button: 3, count: 1),
            .click(x: 640, y: 400, button: 1, count: 2),
        ], "the context menu and the double click did not arrive as themselves")
    }

    /// The wheel over the stage reaches the deck as a scroll at the pointer,
    /// and a wheel over the letterbox reaches it as nothing.
    func testTheWheelOverThePictureScrollsTheAgentsScreen() async throws {
        let client = FakeScreenClient(status: try Self.runningStatus())
        let model = AgentComputerModel(desk: "acme", displayName: "Acme", client: client)
        await model.refreshStatus()
        let fit = try XCTUnwrap(model.fit(inViewWidth: 400, height: 400))

        await model.scroll(atViewX: 200, y: 10, wheelY: -4, wheelX: 0, in: fit)
        XCTAssertEqual(client.inputs, [],
                       "the wheel over the letterbox scrolled the agent's screen")

        await model.scroll(atViewX: 200, y: 200, wheelY: -4, wheelX: 0, in: fit)
        XCTAssertEqual(client.inputs, [.scroll(x: 640, y: 400, dy: -4, dx: 0)],
                       "the wheel over the picture did not scroll it")
    }

    // MARK: - helpers

    /// A keyboard, as a person uses one. Every entry is a key that has to work.
    static let keyboard: [(String, KeyStroke)] = [
        ("Return", KeyStroke(keyCode: 36, characters: "\r", unmodified: "\r")),
        ("keypad Enter", KeyStroke(keyCode: 76, characters: "\u{3}",
                                   unmodified: "\u{3}")),
        ("Tab", KeyStroke(keyCode: 48, characters: "\t", unmodified: "\t")),
        ("shift+Tab", KeyStroke(keyCode: 48, characters: "\t", unmodified: "\t",
                                shift: true)),
        ("Escape", KeyStroke(keyCode: 53, characters: "\u{1B}", unmodified: "\u{1B}")),
        ("BackSpace", KeyStroke(keyCode: 51, characters: "\u{8}", unmodified: "\u{8}")),
        ("Delete", KeyStroke(keyCode: 117, characters: "\u{7F}", unmodified: "\u{7F}")),
        ("Left", KeyStroke(keyCode: 123)),
        ("Right", KeyStroke(keyCode: 124)),
        ("Down", KeyStroke(keyCode: 125)),
        ("Up", KeyStroke(keyCode: 126)),
        ("Home", KeyStroke(keyCode: 115)),
        ("End", KeyStroke(keyCode: 119)),
        ("Page Up", KeyStroke(keyCode: 116)),
        ("Page Down", KeyStroke(keyCode: 121)),
        ("F1", KeyStroke(keyCode: 122)),
        ("F2", KeyStroke(keyCode: 120)),
        ("F5", KeyStroke(keyCode: 96)),
        ("F11", KeyStroke(keyCode: 103)),
        ("F12", KeyStroke(keyCode: 111)),
        ("ctrl+c", KeyStroke(keyCode: 8, characters: "\u{3}", unmodified: "c",
                             control: true)),
        ("ctrl+d", KeyStroke(keyCode: 2, characters: "\u{4}", unmodified: "d",
                             control: true)),
        ("ctrl+l", KeyStroke(keyCode: 37, characters: "\u{C}", unmodified: "l",
                             control: true)),
        ("ctrl+shift+V", KeyStroke(keyCode: 9, characters: "\u{16}", unmodified: "V",
                                   control: true, shift: true)),
        ("ctrl+Left", KeyStroke(keyCode: 123, control: true)),
        ("ctrl+Return", KeyStroke(keyCode: 36, characters: "\r", unmodified: "\r",
                                  control: true)),
        ("alt+Tab", KeyStroke(keyCode: 48, characters: "\t", unmodified: "\t",
                              option: true)),
        ("ctrl+minus", KeyStroke(keyCode: 27, characters: "-", unmodified: "-",
                                 control: true)),
        ("ctrl+slash", KeyStroke(keyCode: 44, characters: "/", unmodified: "/",
                                 control: true)),
        ("ctrl+space", KeyStroke(keyCode: 49, characters: " ", unmodified: " ",
                                 control: true)),
        ("ctrl+bracketleft", KeyStroke(keyCode: 33, characters: "[", unmodified: "[",
                                       control: true)),
        ("ctrl+9", KeyStroke(keyCode: 25, characters: "9", unmodified: "9",
                             control: true)),
        ("the letter a", KeyStroke(keyCode: 0, characters: "a", unmodified: "a")),
        ("a capital Q", KeyStroke(keyCode: 12, characters: "Q", unmodified: "Q",
                                  shift: true)),
        ("a semicolon", KeyStroke(keyCode: 41, characters: ";", unmodified: ";")),
    ]

    /// Parsed, not stringified: the assertion is about the body, not about how
    /// this app happens to order keys in it.
    static func body(_ input: ScreenInput) throws -> NSDictionary {
        let data = try DeckCoding.encoder.encode(input)
        return try JSONSerialization.jsonObject(with: data) as? NSDictionary ?? [:]
    }

    static func client() throws -> FakeScreenClient {
        FakeScreenClient(status: try runningStatus())
    }

    static func runningStatus() throws -> AgentScreenStatus {
        try AgentComputerTests.runningStatus()
    }
}

/// A clock that remembers **how long** it was asked to wait, not only how many
/// times. The rate is what is under test, so the number is the evidence.
final class IntervalClock: ScreenPollClock, @unchecked Sendable {
    private let lock = NSLock()
    private var _waits: [TimeInterval] = []
    let budget: Int
    var onWait: (@Sendable (Int) -> Void)?

    init(budget: Int) { self.budget = budget }

    var waits: [TimeInterval] { lock.lock(); defer { lock.unlock() }; return _waits }

    func wait(_ seconds: TimeInterval) async throws {
        lock.lock(); _waits.append(seconds); let tick = _waits.count; lock.unlock()
        onWait?(tick)
        // Let anything the hook scheduled actually run before the next fetch.
        await Task.yield()
        await Task.yield()
        if tick >= budget { throw CancellationError() }
    }
}
