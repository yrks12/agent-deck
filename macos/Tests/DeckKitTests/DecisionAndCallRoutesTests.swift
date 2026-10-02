import XCTest
@testable import DeckKit

/// **K2, K4 and K6 as the app sends them.** The shapes are the contract's; no
/// socket is opened — the performer is injected, as in `HTTPDeckClientTests`.
final class DecisionAndCallRoutesTests: XCTestCase {

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    private func body(_ request: URLRequest?) throws -> [String: Any] {
        let data = try XCTUnwrap(request?.httpBody)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private static let sent = Data(#"""
    {"ok":true,"delivered":true,"message":{"id":"m1","thread_id":"direct:atlas","author":"owner",
     "role":"owner","ts":1.0,"text":"hi","channel":"text","kind":"text"}}
    """#.utf8)

    /// K2: the app says out loud that the owner typed it.
    func testATypedMessageIsSentAsHimOnTheTextChannel() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Self.sent
        _ = try await client(performer).send(threadID: "direct:atlas", text: "hi")

        let sent = try body(performer.requests.first)
        XCTAssertEqual(sent["text"] as? String, "hi")
        XCTAssertEqual(sent["as"] as? String, "owner")
        XCTAssertEqual(sent["channel"] as? String, "text")
        XCTAssertNil(sent["call_id"], "a typed line belongs to no call")
    }

    /// K4: one key, to the decision's own route.
    func testAnsweringADecisionPostsTheValueToItsRoute() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(#"{"ok":true}"#.utf8)
        try await client(performer).answerDecision(id: "dec_0123456789ab", value: "Hold copy")

        XCTAssertEqual(performer.paths, ["/v1/decisions/dec_0123456789ab"])
        XCTAssertEqual(performer.requests.first?.httpMethod, "POST")
        XCTAssertEqual(try body(performer.requests.first)["value"] as? String, "Hold copy")
    }

    func testAnAlreadyAnsweredDecisionIsAnError() async throws {
        let performer = StubPerformer()
        performer.status = 409
        performer.defaultBody = Data(#"{"reason":"already_answered"}"#.utf8)
        do {
            try await client(performer).answerDecision(id: "dec_1", value: "x")
            XCTFail("a 409 was swallowed")
        } catch {}
    }

    /// K6: start, a spoken line, end.
    func testACallStartsSpeaksAndEnds() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/calls"] = Data(#"""
        {"call_id":"call_0123456789ab","thread_id":"direct:atlas","voice":{"id":"","rate":1.0}}
        """#.utf8)
        performer.bodies["/v1/threads/direct:atlas/messages"] = Self.sent
        performer.bodies["/v1/calls/call_0123456789ab/end"] = Data(#"{"ok":true}"#.utf8)
        let deck = client(performer)

        let call = try await deck.startCall(agent: "atlas")
        XCTAssertEqual(call.callID, "call_0123456789ab")
        XCTAssertEqual(call.threadID, "direct:atlas")
        XCTAssertEqual(call.voice, DeskVoice(id: "", rate: 1.0))
        XCTAssertEqual(try body(performer.requests[0])["agent"] as? String, "atlas")

        _ = try await deck.sendVoice(threadID: call.threadID, text: "status of acme", callID: call.callID)
        let spoken = try body(performer.requests[1])
        XCTAssertEqual(spoken["channel"] as? String, "voice")
        XCTAssertEqual(spoken["call_id"] as? String, "call_0123456789ab")
        XCTAssertEqual(spoken["as"] as? String, "owner")

        try await deck.endCall(id: call.callID)
        XCTAssertEqual(performer.paths.last, "/v1/calls/call_0123456789ab/end")
        XCTAssertEqual(performer.requests.last?.httpMethod, "POST")
    }

    /// The fixture answers the same routes, so a demo build can tap a card.
    func testTheFixtureAnswersDecisionsAndCalls() async throws {
        let fixture = FixtureDeckClient()
        try await fixture.answerDecision(id: "dec_fixture01", value: "Go with A")
        let call = try await fixture.startCall(agent: "chief")
        XCTAssertEqual(call.threadID, "direct:chief")
        let line = try await fixture.sendVoice(threadID: "direct:chief", text: "hi", callID: call.callID)
        XCTAssertEqual(line.channel, .voice)
        try await fixture.endCall(id: call.callID)
    }
}
