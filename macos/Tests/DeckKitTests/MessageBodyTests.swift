import XCTest
@testable import DeckKit

/// The thread view renders links as links and file paths in monospace. That
/// decision is made here, once, on plain text — not inside a view body.
final class MessageBodyTests: XCTestCase {

    func testPlainProseIsOneTextSpan() {
        XCTAssertEqual(MessageBody.spans("just a sentence"), [.text("just a sentence")])
    }

    func testALinkBecomesItsOwnSpan() {
        let spans = MessageBody.spans("see https://example.com/report now")

        XCTAssertEqual(spans, [
            .text("see "),
            .link(URL(string: "https://example.com/report")!),
            .text(" now"),
        ])
    }

    func testAnAbsolutePathBecomesAMonospacedSpan() {
        let spans = MessageBody.spans("wrote /Users/sam/notes.md ok")

        XCTAssertEqual(spans, [.text("wrote "), .path("/Users/sam/notes.md"), .text(" ok")])
    }

    func testATildePathIsAPathToo() {
        XCTAssertEqual(MessageBody.spans("~/Projects/deck-app"), [.path("~/Projects/deck-app")])
    }

    func testTrailingPunctuationStaysOutOfTheLink() {
        let spans = MessageBody.spans("open https://example.com.")

        XCTAssertEqual(spans, [
            .text("open "),
            .link(URL(string: "https://example.com")!),
            .text("."),
        ])
    }

    func testALoneSlashIsProseNotAPath() {
        XCTAssertEqual(MessageBody.spans("either / or"), [.text("either / or")])
    }

    func testASeparatorLineIsDerivedFromTheRelayOnTheMessage() {
        XCTAssertEqual(Relay.messaged("Seeker").separatorText, "Messaged Seeker")
        XCTAssertEqual(Relay.received("Hemingway").separatorText, "Message from Hemingway")
    }

    func testTheUsersOwnMessagesAreIdentifiableForRightAlignment() {
        XCTAssertTrue(makeMessage("m1", cursor: "0001-m1", author: "owner", role: .owner).isFromUser)
        XCTAssertFalse(makeMessage("m2", cursor: "0002-m2", author: "chief", role: .agent).isFromUser)
    }
}
