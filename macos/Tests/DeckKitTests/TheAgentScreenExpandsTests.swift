import XCTest
@testable import DeckKit

/// **He was solving a captcha on a 640pt sheet.** The take-over came up small
/// with black bands, and there was no way to make the agent's screen bigger.
/// Now: the sheet opens at ~90% of his window in the display's own shape, and
/// Expand puts the screen in its own resizable window that can go full screen.
/// What must not break when it gets big is the click mapping.
final class TheAgentScreenExpandsTests: XCTestCase {

    func testClicksLandOnTheRightPixelWhenTheScreenIsDrawnHuge() throws {
        // A 1280x800 display drawn full screen on a 3024x1964 Retina-point window.
        let fit = try XCTUnwrap(ScreenFit(displayWidth: 1280, displayHeight: 800,
                                          viewWidth: 3024, viewHeight: 1964))
        XCTAssertGreaterThan(fit.scale, 2.3, "the picture did not scale up with the window")
        let centre = try XCTUnwrap(fit.displayPoint(atViewX: 1512, y: 982))
        XCTAssertEqual(centre.x, 640)
        XCTAssertEqual(centre.y, 400)
        let corner = try XCTUnwrap(fit.displayPoint(atViewX: fit.drawnX + fit.drawnWidth - 0.5,
                                                    y: fit.drawnY + fit.drawnHeight - 0.5))
        XCTAssertEqual(corner.x, 1279)
        XCTAssertEqual(corner.y, 799)
        XCTAssertNil(fit.displayPoint(atViewX: 1512, y: fit.drawnY - 1), "a click on the band is not sent")
    }

    func testTheSheetOpensAtNinetyPercentInTheDisplaysShape() {
        let wide = Takeover.Stage.sheetSize(window: CGSize(width: 1600, height: 1000))
        XCTAssertEqual(wide.width / wide.height, 1.6, accuracy: 0.01, "black bands above and below")
        XCTAssertLessThanOrEqual(wide.width, 1600 * 0.9 + 0.5)
        XCTAssertLessThanOrEqual(wide.height, 1000 * 0.9 + 0.5)
        XCTAssertGreaterThanOrEqual(max(wide.width / 1600, wide.height / 1000), 0.89, "not ~90% of the window")

        let tall = Takeover.Stage.sheetSize(window: CGSize(width: 1000, height: 1400))
        XCTAssertEqual(tall.width, 900, accuracy: 0.5)
        XCTAssertEqual(tall.width / tall.height, 1.6, accuracy: 0.01)

        let small = Takeover.Stage.sheetSize(window: CGSize(width: 600, height: 400))
        XCTAssertGreaterThanOrEqual(small.width, Takeover.Stage.minWidth)
    }

    func testExpandOpensTheWindowOnTheSameDesk() {
        let request = TakeoverRequest(desk: "acme-growth", displayName: "Acme-growth",
                                      workspace: "/w", focus: .screen)
        let expanded = ExpandedScreen(request)
        XCTAssertEqual(expanded.desk, "acme-growth")
        XCTAssertEqual(expanded.displayName, "Acme-growth")
        XCTAssertEqual(expanded.takeoverRequest.desk, "acme-growth")
        XCTAssertEqual(expanded.takeoverRequest.focus, .screen)
        let data = try? JSONEncoder().encode(expanded)
        XCTAssertEqual(data.flatMap { try? JSONDecoder().decode(ExpandedScreen.self, from: $0) }, expanded,
                       "the window's value survives being restored")
    }
}
