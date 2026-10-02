import XCTest
@testable import DeckKit

/// **A call is as smart as chat.** Owner, 2026-10-01: "On the chat, Atlas
/// performs way better than in a call." Measured on the box: two thirds of
/// what he said on a call was answered by the voice model alone. Now the
/// voice is a thin layer: a work question goes to the desk, the voice says a
/// short ack and nothing else, and what it then speaks is the desk's reply,
/// under instructions that forbid adding to it. A decision card the desk
/// raises mid-call is read out, and his spoken pick answers the card.
///
/// Silent: the audio is `FakeCallAudio` and the socket is in memory.
@MainActor
final class CallBrainTests: XCTestCase {
    /// Calls plus K4 answers, so a spoken pick can resolve a card.
    final class DecidingCalls: CallClient, DecisionClient, @unchecked Sendable {
        let base: FakeCalls
        private let lock = NSLock()
        private var _answered: [(id: String, value: String)] = []
        var answered: [(id: String, value: String)] { lock.lock(); defer { lock.unlock() }; return _answered }

        init(base: FakeCalls) { self.base = base }

        func startCall(agent: String) async throws -> CallStart { try await base.startCall(agent: agent) }
        func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
            try await base.sendVoice(threadID: threadID, text: text, callID: callID)
        }
        func endCall(id: String) async throws { try await base.endCall(id: id) }
        func postTranscript(callID: String, lines: [CallLine]) async throws {
            try await base.postTranscript(callID: callID, lines: lines)
        }
        func answerDecision(id: String, value: String) async throws {
            lock.lock(); _answered.append((id, value)); lock.unlock()
        }
    }

    private var calls: FakeCalls!
    private var deciding: DecidingCalls!
    private var transport: FakeRealtimeTransport!
    private var audio: FakeCallAudio!
    private var session: VoiceSession!
    private var history: [Message] = []

    private let offer = RealtimeOffer(
        wsURL: URL(string: "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1")!,
        clientSecret: "ek_test", expiresAt: RealtimeCallTests.minted.addingTimeInterval(60),
        model: "gpt-realtime-2.1", voice: "coral", instructions: "You are Atlas", tools: [])

    override func setUp() async throws {
        let log = VoiceLog()
        calls = FakeCalls(log: log)
        calls.realtime = offer
        deciding = DecidingCalls(base: calls)
        transport = FakeRealtimeTransport()
        audio = FakeCallAudio()
        let transport = self.transport!, audio = self.audio!, deciding = self.deciding!
        session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                               calls: { deciding }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls,
                                now: { RealtimeCallTests.minted })
        })
        history = [Message(id: "h1", cursor: "h1", threadID: "direct:atlas", author: "atlas", role: .agent,
                           sentAt: Date(), text: "Old news.")]
        feed()
    }

    private func feed() {
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: true, messages: history)
    }

    private func deskSays(_ id: String, _ text: String) {
        history.append(Message(id: id, cursor: id, threadID: "direct:atlas", author: "atlas", role: .agent,
                               sentAt: Date(), text: text))
        feed()
    }

    private func deskAsks(_ card: Decision) {
        history.append(Message(id: "m-\(card.id)", cursor: "m-\(card.id)", threadID: "direct:atlas",
                               author: "atlas", role: .agent, sentAt: Date(),
                               text: card.prompt, kind: .decision, decision: card))
        feed()
    }

    private func until(_ what: String, timeout: TimeInterval = 2, _ condition: () -> Bool) async {
        let end = Date().addingTimeInterval(timeout)
        while !condition() && Date() < end { try? await Task.sleep(nanoseconds: 5_000_000) }
        XCTAssertTrue(condition(), "timed out waiting: \(what) — sent \(transport.sentTypes)")
    }

    private func dial() async throws -> RealtimeCallSession {
        await session.startCall()
        return try XCTUnwrap(session.realtime, "no live call: \(session.notice ?? "no notice")")
    }

    private var items: [[String: Any]] {
        transport.sent.filter { $0["type"] as? String == "conversation.item.create" }
            .compactMap { $0["item"] as? [String: Any] }
    }

    private var itemTexts: [String] {
        items.compactMap { (($0["content"] as? [[String: Any]])?.first?["text"]) as? String }
    }

    private var responses: [[String: Any]] {
        transport.sent.filter { $0["type"] as? String == "response.create" }
    }

    private func toolCall(_ rt: RealtimeCallSession, id: String, name: String, _ args: [String: String]) {
        let json = String(decoding: try! JSONSerialization.data(withJSONObject: args), as: UTF8.self)
        let event: [String: Any] = ["type": "response.function_call_arguments.done",
                                    "call_id": id, "name": name, "arguments": json]
        rt.handle(String(decoding: try! JSONSerialization.data(withJSONObject: event), as: UTF8.self))
    }

    // MARK: a work question is the desk's, and so is the answer

    func testAWorkQuestionIsRoutedToTheDeskAndTheSpokenAnswerIsTheDesks() async throws {
        let rt = try await dial()
        // The voice acks out loud and hands his words over in the same response.
        rt.handle(#"{"type":"response.created"}"#)
        rt.handle(#"{"type":"response.output_audio_transcript.delta","delta":"On it."}"#)
        toolCall(rt, id: "fc_1", name: "send_to_desk", ["text": "what's in my team memory about sign-ins?"])
        rt.handle(#"{"type":"response.done","response":{"output":[]}}"#)

        await until("routed to the desk") { !calls.sent.isEmpty }
        XCTAssertEqual(calls.sent.first?.text, "what's in my team memory about sign-ins?",
                       "his words, as he said them, go to the desk's own thread")
        XCTAssertEqual(calls.sent.first?.threadID, "direct:atlas")
        await until("tool output") { items.contains { $0["type"] as? String == "function_call_output" } }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(responses.isEmpty,
                      "the ack was already said: no second response to fill the wait with its own words")

        let answer = "Two lessons: sign in with the Chrome login card first, and Google needs the passkey card."
        deskSays("r1", answer)
        await until("the desk's answer is spoken") { !responses.isEmpty }
        XCTAssertEqual(itemTexts.last, "[Atlas update] \(answer)")
        let spoken = try XCTUnwrap(responses.last?["response"] as? [String: Any],
                                   "the answer is spoken under its own instructions")
        let rules = try XCTUnwrap(spoken["instructions"] as? String)
        XCTAssertTrue(rules.contains("only what it says"), rules)
        XCTAssertTrue(rules.contains("Add no facts"), rules)
    }

    func testWhenTheVoiceSaidNothingItAcksInAFewWordsOnly() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.created"}"#)
        toolCall(rt, id: "fc_2", name: "send_to_desk", ["text": "is the deploy green?"])
        rt.handle(#"{"type":"response.done","response":{"output":[]}}"#)
        await until("ack") { !responses.isEmpty }
        let rules = (responses.first?["response"] as? [String: Any])?["instructions"] as? String ?? ""
        XCTAssertTrue(rules.contains("On it"), rules)
        XCTAssertTrue(rules.contains("nothing else"), rules)
    }

    func testEveryProgressLineIsSpokenAsItArrives() async throws {
        let rt = try await dial()
        deskSays("p1", "On it — opening Stripe.")
        await until("first") { responses.count == 1 }
        // The second line lands while the first is still being spoken: it
        // waits for that response to end, and keeps its rules when it goes.
        deskSays("p2", "Stripe shows £99, live.")
        await until("second item") { itemTexts.count == 2 }
        rt.handle(#"{"type":"response.done","response":{"output":[]}}"#)
        XCTAssertEqual(itemTexts, ["[Atlas update] On it — opening Stripe.", "[Atlas update] Stripe shows £99, live."])
        await until("both spoken") { responses.count == 2 }
        for response in responses {
            let rules = (response["response"] as? [String: Any])?["instructions"] as? String ?? ""
            XCTAssertTrue(rules.contains("only what it says"), "each line is the desk's, spoken as it arrives: \(rules)")
        }
    }

    // MARK: a decision card mid-call

    private let card = Decision(id: "dec_1", prompt: "Ship the price change today?",
                                options: [DecisionOption(label: "Ship it", value: "ship"),
                                          DecisionOption(label: "Hold", value: "hold")])

    func testADecisionCardIsSpokenAsChoices() async throws {
        _ = try await dial()
        deskAsks(card)
        await until("card spoken") { !responses.isEmpty }
        let said = try XCTUnwrap(itemTexts.last)
        XCTAssertTrue(said.hasPrefix("[Atlas asks you to pick]"), said)
        XCTAssertTrue(said.contains("dec_1") && said.contains("Ship the price change today?"), said)
        XCTAssertTrue(said.contains("Ship it") && said.contains("Hold"), said)
    }

    func testHisSpokenPickResolvesTheCard() async throws {
        let rt = try await dial()
        deskAsks(card)
        await until("card spoken") { !responses.isEmpty }
        rt.handle(#"{"type":"response.done","response":{"output":[]}}"#)
        toolCall(rt, id: "fc_3", name: "answer_card", ["card_id": "dec_1", "choice": "ship it"])
        await until("answered") { !deciding.answered.isEmpty }
        XCTAssertEqual(deciding.answered.first?.id, "dec_1")
        XCTAssertEqual(deciding.answered.first?.value, "ship", "a picked label answers with that option's value")
        await until("output") { items.contains { $0["call_id"] as? String == "fc_3" } }
        let out = items.first { $0["call_id"] as? String == "fc_3" }?["output"] as? String ?? ""
        XCTAssertTrue(out.contains(#""ok":true"#) && out.contains("Ship it"), out)
    }

    func testHisOwnWordsAnswerACardThatAllowsThem() async throws {
        let rt = try await dial()
        deskAsks(card)
        toolCall(rt, id: "fc_4", name: "answer_card", ["card_id": "dec_1", "choice": "only after the demo"])
        await until("answered") { !deciding.answered.isEmpty }
        XCTAssertEqual(deciding.answered.first?.value, "only after the demo")
    }

    func testACardTheVoiceNeverHeardIsNotAnswered() async throws {
        let rt = try await dial()
        toolCall(rt, id: "fc_5", name: "answer_card", ["card_id": "dec_made_up", "choice": "yes"])
        await until("output") { items.contains { $0["call_id"] as? String == "fc_5" } }
        XCTAssertTrue(deciding.answered.isEmpty)
        let out = items.first { $0["call_id"] as? String == "fc_5" }?["output"] as? String ?? ""
        XCTAssertTrue(out.contains(#""ok":false"#) && out.contains("no open card"), out)
    }

    func testAnAnsweredCardIsNotReadOutButTheNextOpenOneIs() async throws {
        _ = try await dial()
        var done = card
        done.state = .answered
        done.answer = "ship"
        deskAsks(done)
        deskAsks(Decision(id: "dec_2", prompt: "Post it tonight?",
                          options: [DecisionOption(label: "Yes", value: "yes"), DecisionOption(label: "No", value: "no")]))
        await until("the open card") { !itemTexts.isEmpty }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(itemTexts.count, 1, "\(itemTexts)")
        XCTAssertTrue(itemTexts.first?.contains("dec_2") == true, "\(itemTexts)")
    }
}
