import XCTest
@testable import DeckKit

/// **Answering a desk's call, on the wire and in the voice session.** The
/// app answers, declines and edits the settings on their own routes, and
/// Answer opens the SAME live call as his own, with the desk's reason as the
/// first spoken line (`VoiceSession.answerCall`).
@MainActor
final class IncomingCallAnswerTests: XCTestCase {

    // MARK: the routes

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    func testAnswerDeclineAndSettingsGoToTheirRoutes() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/calls/incoming/ring_abc/answer"] = Data(#"""
        {"call_id":"call_1","thread_id":"direct:atlas","ring_id":"ring_abc","opening":"prod db is down"}
        """#.utf8)
        performer.bodies["/v1/calls/settings"] = Data(#"""
        {"who":"chief","when":"urgent","quiet_hours":"22:00-08:00","tz":"America/New_York",
         "max_per_day":3,"call_me_now":false,"ring_seconds":30}
        """#.utf8)
        let deck = client(performer)
        let started = try await deck.answerRing(id: "ring_abc")
        XCTAssertEqual(started.opening, "prod db is down")
        XCTAssertEqual(started.ringID, "ring_abc")
        try await deck.declineRing(id: "ring_abc")
        let conf = try await deck.callSettings()
        XCTAssertEqual(conf.when, .urgent)
        var edited = conf
        edited.callMeNow = true
        _ = try await deck.updateCallSettings(edited.changes(from: conf))
        XCTAssertEqual(performer.paths, ["/v1/calls/incoming/ring_abc/answer", "/v1/calls/incoming/ring_abc/decline",
                                         "/v1/calls/settings", "/v1/calls/settings"])
        XCTAssertEqual(performer.requests.map { $0.httpMethod }, ["POST", "POST", "GET", "PATCH"])
        let patch = try XCTUnwrap(JSONSerialization.jsonObject(with: performer.requests[3].httpBody!) as? [String: Any])
        XCTAssertEqual(patch.count, 1, "only what he changed")
        XCTAssertEqual(patch["call_me_now"] as? Bool, true)
    }

    // MARK: answering opens the same live call, reason first

    func testAnsweringOpensTheLiveCallWithTheReasonAsTheFirstLine() async throws {
        let log = VoiceLog()
        let calls = RingCalls()
        let transport = FakeRealtimeTransport()
        let audio = FakeCallAudio()
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { calls }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls)
        })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        await session.answerCall(ringID: "ring_abc")
        for _ in 0..<20 { await Task.yield() }

        XCTAssertEqual(calls.answered, ["ring_abc"])
        XCTAssertEqual(calls.started, [], "answering is not a second, outgoing call")
        XCTAssertEqual(session.call?.id, "call_9")
        XCTAssertNotNil(session.realtime)
        let said = transport.sent.compactMap { event -> String? in
            guard let item = event["item"] as? [String: Any],
                  let content = item["content"] as? [[String: Any]] else { return nil }
            return content.first?["text"] as? String
        }
        XCTAssertEqual(said.first, "[Atlas update] prod db is down", "the desk's reason is the call's first line")
        XCTAssertTrue(transport.sentTypes.contains("response.create"))
    }
}

/// A deck that can be called and can call: records which ring was answered.
private final class RingCalls: CallClient, IncomingCallClient, @unchecked Sendable {
    private(set) var answered: [String] = []
    private(set) var started: [String] = []
    private let offer = RealtimeOffer(wsURL: URL(string: "wss://api.openai.com/v1/realtime")!,
                                      clientSecret: "ek_test", expiresAt: Date().addingTimeInterval(60))

    func startCall(agent: String) async throws -> CallStart {
        started.append(agent)
        return CallStart(callID: "call_1", threadID: "direct:\(agent)")
    }

    func answerRing(id: String) async throws -> CallStart {
        answered.append(id)
        return CallStart(callID: "call_9", threadID: "direct:atlas", realtime: offer,
                         ringID: id, opening: "prod db is down")
    }

    func declineRing(id: String) async throws {}
    func callSettings() async throws -> CallOwnerSettings { .deckDefault }
    func updateCallSettings(_ changes: [String: CallSettingValue]) async throws -> CallOwnerSettings { .deckDefault }
    func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        Message(id: "m", cursor: "", threadID: threadID, author: DeckOwner.name, role: .owner,
                sentAt: Date(), text: text, channel: .voice)
    }
    func endCall(id: String) async throws {}
    func postTranscript(callID: String, lines: [CallLine]) async throws {}
}
