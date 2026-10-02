import XCTest
@testable import DeckKit

/// **"When the keyboard opens, the top half is black and the password field
/// is behind the keyboard."**
///
/// The owner's screenshot (iPhone, portrait, Atlas's screen, typing a Google
/// password): the agent's picture squeezed into a strip above the key bar,
/// the field he had tapped hidden under it, and "Take over" cut to "T…".
/// Every rule that fixes it is pure and pinned here: the area left to draw
/// in, the zoom that puts the field he tapped in the middle of it, and the
/// way back to the zoom he had when the keyboard goes.
@MainActor
final class PhoneKeyboardLayoutTests: XCTestCase {

    // iPhone 17 Pro, portrait, full-bleed: 402x874pt. The status bar plus the
    // toolbar cover the top ~110pt; the key bar plus the iOS keyboard ~490pt.
    let w = 402.0, h = 874.0
    let barBottom = 110.0
    let keysTop = 384.0   // the key bar's top edge, in the same coordinates

    // MARK: - the visible area

    func testWithTheKeyboardUpTheStageIsEverythingAboveTheKeyBarTopAligned() {
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h,
                                             coveredTop: barBottom, coveredBottom: h - keysTop)
        XCTAssertEqual(area.width, w)
        XCTAssertEqual(area.stageHeight, keysTop, accuracy: 0.001,
                       "the stage must end where the key bar starts, not run on under it")
        XCTAssertEqual(area.bandTop, barBottom, accuracy: 0.001)
        XCTAssertEqual(area.bandBottom, keysTop, accuracy: 0.001)
        XCTAssertEqual(area.bandMidY, (barBottom + keysTop) / 2, accuracy: 0.001)
        XCTAssertTrue(area.isCovered)
    }

    func testWithNoKeyboardTheStageIsTheWholeScreen() {
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h, coveredTop: barBottom, coveredBottom: 0)
        XCTAssertEqual(area.stageHeight, h)
        XCTAssertFalse(area.isCovered)
    }

    func testAKeyboardTallerThanTheScreenLeavesNothingNotANegativeStage() {
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h, coveredTop: barBottom, coveredBottom: h + 50)
        XCTAssertEqual(area.stageHeight, 0)
        XCTAssertLessThanOrEqual(area.bandTop, area.bandBottom)
        let negative = ScreenStageLayout.visible(viewWidth: w, viewHeight: h, coveredTop: -5, coveredBottom: -20)
        XCTAssertEqual(negative.stageHeight, h, "a negative cover is no cover")
        XCTAssertEqual(negative.bandTop, 0)
    }

    // MARK: - centring the field he tapped

    /// The Google password field on a 1280x800 desktop.
    let field = DisplayPoint(x: 640, y: 519)

    func testTheFieldHeTappedIsCentredAboveTheKeyboardAndReadable() {
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h,
                                             coveredTop: barBottom, coveredBottom: h - keysTop)
        let fit = ScreenFit(displayWidth: 1280, displayHeight: 800,
                            viewWidth: area.width, viewHeight: area.stageHeight)!
        let zoom = ScreenZoom.fitted.focusing(displayX: Double(field.x), y: Double(field.y), in: fit,
                                              viewWidth: area.width, viewHeight: area.stageHeight,
                                              centreY: area.bandMidY)
        let at = zoom.viewPoint(ofDisplayX: Double(field.x), y: Double(field.y), in: fit)
        XCTAssertEqual(at.x, w / 2, accuracy: 2, "the field is not centred left-right")
        XCTAssertEqual(at.y, area.bandMidY, accuracy: 2,
                       "the field is at y=\(at.y), not in the middle of the band he can see")
        XCTAssertGreaterThan(at.y, area.bandTop)
        XCTAssertLessThan(at.y, area.bandBottom, "the field is still under the keyboard")
        XCTAssertGreaterThanOrEqual(fit.scale * zoom.scale, ScreenZoom.readablePointsPerPixel - 0.001,
                                    "centred but too small to read")
        XCTAssertEqual(zoom, zoom.clamped(to: fit, viewWidth: area.width, viewHeight: area.stageHeight))
    }

    func testCentringKeepsAZoomHeHadThatWasAlreadyLarger() {
        let fit = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: keysTop)!
        let close = ScreenZoom(scale: 3.8, offsetX: 0, offsetY: 0)
        let zoom = close.focusing(displayX: 640, y: 519, in: fit, viewWidth: w, viewHeight: keysTop,
                                  centreY: (barBottom + keysTop) / 2)
        XCTAssertEqual(zoom.scale, 3.8, accuracy: 0.001)
    }

    func testAFieldInTheCornerIsBroughtAsNearTheMiddleAsThePictureAllows() {
        let fit = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: keysTop)!
        let zoom = ScreenZoom.fitted.focusing(displayX: 1270, y: 790, in: fit, viewWidth: w, viewHeight: keysTop,
                                              centreY: (barBottom + keysTop) / 2)
        XCTAssertEqual(zoom, zoom.clamped(to: fit, viewWidth: w, viewHeight: keysTop),
                       "panned past the edge of the picture: black where the desktop should be")
        let at = zoom.viewPoint(ofDisplayX: 1270, y: 790, in: fit)
        XCTAssertTrue((0...w).contains(at.x) && (0...keysTop).contains(at.y), "corner field at \(at), off the stage")
    }

    func testCentringWorksTheSameInLandscape() {
        // Landscape: 874x402, keyboard + key bar cover the bottom 250pt.
        let area = ScreenStageLayout.visible(viewWidth: 874, viewHeight: 402, coveredTop: 50, coveredBottom: 250)
        let fit = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: area.width, viewHeight: area.stageHeight)!
        let zoom = ScreenZoom.fitted.focusing(displayX: 640, y: 519, in: fit, viewWidth: area.width,
                                              viewHeight: area.stageHeight, centreY: area.bandMidY)
        let at = zoom.viewPoint(ofDisplayX: 640, y: 519, in: fit)
        XCTAssertEqual(at.x, 437, accuracy: 2)
        XCTAssertEqual(at.y, area.bandMidY, accuracy: 2)
    }

    func testInLandscapeAThinStripAboveTheKeyboardIsStillReadableAndStaysSoWhenPanned() {
        // Measured on the simulator: landscape, the key bar plus the keyboard
        // leave ~80pt. Fitted to that, the desktop is drawn at 0.1pt a pixel,
        // and 4x of that is still unreadable.
        let area = ScreenStageLayout.visible(viewWidth: 874, viewHeight: 402, coveredTop: 0, coveredBottom: 322)
        let fit = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: area.width, viewHeight: area.stageHeight)!
        let zoom = ScreenZoom.fitted.focusing(displayX: 640, y: 519, in: fit, viewWidth: area.width,
                                              viewHeight: area.stageHeight, centreY: area.bandMidY)
        XCTAssertGreaterThanOrEqual(fit.scale * zoom.scale, ScreenZoom.readablePointsPerPixel - 0.001,
                                    "the field is centred at \(fit.scale * zoom.scale)pt a pixel: unreadable")
        var panned = zoom
        panned.offsetX += 30
        let settled = panned.clamped(to: fit, viewWidth: area.width, viewHeight: area.stageHeight)
        XCTAssertEqual(settled.scale, zoom.scale, accuracy: 0.001, "a pan snapped the readable zoom back out")
    }

    // MARK: - keyboard up, keyboard down

    func testTheKeyboardOpeningCentresTheTapAndClosingRestoresTheZoomHeHad() {
        let full = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: h)!
        // A zoom he pinched to himself, not the one the screen opens at.
        let before = ScreenZoom(scale: 2.2, offsetX: -40, offsetY: 60).clamped(to: full, viewWidth: w, viewHeight: h)
        XCTAssertNotEqual(before, ScreenZoom.opening(fit: full, viewWidth: w, viewHeight: h), "calibration")
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h,
                                             coveredTop: barBottom, coveredBottom: h - keysTop)
        let small = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: area.stageHeight)!

        var keyboard = ScreenKeyboardZoom()
        XCTAssertFalse(keyboard.isUp)
        let up = keyboard.shown(current: before, currentWidth: w, currentHeight: h,
                                focus: field, fit: small, area: area)
        XCTAssertTrue(keyboard.isUp)
        let at = up.viewPoint(ofDisplayX: Double(field.x), y: Double(field.y), in: small)
        XCTAssertEqual(at.y, area.bandMidY, accuracy: 2, "the keyboard opened and the field stayed hidden")

        // The keyboard moves (QuickType bar, rotation of the key bar): centred
        // again, but the zoom to go back to is still the one from before.
        let moved = ScreenStageLayout.visible(viewWidth: w, viewHeight: h, coveredTop: barBottom, coveredBottom: h - 340)
        let smaller = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: moved.stageHeight)!
        _ = keyboard.shown(current: up, currentWidth: w, currentHeight: keysTop,
                           focus: field, fit: smaller, area: moved)

        let back = keyboard.hidden(fit: full, viewWidth: w, viewHeight: h)
        XCTAssertEqual(back, before, "the keyboard closed and his zoom was not given back")
        XCTAssertFalse(keyboard.isUp)
    }

    func testWithNoTapTheKeyboardShowsTheTopOfThePageReadable() {
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h,
                                             coveredTop: barBottom, coveredBottom: h - keysTop)
        let small = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: area.stageHeight)!
        var keyboard = ScreenKeyboardZoom()
        let up = keyboard.shown(current: .fitted, currentWidth: w, currentHeight: h,
                                focus: nil, fit: small, area: area)
        XCTAssertEqual(up, ScreenZoom.opening(fit: small, viewWidth: w, viewHeight: area.stageHeight))
    }

    func testClosingAfterARotationOpensReadableInsteadOfAZoomForTheOtherShape() {
        let full = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: h)!
        let before = ScreenZoom(scale: 3, offsetX: 100, offsetY: 50)
        let area = ScreenStageLayout.visible(viewWidth: w, viewHeight: h,
                                             coveredTop: barBottom, coveredBottom: h - keysTop)
        let small = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: area.stageHeight)!
        var keyboard = ScreenKeyboardZoom()
        _ = keyboard.shown(current: before, currentWidth: w, currentHeight: h,
                           focus: field, fit: small, area: area)
        // Turned to landscape while typing, then closed the keyboard.
        let wide = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: h, viewHeight: w)!
        let back = keyboard.hidden(fit: wide, viewWidth: h, viewHeight: w)
        XCTAssertEqual(back, ScreenZoom.opening(fit: wide, viewWidth: h, viewHeight: w))
    }

    func testClosingAKeyboardThatNeverOpenedChangesNothing() {
        var keyboard = ScreenKeyboardZoom()
        let full = ScreenFit(displayWidth: 1280, displayHeight: 800, viewWidth: w, viewHeight: h)!
        XCTAssertNil(keyboard.hiddenIfUp(fit: full, viewWidth: w, viewHeight: h))
    }

    // MARK: - the toolbar pill

    func testThePillHasAShortTitleThatFitsANarrowToolbar() {
        var control = ScreenControl(agentName: "Atlas")
        XCTAssertEqual(control.shortButtonTitle, "Take over")
        control.tookOver()
        XCTAssertEqual(control.shortButtonTitle, "Hand back",
                       "'Hand back to Atlas' is the one that cuts to 'H…' on a phone")
        XCTAssertLessThan(control.shortButtonTitle.count, control.buttonTitle.count)
        XCTAssertEqual(control.symbol, "hand.raised.slash")
    }
}
