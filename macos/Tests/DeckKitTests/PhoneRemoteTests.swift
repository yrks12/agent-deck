import XCTest
@testable import DeckKit

/// **"Make the agent's computer VERY easy to operate from the phone."**
///
/// Jump Desktop / Screens quality means the fiddly parts are right: small
/// targets reachable with a trackpad cursor, gestures that mean what every
/// remote-desktop app means by them, modifiers that stick, and a picture that
/// follows the cursor. Every rule is pure and pinned here; the iOS view only
/// draws and forwards touches.
@MainActor
final class PhoneRemoteTests: XCTestCase {

    // Landscape iPhone 16 Pro, full-bleed.
    let viewW = 874.0, viewH = 402.0
    var fit: ScreenFit { ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: viewW, viewHeight: viewH)! }
    var portraitFit: ScreenFit { ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: 402, viewHeight: 874)! }

    // MARK: - opening zoom and following the cursor

    func testTheScreenOpensZoomedToAReadableSizeAtTheTopLeft() {
        for (f, w, h) in [(fit, viewW, viewH), (portraitFit, 402.0, 874.0)] {
            let zoom = ScreenZoom.opening(fit: f, viewWidth: w, viewHeight: h)
            let pointsPerPixel = f.scale * zoom.scale
            XCTAssertGreaterThanOrEqual(pointsPerPixel, ScreenZoom.readablePointsPerPixel - 0.001,
                                        "opened at \(pointsPerPixel)pt per pixel: desktop text is unreadable")
            XCTAssertTrue(zoom.isZoomed)
            XCTAssertEqual(zoom, zoom.clamped(to: f, viewWidth: w, viewHeight: h), "opened outside its own bounds")
            // The top-left of the desktop (menus, the address bar) is in view.
            let topLeft = zoom.viewPoint(ofDisplayX: 0, y: 0, in: f)
            XCTAssertEqual(topLeft.x, 0, accuracy: 1, "the left edge of the desktop is not at the left of the view")
            XCTAssertTrue((0...h).contains(topLeft.y), "the top of the desktop is off the view at \(topLeft.y)")
        }
    }

    func testViewPointIsTheInverseOfDisplayPoint() {
        let zoom = ScreenZoom(scale: 2.2, offsetX: 130, offsetY: -40).clamped(to: fit, viewWidth: viewW, viewHeight: viewH)
        let view = zoom.viewPoint(ofDisplayX: 700, y: 300, in: fit)
        let back = zoom.displayPoint(atViewX: view.x, y: view.y, in: fit)
        XCTAssertNotNil(back)
        XCTAssertLessThanOrEqual(abs((back?.x ?? 0) - 700), 1)
        XCTAssertLessThanOrEqual(abs((back?.y ?? 0) - 300), 1)
    }

    func testAZoomedPictureFollowsTheCursorSoItNeverLeavesTheScreen() {
        let zoom = ScreenZoom(scale: 3, offsetX: 0, offsetY: 0).clamped(to: fit, viewWidth: viewW, viewHeight: viewH)
        // Display (1270, 790) is far off the view at 3x centred.
        let before = zoom.viewPoint(ofDisplayX: 1270, y: 790, in: fit)
        XCTAssertGreaterThan(before.x, viewW, "calibration: the cursor starts off-screen")
        let followed = zoom.following(displayX: 1270, y: 790, in: fit, viewWidth: viewW, viewHeight: viewH)
        let after = followed.viewPoint(ofDisplayX: 1270, y: 790, in: fit)
        XCTAssertTrue((0...viewW).contains(after.x) && (0...viewH).contains(after.y),
                      "cursor at \(after) after following, outside \(viewW)x\(viewH)")
        XCTAssertEqual(followed, followed.clamped(to: fit, viewWidth: viewW, viewHeight: viewH))
        // A cursor already comfortably in view does not move the picture.
        let centre = zoom.following(displayX: 640, y: 400, in: fit, viewWidth: viewW, viewHeight: viewH)
        XCTAssertEqual(centre, zoom)
    }

    // MARK: - trackpad cursor

    func testTheTrackpadCursorMovesRelativelyAtScreenScale() {
        var cursor = TrackpadCursor(displayWidth: 1280, displayHeight: 800)
        XCTAssertEqual(cursor.point, DisplayPoint(x: 640, y: 400), "starts in the middle")
        let zoom = ScreenZoom(scale: 2, offsetX: 0, offsetY: 0)
        cursor.move(byViewDX: 10, dy: -4, fit: fit, zoom: zoom)
        let perPoint = TrackpadCursor.gain / (fit.scale * 2)
        XCTAssertEqual(cursor.x, 640 + 10 * perPoint, accuracy: 0.001)
        XCTAssertEqual(cursor.y, 400 - 4 * perPoint, accuracy: 0.001)
    }

    func testTheCursorStopsAtTheEdgesOfTheDisplay() {
        var cursor = TrackpadCursor(displayWidth: 1280, displayHeight: 800)
        cursor.move(byViewDX: 100_000, dy: -100_000, fit: fit, zoom: .fitted)
        XCTAssertEqual(cursor.point, DisplayPoint(x: 1279, y: 0),
                       "the cursor must stay on the display the deck will accept")
    }

    // MARK: - gestures

    func testTouchModeGesturesClickWhereTheFingerIs() {
        let zoom = ScreenZoom.fitted
        let at = (x: viewW / 2, y: viewH / 2)
        func input(_ g: RemoteGesture) -> ScreenInput? {
            RemoteGestures.input(for: g, mode: .touch, atViewX: at.x, y: at.y, cursor: nil, fit: fit, zoom: zoom)
        }
        let p = zoom.displayPoint(atViewX: at.x, y: at.y, in: fit)!
        XCTAssertEqual(input(.tap), .click(x: p.x, y: p.y, button: 1, count: 1))
        XCTAssertEqual(input(.doubleTap), .click(x: p.x, y: p.y, button: 1, count: 2))
        XCTAssertEqual(input(.longPress), .click(x: p.x, y: p.y, button: 3, count: 1))
        XCTAssertEqual(input(.twoFingerTap), .click(x: p.x, y: p.y, button: 3, count: 1))
        XCTAssertNil(RemoteGestures.input(for: .tap, mode: .touch, atViewX: 1, y: 1, cursor: nil, fit: fit, zoom: zoom),
                     "a tap on the letterbox is not a click")
    }

    func testTrackpadModeGesturesClickAtTheCursorNotTheFinger() {
        var cursor = TrackpadCursor(displayWidth: 1280, displayHeight: 800)
        cursor.move(byViewDX: 37, dy: 11, fit: fit, zoom: .fitted)
        let c = cursor.point
        func input(_ g: RemoteGesture) -> ScreenInput? {
            RemoteGestures.input(for: g, mode: .trackpad, atViewX: 3, y: 3, cursor: cursor, fit: fit, zoom: .fitted)
        }
        XCTAssertEqual(input(.tap), .click(x: c.x, y: c.y, button: 1, count: 1))
        XCTAssertEqual(input(.doubleTap), .click(x: c.x, y: c.y, button: 1, count: 2))
        XCTAssertEqual(input(.longPress), .click(x: c.x, y: c.y, button: 3, count: 1))
        XCTAssertEqual(input(.twoFingerTap), .click(x: c.x, y: c.y, button: 3, count: 1))
    }

    func testPressAndDragSelectsAndPressWithoutMovingIsARightClick() {
        let a = DisplayPoint(x: 100, y: 200), b = DisplayPoint(x: 400, y: 210)
        XCTAssertEqual(RemoteGestures.pressEnded(from: a, to: b), .drag(x: 100, y: 200, toX: 400, toY: 210, button: 1))
        XCTAssertEqual(RemoteGestures.pressEnded(from: a, to: a), .click(x: 100, y: 200, button: 3, count: 1))
    }

    // MARK: - modifiers, keys, combos

    func testStickyModifiersLatchForOneKeyAndLockOnASecondTap() {
        var mods = StickyModifiers()
        mods.tap(.control)
        XCTAssertTrue(mods.isOn(.control))
        XCTAssertEqual(mods.chord(RemoteKeys.left), .key("ctrl+Left"))
        XCTAssertFalse(mods.isOn(.control), "a latched modifier is spent by the key it modified")

        mods.tap(.shift); mods.tap(.shift)  // locked
        XCTAssertTrue(mods.isLocked(.shift))
        XCTAssertEqual(mods.chord(RemoteKeys.right), .key("shift+Right"))
        XCTAssertEqual(mods.chord(RemoteKeys.right), .key("shift+Right"), "a locked modifier stays on")
        mods.tap(.shift)
        XCTAssertFalse(mods.isOn(.shift), "a third tap turns it off")
    }

    func testModifiersAreSpelledInOneOrderAndCommandMeansControlOnTheAgentsLinux() {
        var mods = StickyModifiers()
        mods.tap(.shift); mods.tap(.option); mods.tap(.command)
        XCTAssertEqual(mods.chord("t"), .key("ctrl+alt+shift+t"))
        var both = StickyModifiers()
        both.tap(.command); both.tap(.control)
        XCTAssertEqual(both.chord("c"), .key("ctrl+c"), "Cmd and Ctrl together are one ctrl, not ctrl+ctrl")
    }

    func testATypedCharacterWithAModifierIsAChordAndPlainTextIsText() {
        var mods = StickyModifiers()
        XCTAssertEqual(mods.typed("hello"), [.type("hello")])
        mods.tap(.command)
        XCTAssertEqual(mods.typed("c"), [.key("ctrl+c")], "⌘ then c is copy on the agent's machine")
        XCTAssertEqual(mods.typed("c"), [.type("c")], "and the latch is spent")
        mods.tap(.control)
        XCTAssertEqual(mods.typed("-"), [.key("ctrl+minus")], "punctuation goes by its X11 name")
    }

    func testEveryKeyAndComboThePhoneSendsIsAKeysymTheDeckAccepts() throws {
        let regex = try NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$")
        var sent: [String] = [RemoteKeys.escape, RemoteKeys.tab, RemoteKeys.enter, RemoteKeys.backspace,
                              RemoteKeys.left, RemoteKeys.right, RemoteKeys.up, RemoteKeys.down]
        for combo in RemoteCombo.allCases {
            guard case .key(let k) = combo.input else { return XCTFail("\(combo) is not a key") }
            sent.append(k)
            XCTAssertFalse(combo.title.isEmpty)
        }
        var mods = StickyModifiers()
        for m in StickyModifiers.Key.allCases { mods.tap(m) }
        if case .key(let k) = mods.chord(RemoteKeys.up) { sent.append(k) }
        for name in sent {
            XCTAssertEqual(regex.numberOfMatches(in: name, range: NSRange(name.startIndex..., in: name)), 1,
                           "\(name) would be a 400 bad_input")
        }
        XCTAssertEqual(RemoteCombo.copy.input, .key("ctrl+c"))
        XCTAssertEqual(RemoteCombo.paste.input, .key("ctrl+v"))
        XCTAssertEqual(RemoteCombo.selectAll.input, .key("ctrl+a"))
        XCTAssertEqual(RemoteCombo.back.input, .key("alt+Left"))
    }

    func testOpeningAURLFocusesTheAddressBarTypesItAndPressesReturn() {
        XCTAssertEqual(BrowserActions.open("  example.com/a b "),
                       [.key("ctrl+l"), .type("example.com/a b"), .key("Return")])
        XCTAssertEqual(BrowserActions.open("   "), [])
    }

    func testPasteFromThePhoneTypesTheClipboardInChunksTheDeckAccepts() {
        XCTAssertEqual(ScreenTyping.paste(nil), [])
        XCTAssertEqual(ScreenTyping.paste(""), [])
        XCTAssertEqual(ScreenTyping.paste("pa$$ `w`"), [.type("pa$$ `w`")])
        let long = String(repeating: "a", count: 4096 * 2 + 5)
        let chunks = ScreenTyping.paste(long)
        XCTAssertEqual(chunks.count, 3, "the deck refuses more than 4096 characters in one type")
        XCTAssertEqual(chunks.map { if case .type(let t) = $0 { return t.count } else { return -1 } }, [4096, 4096, 5])
    }

    func testLiveTypingSendsWhatWasAddedAndABackspaceForEachCharacterRemoved() {
        XCTAssertEqual(LiveTyping.inputs(from: "", to: "h"), [.type("h")])
        XCTAssertEqual(LiveTyping.inputs(from: "hel", to: "hello"), [.type("lo")])
        XCTAssertEqual(LiveTyping.inputs(from: "hello", to: "hel"), [.key("BackSpace"), .key("BackSpace")])
        XCTAssertEqual(LiveTyping.inputs(from: "teh", to: "the"),
                       [.key("BackSpace"), .key("BackSpace"), .type("he")], "an autocorrect is a rewrite")
        XCTAssertEqual(LiveTyping.inputs(from: "same", to: "same"), [])
    }

    // MARK: - take over / hand back

    func testTakingOverTellsTheAgentToStopAndHandingBackTellsItToLookAgain() {
        var control = ScreenControl(agentName: "Atlas")
        XCTAssertEqual(control.driver, .agent)
        XCTAssertEqual(control.buttonTitle, "Take over")
        let stop = control.takeOverMessage
        XCTAssertTrue(stop.localizedCaseInsensitiveContains("stop"), stop)
        control.tookOver()
        XCTAssertEqual(control.driver, .owner)
        XCTAssertEqual(control.buttonTitle, "Hand back to Atlas")
        let back = control.handBackMessage
        XCTAssertTrue(back.localizedCaseInsensitiveContains("back"), back)
        XCTAssertNotEqual(stop, back)
        control.handedBack()
        XCTAssertEqual(control.driver, .agent)
    }

    func testClosingTheScreenWhileDrivingHandsItBack() {
        var control = ScreenControl(agentName: "Atlas")
        XCTAssertNil(control.messageOnClose, "closing while the agent drives says nothing")
        control.tookOver()
        XCTAssertEqual(control.messageOnClose, control.handBackMessage,
                       "he closed the screen: the agent must not stay paused forever")
    }

    // MARK: - frame rate follows his hands

    func testFramesSpeedUpWhileHeIsTouchingAndSlowDownWhenIdle() throws {
        let clock = FakeNow()
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme",
                                             client: FakeScreenClient(status: try AgentComputerTests.runningStatus()),
                                             clock: BudgetClock(budget: 1))
        let viewing = ScreenViewing(model: model, now: { clock.value })
        viewing.appeared(sceneActive: true)
        let idle = model.pollInterval
        XCTAssertGreaterThanOrEqual(idle, 0.4, "idle should be gentle on the agent's machine")
        viewing.interacted()
        XCTAssertLessThanOrEqual(model.pollInterval, 0.2, "while he drives he needs to see his result now")
        clock.value += ScreenViewing.idleAfter - 0.5
        viewing.settle()
        XCTAssertLessThanOrEqual(model.pollInterval, 0.2, "still inside the idle window")
        clock.value += 1
        viewing.settle()
        XCTAssertEqual(model.pollInterval, idle, accuracy: 0.0001)
        viewing.disappeared()
        XCTAssertFalse(model.isPolling, "gone is gone, fast or slow")
    }
}

final class FakeNow: @unchecked Sendable {
    var value = Date(timeIntervalSince1970: 1_800_000_000)
}
