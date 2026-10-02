import XCTest

/// The two claims that can only be settled by looking at the running app.
///
/// Everything about *what* the strip says and *how big* the box is is decided
/// in DeckKit and pinned by `DeskStatusTests` and `ComposerBoxTests`, which run
/// under an ordinary `swift test`. What no unit test can settle is whether
/// SwiftUI actually put those decisions on the screen: whether the strip is
/// published to the accessibility tree at all, and whether a `TextField` with
/// `lineLimit(1...12)` really stops growing and scrolls rather than pushing
/// itself off the bottom of the window.
///
/// **These have not been run.** They are gated on `YOS_SCREEN_IS_FREE=1` like
/// every other test in this directory, and the owner was working on that Mac
/// when they were written — see `UITests/DeckUITests/ScreenGate.swift`. Run
/// them when the screen is free:
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 xcodebuild -project UITests/DeckUITests.xcodeproj \
///     -scheme DeckUITests -destination 'platform=macOS,arch=arm64' test
/// ```
///
/// Fixture-backed, deliberately: one of these types into a composer and must
/// never be able to put a line into one of his real sessions.
final class ThreadFeedbackTests: XCTestCase {

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

    private func composer(_ app: XCUIApplication) -> XCUIElement {
        app.textFields.matching(
            NSPredicate(format: "label BEGINSWITH 'Message '")
        ).firstMatch
    }

    /// The fixture's first desk is `WORKING`, so the conversation that opens on
    /// launch must say so — on the thread, with nothing clicked.
    func testTheConversationSaysWhatTheDeskIsDoingWithoutNavigatingAnywhere() {
        let app = launchOnFixture()

        let strip = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS[c] 'Working'"))
            .firstMatch

        XCTAssertTrue(
            strip.waitForExistence(timeout: 15),
            "nothing on the open conversation says the desk is working — this is "
            + "the screen he called inert")
    }

    /// The rule for the send key has to be legible on the window, not only in
    /// the accessibility tree.
    func testTheSendRuleIsPrintedUnderTheBox() {
        let app = launchOnFixture()

        let hint = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS[c] 'Shift-Return'")
        ).firstMatch

        XCTAssertTrue(hint.waitForExistence(timeout: 15),
                      "the composer does not say what Return does")
    }

    /// **The long-message claim.** Type a mandate longer than the box, and the
    /// box must (a) still hold every character, and (b) stop growing rather
    /// than walking off the bottom of the window.
    func testALongPasteStaysInTheBoxAndTheBoxStopsGrowing() {
        let app = launchOnFixture()
        let field = composer(app)
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no composer on a direct thread")

        let windowHeight = app.windows.firstMatch.frame.height
        field.click()
        let closed = field.frame.height

        // Twenty lines: well past the twelve the box draws, so it has to have
        // started scrolling instead.
        for line in 1...20 {
            app.typeText("line \(line)")
            app.typeKey(.return, modifierFlags: [.shift])
        }
        app.typeText("last")

        let grown = field.frame.height
        XCTAssertGreaterThan(grown, closed, "the box never grew at all")
        XCTAssertLessThan(
            grown, windowHeight / 2,
            "the composer grew to \(grown)pt in a \(windowHeight)pt window — it is "
            + "pushing the conversation off the screen instead of scrolling")

        // (a): nothing was eaten on the way in, and the first line is still there
        // to go back and edit.
        let value = (field.value as? String) ?? ""
        XCTAssertTrue(value.contains("line 1"), "the top of the paste is gone")
        XCTAssertTrue(value.contains("last"), "the end of the paste is gone")
    }

    /// Shift-Return must make a new line, not send. There is no unsend, so this
    /// is the expensive one to get wrong.
    func testShiftReturnDoesNotSendTheHalfWrittenLine() {
        let app = launchOnFixture()
        let field = composer(app)
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no composer on a direct thread")

        let sentence = "half written mandate probe"
        field.click()
        app.typeText(sentence)
        app.typeKey(.return, modifierFlags: [.shift])
        Thread.sleep(forTimeInterval: 2)

        XCTAssertTrue(
            ((field.value as? String) ?? "").contains(sentence),
            "Shift-Return emptied the composer, so it sent — a half-written "
            + "message left for the desk and there is no way to take it back")
    }
}
