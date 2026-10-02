import XCTest
@testable import DeckKit

/// **The stream the owner actually watches.**
///
/// Measured live, 2026-09-03: the desk answered him at 23:13:32 and again at
/// 23:15:26 and he saw neither reply until he clicked to another view and back.
/// The deck's own log said `GET /v1/stream HTTP/1.1 200 OK`, so the socket was
/// open and the bytes were arriving — they were being thrown away one layer
/// above the socket, and the thread he was looking at never moved. He concluded
/// the system was broken while it was working.
///
/// Everything under `SSEParserTests` and `ContractTests` already passed at the
/// time, because both of them start from a `String` or an `SSEEvent` that
/// somebody has already framed correctly. The break was in the framing itself,
/// which no test had ever run. So these drive the **real**
/// `URLSessionSSETransport` through a **real** `URLSession`, with a
/// `URLProtocol` standing in for the socket: nothing between the wire bytes and
/// the `DeckEvent` is a stub of the thing under test, and no packet leaves this
/// process.
final class LiveStreamTests: XCTestCase {

    /// A byte-for-byte transcript of what `server/api.py`'s `/v1/stream`
    /// generator writes: `data: <json>\n\n` per frame, no event name, the kind
    /// carried in `type`. The blank line is not decoration — it is the only
    /// thing on the wire that says "that frame is over".
    private static let wire = """
    data: {"type": "hello", "ts": 1788222702.09, "agents": [], "threads": []}

    data: {"type": "heartbeat", "ts": 1788222717.09}

    data: {"type": "message", "ts": 1788222777.3, "thread_id": "direct:chief", \
    "read_only": false, "message": {"id": "m1", "cursor": "001788222777300000-m1", \
    "thread_id": "direct:chief", "author": "chief", "role": "agent", \
    "ts": 1788222777.3, "text": "on it", "attachments": [], "via": "office"}}


    """

    private var request: URLRequest {
        URLRequest(url: URL(string: "http://deck.test/v1/stream")!)
    }

    private func transport(serving wire: String) -> URLSessionSSETransport {
        WireProtocol.wire = Data(wire.utf8)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [WireProtocol.self]
        return URLSessionSSETransport(session: URLSession(configuration: configuration))
    }

    private func chunks(from wire: String) async throws -> [String] {
        var collected: [String] = []
        for try await chunk in transport(serving: wire).lines(for: request) {
            collected.append(chunk)
        }
        return collected
    }

    /// THE detector for "replies do not appear".
    ///
    /// A reply the deck put on the stream has to reach the app as a
    /// `DeckEvent.message`, because that — and only that — is what moves an
    /// open thread without him navigating away and back.
    func testAReplyOnTheStreamArrivesAsAMessageEvent() async throws {
        var parser = SSEParser()
        var events: [DeckEvent] = []
        for chunk in try await chunks(from: Self.wire) {
            for frame in parser.consume(chunk) {
                if let event = DeckEvent(sse: frame) { events.append(event) }
            }
        }

        let replies = events.compactMap { event -> Message? in
            if case .message(let message, _) = event { return message }
            return nil
        }
        XCTAssertEqual(
            replies.map(\.text), ["on it"],
            "the deck put a reply on the stream and the transport delivered "
            + "\(events.count) event(s), none of them the reply — this is the "
            + "thread that stops updating while he watches it")
    }

    /// The same fault, stated as the smallest true thing: **what comes off the
    /// socket must equal what goes into the parser.**
    ///
    /// `AsyncBytes.lines` silently drops EMPTY lines, and an SSE frame is
    /// terminated by exactly one empty line. Feed a parser only the non-empty
    /// lines and it holds every frame open forever, waiting for a terminator
    /// that was eaten upstream — while the bytes keep flowing, so the silence
    /// watchdog stays quiet and the indicator keeps saying **Connected**.
    func testTheBlankLineThatEndsAFrameIsNotSwallowedOnTheWayToTheParser() async throws {
        let wire = "data: one\n\ndata: two\n\n"

        let delivered = try await chunks(from: wire).joined()

        XCTAssertEqual(
            delivered, wire,
            "the transport handed the parser \(String(reflecting: delivered)) "
            + "instead of the bytes that arrived — a frame terminator was lost")
    }

    /// A frame split across two reads is the normal case on a real socket, and
    /// the split lands wherever TCP decides. It must still arrive once, whole.
    func testAFrameSplitAcrossTwoReadsStillArrivesWholeAndOnce() async throws {
        WireProtocol.split = true
        defer { WireProtocol.split = false }

        var parser = SSEParser()
        var texts: [String] = []
        for chunk in try await chunks(from: Self.wire) {
            for frame in parser.consume(chunk) {
                if case .message(let message, _) = DeckEvent(sse: frame) { texts.append(message.text) }
            }
        }

        XCTAssertEqual(texts, ["on it"], "a split frame arrived \(texts.count) times")
    }

    /// A refused stream is a refusal, not an empty conversation. Without this
    /// a 401 reads exactly like a deck with nothing to say.
    func testARefusedStreamThrowsRatherThanEndingQuietly() async throws {
        WireProtocol.status = 401
        defer { WireProtocol.status = 200 }

        do {
            _ = try await chunks(from: "")
            XCTFail("a 401 on the stream ended the feed as if the deck had finished talking")
        } catch {
            // The shape is `DeckError`; which one `DeckError(status:body:)`
            // picks is pinned where refusals are, not here.
            XCTAssertTrue(error is DeckError, "expected a DeckError, got \(error)")
        }
    }
}

/// The socket, minus the socket. Serves `Self.wire` as a `text/event-stream`
/// response through the ordinary `URLSession` loading pipeline, so the code
/// under test is the code that runs against the deck.
final class WireProtocol: URLProtocol {
    /// What the "deck" is about to say.
    static var wire = Data()
    /// Deliver it in two parts, so a frame boundary lands mid-read.
    static var split = false
    static var status = 200

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: Self.status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "text/event-stream", "Cache-Control": "no-cache"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if Self.split, Self.wire.count > 4 {
            let cut = Self.wire.count / 2
            client?.urlProtocol(self, didLoad: Self.wire.prefix(cut))
            client?.urlProtocol(self, didLoad: Self.wire.suffix(from: cut))
        } else {
            client?.urlProtocol(self, didLoad: Self.wire)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
