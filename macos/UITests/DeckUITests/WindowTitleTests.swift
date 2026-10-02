import XCTest

/// Flow 1, first half: **which deck am I looking at.**
///
/// The single most confusing failure this app can produce is showing the
/// fixture's invented agents while the owner believes he is looking at his own
/// sessions. The only thing on screen that tells them apart is the window
/// title, so that title is the first thing driven and asserted here.
///
/// Both tests launch the real, built `Agent Deck.app` through XCUITest and read
/// the title off the **rendered** window via the accessibility tree. Nothing
/// here inspects a view model.
final class WindowTitleTests: XCTestCase {

    override func setUpWithError() throws {
        // Default deny: this launches the real app and takes the screen.
        // See UITests/DeckUITests/ScreenGate.swift.
        try requireTheScreenIsFree()
        continueAfterFailure = false
    }

    /// The detector: launched the way the owner launches it, the title is the
    /// live one.
    func testTheAppOpensOnTheRealDeckAndSaysSoInTheTitle() {
        let app = XCUIApplication()
        app.launch()

        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 30), "no window ever appeared")
        // The title used to be correct only until something was selected: the
        // detail column's navigationTitle became the window title and the
        // disclosure vanished. So wait until a desk is actually open.
        _ = app.outlines.cells.element(boundBy: 1).waitForExistence(timeout: 45)
        XCTAssertTrue(
            window.title.hasPrefix("Shaliach"),
            "the window must lead with which deck it is on — title was \(window.title)"
        )
        XCTAssertFalse(
            window.title.contains("fixture"),
            "the app opened on the mock while the deck was up — title was \(window.title)"
        )
    }

    /// The negative control for that detector. If the title assertion above
    /// were reading something that is always "Agent Deck" regardless of the
    /// backend, this test would fail — the app is deliberately put on the
    /// fixture and the harness is required to *notice*.
    func testTheSameDetectorCatchesTheFixture() {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        app.launch()

        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 30), "no window ever appeared")
        XCTAssertTrue(
            window.title.hasPrefix("Shaliach (fixture)"),
            "the fixture is not disclosed in the title — title was \(window.title)"
        )
    }
}
