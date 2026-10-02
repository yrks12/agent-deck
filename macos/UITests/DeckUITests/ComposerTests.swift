import XCTest

/// Driven against the **fixture**, deliberately: both of these press Send and
/// close threads, and neither may put a message into one of the owner's real
/// sessions. `DECK_FIXTURE=1` keeps every byte of this local.
final class ComposerTests: XCTestCase {

    override func setUpWithError() throws {
        // Default deny: this launches the real app and takes the screen.
        // See UITests/DeckUITests/ScreenGate.swift.
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


    /// A bubble is an accessibility *group* carrying one label, so the message
    /// is found by label, not by static text.
    ///
    /// One predicate query rather than enumerating every element: walking the
    /// whole tree took ~70s and raced the app redrawing underneath it, which
    /// made this read 0 at random.
    private func copiesOnScreen(_ app: XCUIApplication, of sentence: String) -> Int {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", sentence))
            .count
    }

    private func composer(_ app: XCUIApplication) -> XCUIElement {
        // The composer's placeholder IS its label: "Message Chief". Matching
        // on "not the search field" picks up the inspector's Title box.
        app.textFields.matching(
            NSPredicate(format: "label BEGINSWITH 'Message '")
        ).firstMatch
    }

    /// The composer binds Return twice — `.onSubmit` on the field and
    /// `.keyboardShortcut(.return)` on the send button. If both fire, one press
    /// says it twice, and there is no unsend.
    func testOneReturnSendsTheMessageExactlyOnce() {
        let app = launchOnFixture()
        let field = composer(app)
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no composer on a direct thread")

        let sentence = "harness probe one two three"
        field.click()
        app.typeText(sentence)
        app.typeKey(.return, modifierFlags: [])
        Thread.sleep(forTimeInterval: 3)

        let copies = copiesOnScreen(app, of: sentence)
        XCTAssertEqual(copies, 1, "one Return put \(copies) copies of the line in the transcript")
    }

    /// The control for the test above: if pressing the send button does not
    /// put the line on screen either, then Return is not the variable and the
    /// composer is broken outright.
    func testClickingSendPutsTheLineInTheTranscript() {
        let app = launchOnFixture()
        let field = composer(app)
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no composer on a direct thread")

        let sentence = "harness probe by button"
        field.click()
        app.typeText(sentence)
        app.buttons["Send message"].firstMatch.click()
        Thread.sleep(forTimeInterval: 3)

        let copies = copiesOnScreen(app, of: sentence)
        XCTAssertEqual(copies, 1, "pressing Send put \(copies) copies of the line on screen")
    }

    /// Closing a view-only peer thread must leave a way back to the desk. The
    /// sidebar keeps its selection, so if re-clicking the already-selected row
    /// is a no-op there is nothing left to press.
    func testClosingAViewOnlyThreadStillLeavesAWayBackToTheDesk() throws {
        let app = launchOnFixture()

        let peer = app.radioButtons.matching(
            NSPredicate(format: "label CONTAINS '⇄'")
        ).firstMatch
        guard peer.waitForExistence(timeout: 15) else {
            throw XCTSkip("this fixture desk has no peer thread to close")
        }
        peer.click()
        Thread.sleep(forTimeInterval: 2)

        let close = app.buttons.matching(
            NSPredicate(format: "label CONTAINS[c] 'close'")
        ).firstMatch
        guard close.waitForExistence(timeout: 10) else {
            throw XCTSkip("no close button on the view-only footer")
        }
        close.click()
        Thread.sleep(forTimeInterval: 2)

        // Now get back to a conversation by clicking the desk in the sidebar,
        // which is the only route the app offers.
        app.outlines.cells.element(boundBy: 1).click()
        Thread.sleep(forTimeInterval: 3)

        XCTAssertFalse(
            app.staticTexts["No conversation open"].exists,
            "closing a peer thread stranded the window with no way back to the desk"
        )
    }
}
