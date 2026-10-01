import XCTest
@testable import DeckKit

/// A socket hands you bytes, not events. The parser has to survive a payload
/// split anywhere — including inside a field name — because that is the normal
/// case on a real connection, not the exotic one.
final class SSEParserTests: XCTestCase {

    func testAWholeEventInOneChunk() {
        var parser = SSEParser()
        let events = parser.consume("event: message\ndata: {\"a\":1}\n\n")

        XCTAssertEqual(events, [SSEEvent(name: "message", data: "{\"a\":1}", id: nil)])
    }

    func testAnEventSplitAcrossChunksArrivesWholeOnce() {
        var parser = SSEParser()

        XCTAssertEqual(parser.consume("event: mess"), [])
        XCTAssertEqual(parser.consume("age\ndata: {\"a\":"), [])
        let events = parser.consume("1}\n\n")

        XCTAssertEqual(events, [SSEEvent(name: "message", data: "{\"a\":1}", id: nil)])
    }

    func testMultiLineDataIsJoinedWithNewlines() {
        var parser = SSEParser()
        let events = parser.consume("data: first\ndata: second\n\n")

        XCTAssertEqual(events.first?.data, "first\nsecond")
    }

    func testTheLastEventIdIsCarried() {
        var parser = SSEParser()
        let events = parser.consume("id: 42\ndata: hi\n\n")

        XCTAssertEqual(events.first?.id, "42")
    }

    func testCommentHeartbeatsProduceNoEventButKeepTheStreamAlive() {
        var parser = SSEParser()

        XCTAssertEqual(parser.consume(": keep-alive\n\n"), [])
        XCTAssertEqual(parser.consume("data: real\n\n"), [SSEEvent(name: nil, data: "real", id: nil)])
    }

    func testTwoEventsInOneChunkBothCome() {
        var parser = SSEParser()
        let events = parser.consume("data: one\n\ndata: two\n\n")

        XCTAssertEqual(events.map(\.data), ["one", "two"])
    }

    func testCarriageReturnLineEndingsAreAccepted() {
        var parser = SSEParser()
        let events = parser.consume("event: message\r\ndata: hi\r\n\r\n")

        XCTAssertEqual(events, [SSEEvent(name: "message", data: "hi", id: nil)])
    }

    /// The deck sends `data:` frames with no event name; the kind is a `type`
    /// field inside the JSON. Typing off `event:` would decode nothing at all.
    func testAMessageFrameIsTypedByItsJSONNotByAnEventName() throws {
        let sse = SSEEvent(name: nil, data: """
        {"type":"message","ts":1.0,"thread_id":"direct:chief","read_only":false,
         "message":{"id":"m1","cursor":"0001-m1","thread_id":"direct:chief",
          "author":"chief","role":"agent","ts":1756000050.0,"text":"done",
          "attachments":[],"via":"office"}}
        """, id: nil)

        let event = try XCTUnwrap(DeckEvent(sse: sse))

        guard case .message(let message, let readOnly) = event else {
            return XCTFail("expected a message event, got \(event)")
        }
        XCTAssertEqual(message.id, "m1")
        XCTAssertEqual(message.cursor, "0001-m1")
        XCTAssertEqual(message.author, "chief")
        XCTAssertFalse(readOnly)
    }
}
