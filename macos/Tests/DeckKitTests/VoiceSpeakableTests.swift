import XCTest
@testable import DeckKit

/// **What the desk says out loud is the first two sentences of what it wrote,
/// and never a code block, a path or a link.** A reply read verbatim would
/// spell out `~/Projects/acme/server/app.py` letter by letter on a call.
final class VoiceSpeakableTests: XCTestCase {

    func testSpeaksOnlyTheFirstTwoSentences() {
        XCTAssertEqual(
            Speakable.text("Acme is green. Tests pass on main. Deploying next, then I'll report."),
            "Acme is green. Tests pass on main.")
    }

    func testAShortSayLineIsSpokenWhole() {
        XCTAssertEqual(Speakable.text("On it — checking acme now."), "On it — checking acme now.")
    }

    func testNeverSpeaksACodeBlock() {
        let reply = "Fixed it.\n```swift\nlet broken = true\nprint(broken)\n```\nAll green now."
        let spoken = Speakable.text(reply)
        XCTAssertEqual(spoken, "Fixed it. All green now.")
        XCTAssertFalse(spoken?.contains("broken") ?? true)
    }

    func testNeverSpeaksAURL() {
        let spoken = Speakable.text("The PR is up: https://github.com/acme/shop/pull/12 — review when free.")
        XCTAssertNotNil(spoken)
        XCTAssertFalse(spoken!.contains("http"), spoken!)
        XCTAssertFalse(spoken!.contains("github.com"), spoken!)
        XCTAssertTrue(spoken!.contains("review when free"), spoken!)
    }

    func testAMarkdownLinkKeepsItsWordsAndDropsTheAddress() {
        XCTAssertEqual(Speakable.text("See [the release notes](https://x.io/notes)."), "See the release notes.")
    }

    func testNeverSpeaksAPath() {
        let spoken = Speakable.text("I changed ~/Projects/acme/server/app.py and /tmp/out.log. It restarts cleanly.")
        XCTAssertNotNil(spoken)
        XCTAssertFalse(spoken!.contains("/"), spoken!)
        XCTAssertFalse(spoken!.contains("app.py"), spoken!)
        XCTAssertTrue(spoken!.contains("It restarts cleanly."), spoken!)
    }

    func testARelativePathAndABareFileNameAreDroppedToo() {
        let spoken = Speakable.text("Edited server/calls.py and README.md today.")
        XCTAssertNotNil(spoken)
        XCTAssertFalse(spoken!.contains("calls"), spoken!)
        XCTAssertFalse(spoken!.contains("README"), spoken!)
    }

    func testMarkdownDecorationIsNotReadAloud() {
        XCTAssertEqual(Speakable.text("## Status\n**Acme** is `green`.\n- deploy done"),
                       "Status. Acme is green.")
    }

    func testInlineCodeThatIsACommandIsDropped() {
        let spoken = Speakable.text("Run `gh pr merge 15 --squash` when ready.")
        XCTAssertNotNil(spoken)
        XCTAssertFalse(spoken!.contains("--squash"), spoken!)
        XCTAssertFalse(spoken!.contains("merge 15"), spoken!)
    }

    func testAReplyThatIsOnlyCodeSaysNothing() {
        XCTAssertNil(Speakable.text("```\nls -la\n```"))
        XCTAssertNil(Speakable.text("https://example.com/a/b"))
        XCTAssertNil(Speakable.text("   \n  "))
    }

    func testHebrewIsRecognisedSoItIsSpokenInAHebrewVoice() {
        XCTAssertEqual(Speakable.language(of: "הכול ירוק. הבדיקות עוברות."), "he")
        XCTAssertEqual(Speakable.language(of: "All green."), "en")
    }
}
