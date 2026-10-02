import XCTest

/// §6.2 read off the **running app**, not off a view model.
///
/// The unit tests decide what a dispatch and a reply are; these check that the
/// decision reaches the screen — that the chip is a real, labelled control in
/// the accessibility tree, that the words of a relayed line are never
/// published as the desk answering the owner, and that tapping the chip lands
/// on the peer transcript.
///
/// Driven against the **fixture**. Its `direct:chief` carries the shape the
/// owner actually reads: he asks, the desk says what it is checking, two
/// dispatches go out to two reports, two sets of counts come back, and the
/// desk summarises.
final class RelayTests: XCTestCase {

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

    /// One predicate query rather than a walk of the whole tree: enumerating
    /// every element takes ~70s here and races the app redrawing underneath.
    private func labelled(_ app: XCUIApplication, containing text: String) -> Int {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", text))
            .count
    }

    // MARK: the three cases, on screen at once

    /// The desk answers him *and* two reports answer the desk, in one
    /// conversation. All three have to be distinguishable without leaving it.
    func testADispatchAndAReplyBothStandOnTheirOwnInTheDeskConversation() {
        let app = launchOnFixture()

        for chip in ["to Hemingway", "from Hemingway", "to Seeker", "from Seeker"] {
            XCTAssertTrue(
                app.buttons[chip].waitForExistence(timeout: 20),
                "no `\(chip)` control in the running app's accessibility tree"
            )
        }
        XCTAssertGreaterThan(
            labelled(app, containing: "Checking the launch note"), 0,
            "the desk's own answer to him must still be an ordinary bubble"
        )
    }

    /// The failure this whole change exists to prevent, measured on the
    /// screen: a worker's line published as though the desk had said it to
    /// him. Every element carrying the reply's words must carry the
    /// attribution too.
    func testAReplysWordsAreNeverPublishedWithoutSayingWhoSentThem() {
        let app = launchOnFixture()
        XCTAssertTrue(app.buttons["from Seeker"].waitForExistence(timeout: 20),
                      "the reply never drew at all")

        let words = "3 new sources"
        let carryingTheWords = labelled(app, containing: words)
        let alsoSayingWhoSentThem = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@ AND label CONTAINS 'Reply from Seeker'", words)
        ).count

        XCTAssertGreaterThan(carryingTheWords, 0,
                             "the reply's words are nowhere in the accessibility tree")
        XCTAssertEqual(
            carryingTheWords, alsoSayingWhoSentThem,
            "\(carryingTheWords - alsoSayingWhoSentThem) element(s) read Seeker's counts out "
            + "with no sign they came from Seeker"
        )
    }

    /// The name is a desk's name. An id on screen is the bug this replaced.
    func testNothingOnScreenShowsARawThreadId() {
        let app = launchOnFixture()
        XCTAssertTrue(app.buttons["to Hemingway"].waitForExistence(timeout: 20))

        XCTAssertEqual(labelled(app, containing: "peer:"), 0, "a raw thread id reached the screen")
        XCTAssertEqual(labelled(app, containing: "chief|"), 0, "half a thread id reached the screen")
    }

    /// One record, three views, one id. It is inline in the desk's thread, in
    /// the other desk's thread and in the pair's own — and it may be on screen
    /// once.
    func testARelayedLineIsDrawnOnceNotOncePerViewThatContainsIt() {
        let app = launchOnFixture()
        XCTAssertTrue(app.buttons["to Hemingway"].waitForExistence(timeout: 20))

        XCTAssertEqual(app.buttons["to Hemingway"].firstMatch.exists, true)
        XCTAssertEqual(
            app.buttons.matching(NSPredicate(format: "label == 'to Hemingway'")).count, 1,
            "the same dispatch was drawn more than once"
        )
    }

    // MARK: the tap

    /// The chip carries the peer thread's id, so it is the way into the full
    /// record. A peer thread is view-only by contract, which is how the
    /// harness can tell it actually arrived somewhere else.
    func testTappingTheAttributionOpensThePeerTranscript() {
        let app = launchOnFixture()
        let chip = app.buttons["to Hemingway"]
        XCTAssertTrue(chip.waitForExistence(timeout: 20), "nothing to tap")

        chip.click()

        XCTAssertTrue(
            app.staticTexts["This chat is view-only"].waitForExistence(timeout: 15),
            "the tap did not land on the read-only peer transcript"
        )
        XCTAssertFalse(
            app.textFields.matching(NSPredicate(format: "label BEGINSWITH 'Message '"))
                .firstMatch.exists,
            "a peer thread has no composer"
        )
    }

    /// The negative control for the tap: the desk's own conversation is
    /// writable, so if the assertion above were reading something that is
    /// always view-only it would pass here too.
    func testTheDeskConversationItselfStillTakesInput() {
        let app = launchOnFixture()

        XCTAssertTrue(
            app.textFields.matching(NSPredicate(format: "label BEGINSWITH 'Message '"))
                .firstMatch.waitForExistence(timeout: 20),
            "relaying another desk's traffic took his own composer away"
        )
        XCTAssertFalse(app.staticTexts["This chat is view-only"].exists)
    }
}
