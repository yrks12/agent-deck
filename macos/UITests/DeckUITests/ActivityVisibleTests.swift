import XCTest

/// **The three claims about the activity surface that only the running app can
/// settle — and they have NOT been run.**
///
/// Everything about *what* a tool-call card says, *where* it lands in the
/// conversation and *what state* it is in is decided in DeckKit and pinned by
/// `WorkIsVisibleTests` and `LiveWhileWatchingTests`, both of which run under
/// an ordinary `swift test`. What no unit test can settle is whether SwiftUI
/// actually puts those decisions on a screen:
///
/// 1. that a card is **published to the accessibility tree at all** — the
///    transcript now draws a heterogeneous list through an exhaustive switch,
///    and a `ForEach` over an enum is a shape this app has not shipped before;
/// 2. that the **status pill is reachable and readable**, not clipped out of
///    the card by the title's `fixedSize` when the subject is long;
/// 3. that answering one **leaves its record on screen** rather than
///    collapsing the row, which is the whole point of keeping it.
///
/// **These have not been run.** The owner was working on that Mac while they
/// were written — his instruction, verbatim, was that the screen is his. They
/// are gated on `YOS_SCREEN_IS_FREE=1` like everything else in this directory;
/// see `UITests/DeckUITests/ScreenGate.swift`. Run them when the screen is
/// free:
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 xcodebuild -project UITests/DeckUITests.xcodeproj \
///     -scheme DeckUITests -destination 'platform=macOS,arch=arm64' test
/// ```
///
/// Fixture-backed, deliberately. One of these **taps an approval button**, and
/// on a real deck that writes a standing permission on his Mac. It must never
/// be able to reach one.
final class ActivityVisibleTests: XCTestCase {

    override func setUpWithError() throws {
        // Default deny: this launches the real app and takes the screen.
        try requireTheScreenIsFree()
        continueAfterFailure = false
    }

    private func launchOnFixture() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        app.launch()
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        XCTAssertTrue(
            app.outlines.cells.element(boundBy: 1).waitForExistence(timeout: 30),
            "the fixture roster never drew"
        )
        return app
    }

    /// The desk with the canned tool call on it.
    private func openTheDeskWithAnAsk(_ app: XCUIApplication) {
        app.outlines.cells.element(boundBy: 1).click()
    }

    /// **The good signal: a card he can see and a status he can read.**
    func testAToolCallCardIsOnScreenWithItsStatus() throws {
        let app = launchOnFixture()
        openTheDeskWithAnAsk(app)

        let pill = app.staticTexts["Status: Waiting on you"]
        XCTAssertTrue(
            pill.waitForExistence(timeout: 15),
            "no tool call is on the conversation. The store has the ask — "
            + "`WorkIsVisibleTests` proves that — so this is SwiftUI not drawing "
            + "the card, or drawing it somewhere he cannot see")
        XCTAssertTrue(pill.isHittable, "the pill is drawn but clipped out of the card")
    }

    /// The disclosure is the reference product's "Show the details", and it has
    /// to open with the keyboard: this is the control that shows him the exact
    /// command before he grants anything.
    func testTheDetailsOfAToolCallOpenFromTheKeyboard() throws {
        let app = launchOnFixture()
        openTheDeskWithAnAsk(app)

        let disclosure = app.disclosureTriangles.firstMatch
        XCTAssertTrue(disclosure.waitForExistence(timeout: 15),
                      "the card has no way to see what would actually run")
        disclosure.click()
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "value CONTAINS 'Bash'"))
                .firstMatch.waitForExistence(timeout: 5),
            "the details opened and did not name the tool")
    }

    /// **The record stays.** He taps, and what he granted is still readable —
    /// this is the failure he reported as "I can't see anything".
    func testAnsweringLeavesTheCardOnScreenSayingWhatItDid() throws {
        let app = launchOnFixture()
        openTheDeskWithAnAsk(app)

        let allowOnce = app.buttons.matching(
            NSPredicate(format: "label BEGINSWITH 'Allow once'")
        ).firstMatch
        XCTAssertTrue(allowOnce.waitForExistence(timeout: 15))
        allowOnce.click()

        XCTAssertTrue(
            app.staticTexts["Status: Allowed once"].waitForExistence(timeout: 10),
            "the card vanished on the tap and the conversation went back to "
            + "exactly what it was, with nothing saying what had just been "
            + "allowed on his machine")
        XCTAssertFalse(
            allowOnce.exists,
            "a settled card still offers to grant, so the same permission can "
            + "be written twice")
    }
}
