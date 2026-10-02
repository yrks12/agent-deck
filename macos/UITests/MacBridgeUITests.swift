import XCTest

/// The Mac-side taps of the Mac-bridge live detectors L-2 / L-3 / L-4
/// (docs/plans/2026-09-30-mac-bridge.md). `tests/live/test_mac_bridge_live.py`
/// runs these through its DECK_MAC_*_CMD hooks, e.g.
///
///     DECK_MAC_GRANT_CMD='YOS_SCREEN_IS_FREE=1 xcodebuild ... test -only-testing:DeckUITests/MacBridgeUITests/testAllowForOneHour'
///
/// Each test first asserts the GOOD signal is on screen (the card, the bar, the
/// menu item) and only then taps it, so a build without the bridge fails
/// instead of passing. They drive the REAL installed app against the real box
/// (no DECK_FIXTURE): the point is the round trip.
///
/// Default deny like every UI test here: `requireTheScreenIsFree()` skips
/// unless YOS_SCREEN_IS_FREE=1, so nothing takes the owner's screen by default.
/// Labels are the plan's words; if M2 drew different ones Q2 edits THIS file.
/// NOTE: `UITests/project.yml` only lists `DeckUITests/`; R1/Q2 must add this
/// file to `sources` before it can run (same as OverhaulScreenshotTests).
final class MacBridgeUITests: XCTestCase {
    /// The app under test: this bundle is `<app id>.uitests` (project.yml),
    /// so the id follows whatever DECK_BUNDLE_ID the build used.
    static let appID = (Bundle(for: MacBridgeUITests.self).bundleIdentifier ?? "")
        .replacingOccurrences(of: ".uitests", with: "")


    override func setUpWithError() throws {
        try requireTheScreenIsFree()
        continueAfterFailure = false
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        return app
    }

    private func element(_ app: XCUIApplication, _ label: String, timeout: TimeInterval = 90) -> XCUIElement {
        let match = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@ OR title CONTAINS %@", label, label)).firstMatch
        XCTAssertTrue(match.waitForExistence(timeout: timeout), "'\(label)' is not on screen")
        return match
    }

    /// L-3: the grant card has three buttons; Allow for 1 hour lets the job run.
    func testAllowForOneHour() {
        let app = launch()
        _ = element(app, "Deny")
        _ = element(app, "Always allow")
        element(app, "Allow for 1 hour").click()
    }

    /// L-3: Deny -> the desk is told no.
    func testDeny() {
        let app = launch()
        _ = element(app, "Allow for 1 hour")
        element(app, "Deny").click()
    }

    /// L-4: while a desk job runs the in-use bar reads "<desk> is using your Mac" with Stop.
    func testStopInTheInUseBar() {
        let app = launch()
        _ = element(app, "is using your Mac")
        element(app, "Stop").click()
    }

    /// L-2: menu bar -> Pause Mac access.
    func testPauseMacAccess() {
        _ = launch()
        let bar = XCUIApplication(bundleIdentifier: Self.appID).menuBars.statusItems.firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 15), "no menu-bar extra")
        bar.click()
        let pause = XCUIApplication().descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS 'Pause Mac access' OR title CONTAINS 'Pause Mac access'")).firstMatch
        XCTAssertTrue(pause.waitForExistence(timeout: 10), "no 'Pause Mac access' item")
        pause.click()
    }

    /// L-2: menu bar -> Resume Mac access.
    func testResumeMacAccess() {
        _ = launch()
        let bar = XCUIApplication(bundleIdentifier: Self.appID).menuBars.statusItems.firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 15), "no menu-bar extra")
        bar.click()
        let resume = XCUIApplication().descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS 'Resume Mac access' OR title CONTAINS 'Resume Mac access'")).firstMatch
        XCTAssertTrue(resume.waitForExistence(timeout: 10), "no 'Resume Mac access' item")
        resume.click()
    }
}
