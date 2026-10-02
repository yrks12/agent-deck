import XCTest
@testable import DeckKit

/// The worst thing found by driving the app: **VoiceOver never read a single
/// message.**
///
/// `MessageBubble` combined its children and then set an
/// `.accessibilityLabel` of "who at when", which *replaces* the combined text
/// rather than adding to it. Measured on the running app — every bubble in the
/// transcript came back as:
///
///     Other, label: 'chief at 1 Sep 2026 at 18:43'
///
/// and nothing anywhere carried what chief had said. The entire content of
/// every conversation was invisible to assistive technology.
///
/// The sentence is built here, in DeckKit, so it is decidable without a
/// screen — which is the only reason this could be pinned at all.
final class SpokenMessageTests: XCTestCase {

    /// The view passes `Theme.spokenTimestamp(...)`; DeckKit stays free of the
    /// UI layer, and the sentence stays decidable without a screen.
    private let stamp = "1 Sep 2026 at 18:43"

    private func message(_ text: String, author: String = "chief",
                         role: MessageRole = .agent) -> Message {
        Message(
            id: "m1", cursor: "1-m1", threadID: "direct:chief",
            author: author, role: role,
            sentAt: Date(timeIntervalSince1970: 1_756_000_000),
            text: text, attachments: []
        )
    }

    /// THE test. Take the body out of the label and this fails.
    func testAMessageSaysWhatWasActuallySaid() {
        let spoken = message("wave 7 is merged, two branches left").spokenLabel(timestamp: stamp)
        XCTAssertTrue(
            spoken.contains("wave 7 is merged, two branches left"),
            "a message that does not carry its own text is unreadable to VoiceOver — got \(spoken)"
        )
    }

    /// Who said it still matters — the fix must not trade one for the other.
    func testAMessageStillSaysWhoAndWhen() {
        let spoken = message("wave 7 is merged").spokenLabel(timestamp: stamp)
        XCTAssertTrue(spoken.contains("chief"), "lost the author — got \(spoken)")
        XCTAssertTrue(spoken.contains(stamp), "lost the timestamp — got \(spoken)")
    }

    /// His own lines are announced as his, not as an agent's.
    func testHisOwnLineIsAnnouncedAsHis() {
        let spoken = message("ship it", author: "owner", role: .owner).spokenLabel(timestamp: stamp)
        XCTAssertTrue(spoken.hasPrefix("You"), "got \(spoken)")
        XCTAssertTrue(spoken.contains("ship it"))
    }

    /// Sweeping the class rather than the one case: a body that is a path or a
    /// link is still spoken, and an empty body does not produce a label that
    /// trails off into nothing.
    func testEveryKindOfBodyIsSpoken() {
        for body in ["plain prose",
                     "see https://example.com/thing",
                     "~/Projects/deck-app/README.md",
                     "mixed: ~/tmp/a.png and https://example.com"] {
            XCTAssertTrue(message(body).spokenLabel(timestamp: stamp).contains(body),
                          "body \(body.debugDescription) was not spoken")
        }
    }

    func testAnEmptyBodySaysSoRatherThanTrailingOff() {
        let spoken = message("").spokenLabel(timestamp: stamp)
        XCTAssertFalse(spoken.hasSuffix(", "), "got \(spoken)")
        XCTAssertTrue(spoken.contains("chief"))
    }
}
