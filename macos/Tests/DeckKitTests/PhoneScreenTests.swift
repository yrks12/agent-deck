import XCTest
@testable import DeckKit

/// **"I can't see or control my agents' computers from the phone."**
///
/// The Mac's take-over already reaches the three screen routes; the phone
/// reuses all of it (`AgentComputerModel`, `ScreenFit`, `ScreenInput`,
/// `ScreenKeys`). What a phone adds is four things a mouse never needed, and
/// each is pinned here without a device:
///
/// 1. **Pinch zoom.** A 1280x800 desktop is 390pt wide on an iPhone, so he
///    zooms — and a tap on a zoomed picture must still land on the display
///    pixel under his finger, not on the one that would be there unzoomed.
/// 2. **Two-finger drag is the wheel.** The route takes whole notches, one
///    axis, `1...10`, `dy < 0` is down; a drag is a continuous translation.
/// 3. **A keyboard of his own.** Text goes down verbatim; Return and Backspace
///    are keysyms the deck's regex accepts, from `ScreenKeys`, never literals.
/// 4. **Frames only while he is looking**, at a phone rate (~3 fps), and the
///    poll stops the moment the view goes away or the app is backgrounded.
@MainActor
final class PhoneScreenTests: XCTestCase {

    /// iPhone 16 Pro portrait, full-bleed: 402x874. A 1280x800 display drawn
    /// to fit is 402x251.25, letterboxed 311.375pt above and below.
    let viewW = 402.0, viewH = 874.0
    var fit: ScreenFit { ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: viewW, viewHeight: viewH)! }

    // MARK: - 1. zoom

    func testUnzoomedTheMappingIsExactlyTheLetterboxFit() throws {
        let zoom = ScreenZoom.fitted
        for (vx, vy) in [(0.0, 311.375), (201.0, 437.0), (401.9, 562.0), (100.5, 400.0)] {
            XCTAssertEqual(zoom.displayPoint(atViewX: vx, y: vy, in: fit),
                           fit.displayPoint(atViewX: vx, y: vy),
                           "at 1x the zoom must not move a click (\(vx),\(vy))")
        }
        XCTAssertNil(zoom.displayPoint(atViewX: 201, y: 100, in: fit),
                     "a tap on the letterbox bar is not a point on his agent's screen")
    }

    func testTheCentreOfTheViewIsTheCentreOfTheDisplayAtEveryZoom() {
        for scale in [1.0, 1.5, 2.0, 3.0, 4.0] {
            let zoom = ScreenZoom(scale: scale, offsetX: 0, offsetY: 0)
            assertPixel(zoom.displayPoint(atViewX: viewW / 2, y: viewH / 2, in: fit),
                        640, 400, "centre moved at \(scale)x")
        }
    }

    func testAtTwoTimesATapHalfwayToTheEdgeIsAQuarterOfTheDisplay() {
        // Zoomed 2x about the centre, the view's right edge shows display x=960.
        // So a tap at x = 3/4 of the view width is display x = 800.
        let zoom = ScreenZoom(scale: 2, offsetX: 0, offsetY: 0)
        let point = zoom.displayPoint(atViewX: viewW * 0.75, y: viewH / 2, in: fit)
        assertPixel(point, 800, 400)
        // And at 2x the letterbox that was at y=300 now has picture in it.
        XCTAssertNotNil(zoom.displayPoint(atViewX: viewW / 2, y: 320, in: fit))
    }

    func testPanningShiftsTheMappingByThePanOverTheScale() {
        // Panned 100pt right at 2x: what is under the view's centre is the
        // display point that was 50pt left of centre in the fitted picture.
        let zoom = ScreenZoom(scale: 2, offsetX: 100, offsetY: 0)
        let point = zoom.displayPoint(atViewX: viewW / 2, y: viewH / 2, in: fit)
        let expectedX = Int(((viewW / 2 - 50) / fit.drawnWidth) * 1280)
        assertPixel(point, expectedX, 400)
    }

    /// Within one display pixel: `ScreenFit` truncates, and 402pt into 1280
    /// pixels is not exact in binary, so the centre can come out as 399.9999.
    /// One pixel is below anything a finger can aim at.
    private func assertPixel(_ point: DisplayPoint?, _ x: Int, _ y: Int, _ message: String = "",
                             file: StaticString = #filePath, line: UInt = #line) {
        guard let point else { return XCTFail("no point at all. \(message)", file: file, line: line) }
        XCTAssertLessThanOrEqual(abs(point.x - x), 1, "x \(point.x) not \(x). \(message)", file: file, line: line)
        XCTAssertLessThanOrEqual(abs(point.y - y), 1, "y \(point.y) not \(y). \(message)", file: file, line: line)
    }

    func testZoomIsClampedAndNeverPansThePictureOffTheView() {
        let wild = ScreenZoom(scale: 40, offsetX: 99_999, offsetY: -99_999)
            .clamped(to: fit, viewWidth: viewW, viewHeight: viewH)
        XCTAssertEqual(wild.scale, ScreenZoom.maxScale)
        // At 4x the picture is 1608x1005: 603pt spare either side horizontally,
        // 65.5pt vertically.
        XCTAssertEqual(wild.offsetX, (fit.drawnWidth * ScreenZoom.maxScale - viewW) / 2, accuracy: 0.001)
        XCTAssertEqual(wild.offsetY, -(fit.drawnHeight * ScreenZoom.maxScale - viewH) / 2, accuracy: 0.001)

        let under = ScreenZoom(scale: 0.2, offsetX: 50, offsetY: 50)
            .clamped(to: fit, viewWidth: viewW, viewHeight: viewH)
        XCTAssertEqual(under, .fitted, "below 1x there is nothing to pan and nothing to shrink to")
        XCTAssertFalse(under.isZoomed)
        XCTAssertTrue(ScreenZoom(scale: 2, offsetX: 0, offsetY: 0).isZoomed)
    }

    func testEveryEdgeOfAClampedZoomStillMapsOntoTheDisplay() {
        // Wherever he has pinched and panned to, every corner of the view that
        // shows picture maps inside 1280x800 — the deck refuses off-screen.
        let zoom = ScreenZoom(scale: 3, offsetX: 5000, offsetY: 5000)
            .clamped(to: fit, viewWidth: viewW, viewHeight: viewH)
        let corner = zoom.displayPoint(atViewX: 0.5, y: viewH / 2, in: fit)
        XCTAssertEqual(corner?.x, 0, "panned hard right, the left edge shows display x=0")
    }

    // MARK: - 2. two-finger drag is the wheel

    func testDraggingUpScrollsDownInWholeNotchesOnOneAxis() {
        var drag = ScreenScrollDrag()
        let at = DisplayPoint(x: 640, y: 400)
        XCTAssertNil(drag.input(at: at, translationX: 0, translationY: -5),
                     "a twitch is not a notch")
        let first = drag.input(at: at, translationX: 3, translationY: -ScreenScrollDrag.pointsPerNotch * 2.5)
        XCTAssertEqual(first, .scroll(x: 640, y: 400, dy: -2, dx: 0),
                       "fingers up is page down, and the route says dy < 0 is down")
        XCTAssertNil(drag.input(at: at, translationX: 3, translationY: -ScreenScrollDrag.pointsPerNotch * 2.9),
                     "nothing new: the notches already sent are not sent again")
        XCTAssertEqual(drag.input(at: at, translationX: 0, translationY: -ScreenScrollDrag.pointsPerNotch * 1.0),
                       .scroll(x: 640, y: 400, dy: 1, dx: 0),
                       "coming back up scrolls back up")
    }

    func testDraggingLeftScrollsRightAndAFlingIsCappedAtTenNotches() {
        var drag = ScreenScrollDrag()
        let at = DisplayPoint(x: 10, y: 20)
        let input = drag.input(at: at, translationX: -ScreenScrollDrag.pointsPerNotch * 40, translationY: 4)
        XCTAssertEqual(input, .scroll(x: 10, y: 20, dy: 0, dx: 10),
                       "a fling is capped at the route's 10 notches, fingers left is page right")
    }

    func testAScrollDragEncodesExactlyOneAxisOnTheWire() throws {
        var drag = ScreenScrollDrag()
        let input = try XCTUnwrap(drag.input(at: DisplayPoint(x: 1, y: 2), translationX: 0,
                                             translationY: ScreenScrollDrag.pointsPerNotch * 3))
        let raw = try JSONSerialization.jsonObject(with: DeckCoding.encoder.encode(input)) as? [String: Any]
        XCTAssertEqual(raw?["action"] as? String, "scroll")
        XCTAssertEqual(raw?["dy"] as? Int, 3)
        XCTAssertNil(raw?["dx"], "the route refuses both axes, so a zero one is omitted")
    }

    // MARK: - 3. the keyboard

    func testTypedTextGoesDownVerbatimAndReturnFollowsIt() throws {
        XCTAssertEqual(ScreenTyping.inputs(forSubmitted: " ls -la $HOME ", pressReturn: true),
                       [.type(" ls -la $HOME "), .key("Return")])
        XCTAssertEqual(ScreenTyping.inputs(forSubmitted: "hunter2", pressReturn: false), [.type("hunter2")])
        XCTAssertEqual(ScreenTyping.inputs(forSubmitted: "", pressReturn: true), [.key("Return")],
                       "Return on an empty field is still a Return on his agent's machine")
        XCTAssertEqual(ScreenTyping.inputs(forSubmitted: "", pressReturn: false), [])
    }

    func testTheKeysThePhoneSendsAreOnesTheDeckAccepts() throws {
        let regex = try NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$")
        for input in [ScreenTyping.returnKey, ScreenTyping.backspace, ScreenTyping.tab, ScreenTyping.escape] {
            guard case .key(let name) = input else { return XCTFail("\(input) is not a key") }
            XCTAssertEqual(regex.numberOfMatches(in: name, range: NSRange(name.startIndex..., in: name)), 1,
                           "\(name) would be a 400 bad_input")
        }
        XCTAssertEqual(ScreenTyping.returnKey, .key("Return"))
        XCTAssertEqual(ScreenTyping.backspace, .key("BackSpace"))
    }

    // MARK: - 4. frames only while he is looking

    func testThePhoneWatchesAtAboutThreeFramesASecond() throws {
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme",
                                             client: FakeScreenClient(status: try AgentComputerTests.runningStatus()),
                                             clock: BudgetClock(budget: 1))
        let viewing = ScreenViewing(model: model)
        viewing.appeared(sceneActive: true)
        XCTAssertGreaterThanOrEqual(model.pollInterval, 0.25)
        XCTAssertLessThanOrEqual(model.pollInterval, 0.5, "slower than 2 fps and his tap has no visible result")
        viewing.disappeared()
    }

    func testFramesPollWhileTheScreenIsShownAndStopWhenItDisappears() async throws {
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let clock = BudgetClock(budget: 50)
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme", client: client, clock: clock)
        let viewing = ScreenViewing(model: model)
        clock.onWait = { tick in
            if tick == 4 { Task { @MainActor in viewing.disappeared() } }
        }
        viewing.appeared(sceneActive: true)
        XCTAssertTrue(model.isPolling, "the screen is on his phone and no frame is being asked for")
        await model.pollLoopFinished()
        XCTAssertEqual(client.frameCalls, 4,
                       "the screen went away after four frames and the phone kept asking to \(client.frameCalls)")
        XCTAssertFalse(model.isPolling)
    }

    func testBackgroundingTheAppStopsThePollAndComingBackRestartsIt() async throws {
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let clock = BudgetClock(budget: 50)
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme", client: client, clock: clock)
        let viewing = ScreenViewing(model: model)
        clock.onWait = { tick in
            if tick == 2 { Task { @MainActor in viewing.scene(active: false) } }
        }
        viewing.appeared(sceneActive: true)
        await model.pollLoopFinished()
        XCTAssertEqual(client.frameCalls, 2)
        XCTAssertFalse(model.isPolling, "a locked phone is still grabbing frames")

        viewing.scene(active: true)
        XCTAssertTrue(model.isPolling, "back in the app, on the screen, and the picture is frozen")
        viewing.disappeared()
        XCTAssertFalse(model.isPolling)
    }

    func testARepeatedAppearDoesNotLeaveAWatcherBehind() async throws {
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme", client: client,
                                             clock: BudgetClock(budget: 200))
        let viewing = ScreenViewing(model: model)
        viewing.appeared(sceneActive: true)
        viewing.appeared(sceneActive: true)   // SwiftUI may say it twice
        XCTAssertTrue(model.isPolling, "calibration: shown and active, it must poll")
        viewing.disappeared()
        XCTAssertFalse(model.isPolling, "one disappear must stop what two appears started")
        viewing.disappeared()
        XCTAssertFalse(model.isPolling)
    }

    func testNothingPollsBeforeTheScreenIsShownOrWhileTheAppIsInTheBackground() throws {
        let model = ScreenViewing.phoneModel(desk: "acme", displayName: "Acme",
                                             client: FakeScreenClient(status: try AgentComputerTests.runningStatus()),
                                             clock: BudgetClock(budget: 1))
        let viewing = ScreenViewing(model: model)
        XCTAssertFalse(model.isPolling)
        viewing.appeared(sceneActive: false)
        XCTAssertFalse(model.isPolling, "opened while the phone was locked and it polled anyway")
        viewing.scene(active: true)
        XCTAssertTrue(model.isPolling, "calibration: unlocked on the screen, it must poll")
        viewing.disappeared()
    }
}
