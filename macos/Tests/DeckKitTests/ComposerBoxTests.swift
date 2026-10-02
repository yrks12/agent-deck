import XCTest
@testable import DeckKit

/// **"long message i cant scroll/edit easyliy."**
///
/// He pastes a multi-paragraph mandate — measured, ~8 lines — into a box that
/// showed six and then had nowhere to go. There was no way to see the top of
/// what he had written and no stated rule about what Return would do to it, so
/// editing it was guesswork with a send key underneath.
///
/// Two separate promises, both pinned here because both were broken:
/// the box **grows to a sensible size and then scrolls instead of growing**,
/// and **Return does exactly one thing, always the same thing**.
final class ComposerBoxTests: XCTestCase {

    /// His actual paste, in shape: paragraphs, blank lines between them.
    private let mandate = """
    You are the manager. Hold the state, dispatch, report.

    Route implementation to workers. Do not become one.
    Pick the models yourself. Do not ask permission.
    Decide, do it, and tell me afterwards.

    Escalate only when the goal itself is at risk, or when
    the call is mine and only mine.
    """

    // MARK: 1. it grows

    func testAnEmptyComposerIsOneLine() {
        XCTAssertEqual(ComposerBox.make(for: "").lines, 1)
        XCTAssertFalse(ComposerBox.make(for: "").isScrolling)
    }

    func testTheMandateHePastesIsShownWholeRatherThanClipped() {
        let box = ComposerBox.make(for: mandate)

        XCTAssertEqual(box.lines, 8, "the paste is 8 lines and all 8 have to be in the box")
        XCTAssertFalse(
            box.isScrolling,
            "at \(ComposerBox.maximumLines) lines it should not have had to scroll yet — "
            + "he should be able to see his whole mandate at once")
    }

    /// The old cap. Stated as a test so nobody quietly lowers it back under the
    /// length of the thing he actually sends.
    func testTheBoxIsTallerThanTheMessageHeActuallyWrites() {
        XCTAssertGreaterThanOrEqual(
            ComposerBox.maximumLines, 8,
            "six lines was the cap he hit; the mandate he pastes is longer than that")
    }

    // MARK: 2. then it scrolls, and stays editable

    func testPastTheCapItStopsGrowingAndScrollsInstead() {
        let long = (1...40).map { "line \($0)" }.joined(separator: "\n")

        let box = ComposerBox.make(for: long)

        XCTAssertEqual(box.lines, ComposerBox.maximumLines,
                       "it must stop growing, or it walks off the bottom of the window")
        XCTAssertTrue(box.isScrolling,
                      "and it must say it is scrolling, or 40 lines are simply gone")
    }

    func testTheLastLineIsNeverLostAtTheBoundary() {
        let exactly = (1...ComposerBox.maximumLines).map(String.init).joined(separator: "\n")
        let oneMore = exactly + "\nover"

        XCTAssertFalse(ComposerBox.make(for: exactly).isScrolling,
                       "a draft that exactly fills the box has not overflowed it")
        XCTAssertTrue(ComposerBox.make(for: oneMore).isScrolling)
    }

    /// A trailing newline is him about to type the next paragraph. The box has
    /// to have already made room for it, or the caret is off-screen the instant
    /// he presses Shift-Return.
    func testATrailingNewlineHasAlreadyMadeRoomForTheNextLine() {
        XCTAssertEqual(ComposerBox.make(for: "one\n").lines, 2)
    }

    // MARK: 3. Return does one thing, and the screen says which

    func testReturnSends() {
        XCTAssertEqual(ComposerBox.returnPress(shift: false, option: false), .send)
    }

    func testShiftReturnMakesANewLineRatherThanSending() {
        XCTAssertEqual(
            ComposerBox.returnPress(shift: true, option: false), .newline,
            "there is no unsend — a half-written mandate must not be able to leave "
            + "because he reached for a new paragraph")
    }

    /// Option-Return is the other muscle memory for this on a Mac. Sending on
    /// it would be the same accident under a different finger.
    func testOptionReturnAlsoMakesANewLine() {
        XCTAssertEqual(ComposerBox.returnPress(shift: false, option: true), .newline)
    }

    /// Undiscoverable is the same as absent. The rule has to be **on screen**,
    /// and it has to name both halves — the one that sends and the one that
    /// does not.
    func testTheRuleIsWrittenDownWhereHeCanReadIt() {
        let hint = ComposerBox.sendHint

        XCTAssertTrue(hint.contains("Return"), hint)
        XCTAssertTrue(hint.contains("Shift"), hint)
        XCTAssertTrue(
            hint.localizedCaseInsensitiveContains("new line")
            || hint.localizedCaseInsensitiveContains("newline"),
            "the hint has to say what the other key does, not only that Return sends: \(hint)")
    }

    // MARK: 4. one binding, in the source

    /// Return used to be bound **twice** in the composer — `.onSubmit` on the
    /// field and `.keyboardShortcut(.return)` on the send button — and there is
    /// no unsend if both ever fire. This reads the view the way
    /// `ScreenGateTests` reads the UI tests: the property worth protecting is
    /// that a *second* binding cannot be added back next month without a red.
    func testTheComposerBindsReturnInExactlyOnePlace() throws {
        let source = try composerSource()

        let bindings = ["onSubmit(", "keyboardShortcut(.return"].filter { source.contains($0) }

        XCTAssertEqual(
            bindings.count, 1,
            "Return is bound \(bindings.count) times in ThreadView.swift (\(bindings)). "
            + "Two bindings means one press can send the same line twice, and "
            + "none means Return does nothing at all")
    }

    /// And the sentence he reads has to be the one DeckKit decided, not a
    /// second copy of it typed into the view that can drift from the behaviour.
    func testTheViewShowsTheRuleFromTheOnePlaceThatDecidesIt() throws {
        XCTAssertTrue(
            try composerSource().contains("ComposerBox.sendHint"),
            "the composer must render ComposerBox.sendHint, or the caption and "
            + "the key handling are free to disagree")
    }

    private func composerSource() throws -> String {
        let view = URL(fileURLWithPath: #filePath)   // Tests/DeckKitTests/…
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI/ThreadView.swift")
        return try String(contentsOf: view, encoding: .utf8)
    }
}
