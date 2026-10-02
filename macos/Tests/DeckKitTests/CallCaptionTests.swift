import XCTest
@testable import DeckKit

/// "ount to sign into.They…" — his window, mid-call: a caption that started
/// mid-word and ran two replies together.
final class CallCaptionTests: XCTestCase {
    func testOnlyFinishedSentencesAreShown() {
        XCTAssertEqual(CallCaption.shown("I checked it. The deploy is"), "I checked it.")
        XCTAssertEqual(CallCaption.shown("The deploy is"), "")
    }

    func testSentencesRunTogetherGetTheirSpaceBack() {
        XCTAssertEqual(CallCaption.shown("Sign into it.They said yes!Good? "), "Sign into it. They said yes! Good?")
    }

    func testDecimalsAndVersionsAreNotSplit() {
        XCTAssertEqual(CallCaption.shown("It is at 2.5 now. Fine."), "It is at 2.5 now. Fine.")
    }

    func testTheOldestSentencesGoFirstNeverAWordCut() {
        let text = (1...12).map { "Sentence number \($0) is here." }.joined(separator: " ")
        let shown = CallCaption.shown(text)
        XCTAssertLessThanOrEqual(shown.count, CallCaption.limit)
        XCTAssertTrue(shown.hasPrefix("Sentence number"), shown)
        XCTAssertTrue(shown.hasSuffix("Sentence number 12 is here."), shown)
    }

    func testOneHugeSentenceIsCutAtAWordWithAnEllipsis() {
        let text = String(repeating: "and then another thing ", count: 20) + "happened."
        let shown = CallCaption.shown(text)
        XCTAssertLessThanOrEqual(shown.count, CallCaption.limit)
        XCTAssertTrue(shown.hasPrefix("…"), shown)
        let firstWord = shown.dropFirst().split(separator: " ").first.map(String.init) ?? ""
        XCTAssertTrue(["and", "then", "another", "thing"].contains(firstWord), "a whole word: \(shown)")
        XCTAssertTrue(shown.hasSuffix("happened."))
    }
}
