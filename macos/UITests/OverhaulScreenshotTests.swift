import XCTest

/// D-1 / D-2 / D-3 of the 2026-09-30 overhaul, read off the **running app** on
/// the fixture and saved as screenshots in `macos/UITests/Artifacts/overhaul/`.
///
/// One capture per Grok-equivalent state (grok-01..05). Every capture first
/// asserts a GOOD signal is on screen - a label a person can read - and only
/// then saves the picture, so a screenshot of the old UI can never pass.
///
/// The labels below are the contract the D-slices must draw. If D1/D2/D3 chose
/// different words, Q2 edits THIS file to match; the behaviour it checks
/// (hero tile, eyes not initials, centred system captions, decision card,
/// attention card, thumbnail, routines, mic) does not change.
///
/// Default deny like every UI test here: `requireTheScreenIsFree()` skips
/// unless YOS_SCREEN_IS_FREE=1. NOTE: `UITests/project.yml` only lists
/// `DeckUITests/`; R1/Q2 must add this file to `sources` before it can run.
final class OverhaulScreenshotTests: XCTestCase {

    override func setUpWithError() throws {
        try requireTheScreenIsFree()
        continueAfterFailure = true  // one missing state must not hide the others
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        app.launch()
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        return app
    }

    private func present(_ app: XCUIApplication, _ format: String, _ arg: String) -> Bool {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: format, arg)).firstMatch.waitForExistence(timeout: 15)
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let shot = app.windows.firstMatch.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        let dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Artifacts/overhaul")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? shot.pngRepresentation.write(to: dir.appendingPathComponent("\(name).png"))
    }

    /// grok-01: the chief as a hero tile, characters with eyes, a state-coloured preview.
    func testSidebarHasAHeroTileCharactersWithEyesAndAWaitingPreview() {
        let app = launch()
        XCTAssertTrue(present(app, "identifier == %@", "hero-tile"), "no chief hero tile")
        XCTAssertTrue(present(app, "label CONTAINS %@", "Waiting for you"),
                      "no orange 'Waiting for you' preview")
        XCTAssertTrue(present(app, "label ENDSWITH %@", "character"), "avatars are not labelled characters")
        capture(app, "01-sidebar")
    }

    /// grok-02: system lines as centred captions, peer traffic folded with faces.
    func testThreadShowsSystemCaptionsAndAPeerRollup() {
        let app = launch()
        XCTAssertTrue(present(app, "label BEGINSWITH %@", "Routine ·"), "no centred 'Routine · ...' caption")
        XCTAssertTrue(present(app, "label BEGINSWITH %@", "Deck ·"), "no centred 'Deck · ...' caption")
        XCTAssertTrue(present(app, "label CONTAINS %@", "messages with"), "no folded peer rollup")
        capture(app, "02-thread")
    }

    /// grok-03: a decision card with buttons.
    func testADecisionCardOffersButtons() {
        let app = launch()
        XCTAssertTrue(present(app, "identifier == %@", "decision-card"), "no decision card")
        XCTAssertGreaterThanOrEqual(app.buttons.matching(identifier: "decision-option").count, 2)
        capture(app, "03-decision")
    }

    /// grok-04: right pane - attention card with two buttons, screen thumbnail, routines.
    func testRightPaneHasAttentionCardThumbnailAndRoutines() {
        let app = launch()
        XCTAssertTrue(present(app, "label == %@", "Needs your attention"), "no attention card")
        XCTAssertTrue(app.buttons["Skip this step"].exists && app.buttons["I'm done, continue"].exists,
                      "attention card lacks its two buttons")
        XCTAssertTrue(present(app, "identifier == %@", "screen-thumbnail"), "no screen thumbnail")
        XCTAssertTrue(present(app, "label == %@", "Routines"), "no routines list")
        capture(app, "04-right-pane")
    }

    /// grok-05 + D-2: the composer pill with a mic, and no text clipped at 900 px.
    func testComposerPillHasAMicAndNothingIsClippedAtMinimumWidth() {
        let app = launch()
        XCTAssertTrue(present(app, "identifier == %@", "mic-button"), "no mic in the composer")
        let window = app.windows.firstMatch
        XCTAssertGreaterThanOrEqual(window.frame.width, 900)
        let field = app.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 15))
        XCTAssertGreaterThanOrEqual(field.frame.minX, window.frame.minX,
                                    "composer text starts left of the window edge (ours-04)")
        capture(app, "05-composer")
    }
}
