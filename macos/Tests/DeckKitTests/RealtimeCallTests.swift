import XCTest
@testable import DeckKit

// MARK: - Fakes

/// The Realtime socket, in memory: what the app sent, and a pipe for the
/// server's events.
final class FakeRealtimeTransport: RealtimeTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var _sent: [String] = []
    private(set) var connectedURL: URL?
    private(set) var secret: String?
    private(set) var closed = false
    var failConnect = false
    private let stream: AsyncThrowingStream<String, Error>
    private let pipe: AsyncThrowingStream<String, Error>.Continuation
    private var iterator: AsyncThrowingStream<String, Error>.Iterator

    init() {
        let (stream, pipe) = AsyncThrowingStream<String, Error>.makeStream()
        self.stream = stream
        self.pipe = pipe
        self.iterator = stream.makeAsyncIterator()
    }

    func connect(url: URL, secret: String) async throws {
        if failConnect { throw DeckError.transport("refused") }
        connectedURL = url
        self.secret = secret
    }

    func send(_ text: String) async throws {
        lock.lock(); _sent.append(text); lock.unlock()
    }

    func receive() async throws -> String {
        guard let next = try await iterator.next() else { throw DeckError.transport("closed") }
        return next
    }

    func close() {
        closed = true
        pipe.finish()
    }

    /// The server says something.
    func server(_ event: [String: Any]) {
        let data = try! JSONSerialization.data(withJSONObject: event)
        pipe.yield(String(decoding: data, as: UTF8.self))
    }

    /// The server hangs up on us.
    func drop() { pipe.finish(throwing: DeckError.transport("socket closed by peer")) }

    var sent: [[String: Any]] {
        lock.lock(); defer { lock.unlock() }
        return _sent.compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
    }

    var sentTypes: [String] { sent.compactMap { $0["type"] as? String } }
}

@MainActor
final class FakeCallAudio: CallAudio {
    var onMicChunk: ((Data) -> Void)?
    var onMicLevel: ((Float) -> Void)?
    var onOutputLevel: ((Float) -> Void)?
    var failStart = false
    private(set) var started = false
    private(set) var stopped = false
    private(set) var played: [Data] = []
    private(set) var flushes = 0
    private var queued = 0

    var isPlaying: Bool { queued > 0 }

    func start() throws {
        if failStart { throw DeckError.transport("no mic") }
        started = true
    }

    func play(_ pcm16: Data) {
        played.append(pcm16)
        queued += 1
        onOutputLevel?(0.6)
    }

    func flushPlayback() {
        flushes += 1
        queued = 0
        onOutputLevel?(0)
    }

    func stop() { stopped = true; queued = 0 }

    /// The speaker finishes everything queued.
    func drain() { queued = 0; onOutputLevel?(0) }

    /// He talks into the mic.
    func speak(bytes: Int) { onMicChunk?(Data(repeating: 1, count: bytes)) }

    /// The mic hears something this loud (dBFS) for this long, in 20 ms chunks.
    func hear(db: Float, ms: Int) {
        let amplitude = powf(10, db / 20) * Float(2.0.squareRoot())
        for _ in 0..<(ms / 20) {
            onMicChunk?(PCM16.encode((0..<480).map { amplitude * sinf(Float($0) * 2 * .pi * 220 / 24_000) }))
        }
    }
}

// MARK: - Tests

@MainActor
final class RealtimeCallTests: XCTestCase {
    private var log: VoiceLog!
    private var calls: FakeCalls!
    private var input: FakeSpeechInput!
    private var output: FakeSpeechOutput!
    private var transport: FakeRealtimeTransport!
    private var audio: FakeCallAudio!
    private var session: VoiceSession!
    private var history: [Message] = []

    /// When the fixture's pass was minted: a fixed instant, not `Date()`.
    /// XCTest builds every test case when the run starts, so a stored
    /// `Date() + 60` had expired by the time a long suite reached this class
    /// (measured: 65 s in with the Settings captures) and no call dialled.
    static let minted = Date(timeIntervalSince1970: 1_790_000_000)

    private let offer = RealtimeOffer(
        wsURL: URL(string: "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1")!,
        clientSecret: "ek_test", expiresAt: RealtimeCallTests.minted.addingTimeInterval(60), model: "gpt-realtime-2.1",
        voice: "coral", instructions: "You are Atlas", tools: [])

    override func setUp() async throws {
        log = VoiceLog()
        calls = FakeCalls(log: log)
        calls.realtime = offer
        input = FakeSpeechInput(log: log)
        output = FakeSpeechOutput(log: log)
        transport = FakeRealtimeTransport()
        audio = FakeCallAudio()
        let transport = self.transport!, audio = self.audio!
        session = VoiceSession(input: input, output: output, calls: { [calls] in calls }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls,
                                now: { RealtimeCallTests.minted })
        })
        history = [Message(id: "h1", cursor: "h1", threadID: "direct:atlas", author: "atlas", role: .agent,
                           sentAt: Date(), text: "Old news.")]
        feed()
    }

    private func feed(working: Bool = false) {
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: working, messages: history)
    }

    private func deskSays(_ id: String, _ text: String) {
        history.append(Message(id: id, cursor: id, threadID: "direct:atlas", author: "atlas", role: .agent,
                               sentAt: Date(), text: text))
        feed(working: true)
    }

    /// Wait for the outbox (one ordered task) to hand frames to the socket.
    private func until(_ what: String = "", timeout: TimeInterval = 2, _ condition: () -> Bool) async {
        let end = Date().addingTimeInterval(timeout)
        while !condition() && Date() < end { try? await Task.sleep(nanoseconds: 5_000_000) }
        XCTAssertTrue(condition(), "timed out waiting: \(what) — sent \(transport.sentTypes)")
    }

    /// Dial, and fail the test (not the whole run) when no live call came up.
    private func dial() async throws -> RealtimeCallSession {
        await session.startCall()
        return try XCTUnwrap(session.realtime, "no live call: \(session.notice ?? "no notice")")
    }

    // MARK: the contract

    func testTheDecksCallAnswerCarriesTheLiveVoice() throws {
        let json = """
        {"call_id":"c1","thread_id":"direct:atlas","voice":{"id":"","rate":1.0},
         "realtime":{"ws_url":"wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
          "client_secret":"ek_abc","expires_at":1790000000,"model":"gpt-realtime-2.1",
          "voice":"marin","instructions":"You are Atlas, …",
          "tools":[{"type":"function","name":"send_to_desk","description":"…",
           "parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}}
        """
        let start = try JSONDecoder().decode(CallStart.self, from: Data(json.utf8))
        let offer = try XCTUnwrap(start.realtime)
        XCTAssertEqual(offer.clientSecret, "ek_abc")
        XCTAssertEqual(offer.wsURL.host, "api.openai.com")
        XCTAssertEqual(offer.voice, "marin")
        XCTAssertEqual(offer.expiresAt, Date(timeIntervalSince1970: 1_790_000_000))
        XCTAssertEqual(offer.tools.count, 1)
        XCTAssertNil(start.realtimeError)
    }

    func testANoVoiceAnswerCarriesItsReasonAndStillStartsTheCall() throws {
        let json = #"{"call_id":"c1","thread_id":"direct:atlas","realtime_error":"no OPENAI_API_KEY on the deck"}"#
        let start = try JSONDecoder().decode(CallStart.self, from: Data(json.utf8))
        XCTAssertNil(start.realtime)
        XCTAssertEqual(start.realtimeError, "no OPENAI_API_KEY on the deck")
    }

    func testAMalformedOfferCostsTheVoiceNotTheCall() throws {
        let json = #"{"call_id":"c1","thread_id":"direct:atlas","realtime":{"ws_url":"https://x","client_secret":""}}"#
        let start = try JSONDecoder().decode(CallStart.self, from: Data(json.utf8))
        XCTAssertEqual(start.callID, "c1")
        XCTAssertNil(start.realtime)
        XCTAssertNotNil(start.realtimeError)
    }

    // MARK: dialling

    func testTheCallDialsTheLiveVoiceWithTheEphemeralSecret() async throws {
        let rt = try await dial()
        XCTAssertNotNil(session.realtime, "a call with an offer is a phone call")
        XCTAssertEqual(transport.connectedURL, offer.wsURL)
        XCTAssertEqual(transport.secret, "ek_test")
        XCTAssertTrue(audio.started)
        XCTAssertEqual(input.starts, 0, "Apple speech stays off on a live call — one mic owner")
        XCTAssertEqual(rt.phase, .listening)
        XCTAssertEqual(transport.sentTypes.first, "session.update")
        let update = transport.sent.first?["session"] as? [String: Any]
        XCTAssertEqual(update?["type"] as? String, "realtime")
        XCTAssertNil(update?["instructions"], "the minted session owns the instructions")
    }

    func testHisVoiceGoesOutAsPCMAppendsInTenthOfASecondChunks() async {
        await session.startCall()
        audio.speak(bytes: 2_400)
        XCTAssertFalse(transport.sentTypes.contains("input_audio_buffer.append"), "50 ms is held back")
        audio.speak(bytes: 2_400)
        await until("append") { transport.sentTypes.contains("input_audio_buffer.append") }
        let append = transport.sent.first { $0["type"] as? String == "input_audio_buffer.append" }
        let bytes = Data(base64Encoded: append?["audio"] as? String ?? "")
        XCTAssertEqual(bytes?.count, 4_800)
    }

    func testMuteSendsNoAudio() async throws {
        let rt = try await dial()
        session.toggleMute()
        audio.speak(bytes: 9_600)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(transport.sentTypes.contains("input_audio_buffer.append"))
        XCTAssertTrue(rt.isMuted)
    }

    // MARK: the agent talks, and stops when he does

    func testTheAgentsVoiceIsPlayedAsItArrives() async throws {
        let rt = try await dial()
        let pcm = PCM16.encode([0.1, 0.2, 0.3])
        rt.handle(#"{"type":"response.output_audio.delta","delta":"\#(pcm.base64EncodedString())"}"#)
        XCTAssertEqual(audio.played, [pcm])
        XCTAssertEqual(rt.phase, .speaking)
        XCTAssertEqual(rt.agentLevel, 0.6, "the character's mouth follows the agent's level")
        audio.drain()
        XCTAssertEqual(rt.phase, .listening)
        XCTAssertEqual(rt.agentLevel, 0)
    }

    func testTheOlderAudioEventNameIsPlayedToo() async throws {
        let rt = try await dial()
        let pcm = PCM16.encode([0.1])
        rt.handle(#"{"type":"response.audio.delta","delta":"\#(pcm.base64EncodedString())"}"#)
        XCTAssertEqual(audio.played.count, 1)
    }

    func testTalkingOverTheAgentStopsItAtOnce() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.3]).base64EncodedString())"}"#)
        XCTAssertTrue(audio.isPlaying)
        rt.handle(#"{"type":"input_audio_buffer.speech_started"}"#)
        XCTAssertEqual(audio.flushes, 1, "barge-in: whatever was queued is silenced")
        XCTAssertFalse(audio.isPlaying)
        XCTAssertEqual(rt.phase, .hearing)
        XCTAssertEqual(rt.agentLevel, 0)
        rt.handle(#"{"type":"input_audio_buffer.speech_stopped"}"#)
        XCTAssertEqual(rt.phase, .thinking)
    }

    // MARK: the agent's own voice is not him (measured: 6 false barge-ins in 75 s)

    private var sentAudio: [Data] {
        transport.sent.filter { $0["type"] as? String == "input_audio_buffer.append" }
            .compactMap { Data(base64Encoded: $0["audio"] as? String ?? "") }
    }

    func testTheAgentsEchoGoesOutAsSilenceWhileItSpeaks() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.3]).base64EncodedString())"}"#)
        audio.hear(db: -45, ms: 1_200)
        await until("appends") { sentAudio.count >= 5 }
        XCTAssertTrue(sentAudio.allSatisfy { $0.allSatisfy { $0 == 0 } },
                      "the speaker's echo in the mic never reaches the server's VAD")
    }

    func testHisVoiceOverTheAgentStillReachesTheServer() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.3]).base64EncodedString())"}"#)
        audio.hear(db: -60, ms: 600)
        audio.hear(db: -22, ms: 600)
        await until("his voice") { sentAudio.contains { $0.contains { $0 != 0 } } }
    }

    func testWithTheAgentQuietHisMicGoesStraightThrough() async {
        await session.startCall()
        audio.hear(db: -45, ms: 100)
        await until("append") { !sentAudio.isEmpty }
        XCTAssertTrue(sentAudio[0].contains { $0 != 0 })
    }

    func testHisLevelIsNotRedrawnWhileTheAgentTalks() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.3]).base64EncodedString())"}"#)
        audio.onMicLevel?(0.4)
        XCTAssertEqual(rt.micLevel, 0, "the face shows the agent's voice; his echo would only cost redraws")
        audio.drain()
        audio.onMicLevel?(0.4)
        XCTAssertEqual(rt.micLevel, 0.4)
    }

    // MARK: the caption — "ount to sign into.They…"

    func testANewReplyStartsANewCaption() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.created"}"#)
        rt.handle(#"{"type":"response.output_audio_transcript.delta","delta":"You need an account to sign into."}"#)
        rt.handle(#"{"type":"response.done"}"#)
        rt.handle(#"{"type":"response.created"}"#)
        rt.handle(#"{"type":"response.output_audio_transcript.delta","delta":"They said yes. "}"#)
        XCTAssertEqual(rt.caption, "They said yes.")
    }

    func testTheCaptionShowsWholeSentencesOnly() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.created"}"#)
        let long = "The deploy went out cleanly this morning. " + String(repeating: "Then the billing webhook failed twice. ", count: 4)
        rt.handle(#"{"type":"response.output_audio_transcript.delta","delta":"\#(long)"}"#)
        XCTAssertTrue(rt.caption.hasPrefix("Then"), "cut at a sentence, never mid-word: \(rt.caption)")
        XCTAssertLessThanOrEqual(rt.caption.count, CallCaption.limit)
    }

    // MARK: "voice call does not remember any of previous calls"

    func testBothSidesOfTheCallAreRecordedOnTheDeck() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"conversation.item.input_audio_transcription.completed","item_id":"i1","transcript":"Is the deploy done? "}"#)
        rt.handle(#"{"type":"response.output_audio_transcript.done","transcript":"Yes, it went out at nine."}"#)
        rt.handle(#"{"type":"conversation.item.input_audio_transcription.completed","transcript":"  "}"#)
        await until("transcript") { calls.transcript.count == 2 }
        XCTAssertEqual(calls.transcript.map(\.role), ["caller", "agent"])
        XCTAssertEqual(calls.transcript.map(\.text), ["Is the deploy done?", "Yes, it went out at nine."])
        XCTAssertEqual(Set(calls.transcript.map(\.callID)), ["call_1"])
    }

    // MARK: send_to_desk — the desk does the work

    func testSendToDeskPostsHisRequestIntoTheDesksThreadAndAnswersOK() async throws {
        let rt = try await dial()
        rt.handle(#"{"type":"response.created"}"#)
        rt.handle("""
        {"type":"response.function_call_arguments.done","call_id":"fc_1","name":"send_to_desk",
         "arguments":"{\\"text\\":\\"check why the acme build failed\\"}"}
        """)
        XCTAssertTrue(rt.isWorking, "the character shows the desk is on it")
        await until("post") { !calls.sent.isEmpty }
        XCTAssertEqual(calls.sent.first?.threadID, "direct:atlas")
        XCTAssertEqual(calls.sent.first?.text, "check why the acme build failed")
        XCTAssertEqual(calls.sent.first?.callID, "call_1")
        await until("function output") { transport.sentTypes.contains("conversation.item.create") }
        let item = transport.sent.first { $0["type"] as? String == "conversation.item.create" }?["item"] as? [String: Any]
        XCTAssertEqual(item?["type"] as? String, "function_call_output")
        XCTAssertEqual(item?["call_id"] as? String, "fc_1")
        XCTAssertEqual(item?["output"] as? String, #"{"ok":true}"#)
        XCTAssertFalse(rt.isWorking)
        XCTAssertFalse(transport.sentTypes.contains("response.create"),
                       "a response is running: asking for another now would be refused")
        rt.handle(#"{"type":"response.done","response":{"output":[]}}"#)
        await until("response.create after done") { transport.sentTypes.contains("response.create") }
        let types = transport.sentTypes
        XCTAssertLessThan(types.firstIndex(of: "conversation.item.create")!, types.firstIndex(of: "response.create")!)
    }

    func testTheSameToolCallSeenTwiceIsPostedOnce() async throws {
        let rt = try await dial()
        let args = #"{\"text\":\"deploy\"}"#
        rt.handle(#"{"type":"response.function_call_arguments.done","call_id":"fc_9","name":"send_to_desk","arguments":"\#(args)"}"#)
        rt.handle(#"{"type":"response.done","response":{"output":[{"type":"function_call","call_id":"fc_9","name":"send_to_desk","arguments":"\#(args)"}]}}"#)
        await until("post") { !calls.sent.isEmpty }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(calls.sent.count, 1)
    }

    func testAFailedPostIsToldToTheVoiceNotSwallowed() async throws {
        calls.failSend = true
        let rt = try await dial()
        rt.handle(#"{"type":"response.function_call_arguments.done","call_id":"fc_2","name":"send_to_desk","arguments":"{\"text\":\"x\"}"}"#)
        await until("output") { transport.sentTypes.contains("conversation.item.create") }
        let item = transport.sent.first { $0["type"] as? String == "conversation.item.create" }?["item"] as? [String: Any]
        XCTAssertTrue((item?["output"] as? String)?.contains(#""ok":false"#) == true, "\(String(describing: item))")
    }

    // MARK: the desk's progress, relayed while he talks

    func testEveryNewLineFromTheDeskIsHandedToTheVoice() async {
        await session.startCall()
        deskSays("r1", "Found it: the acme build fails on a missing env var. Fixing now.")
        await until("update") { transport.sentTypes.contains("response.create") }
        let item = transport.sent.first { $0["type"] as? String == "conversation.item.create" }?["item"] as? [String: Any]
        let content = (item?["content"] as? [[String: Any]])?.first?["text"] as? String
        XCTAssertEqual(content, "[Atlas update] Found it: the acme build fails on a missing env var. Fixing now.")
        XCTAssertTrue(output.spoken.isEmpty, "the live voice relays it; the Mac's own speech stays quiet")
    }

    func testHistoryIsNotRelayedWhenTheCallStarts() async throws {
        await session.startCall()
        feed()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(transport.sentTypes.contains("conversation.item.create"))
    }

    // MARK: hanging up, dropping, falling back

    func testHangingUpClosesTheLineAndEndsTheCallOnTheDeck() async throws {
        let rt = try await dial()
        let live = rt
        await session.hangUp()
        XCTAssertTrue(transport.closed)
        XCTAssertTrue(audio.stopped)
        XCTAssertEqual(live.phase, .ended)
        XCTAssertNil(session.realtime)
        XCTAssertEqual(calls.ended, ["call_1"])
    }

    func testADroppedLineEndsTheCallAndSaysSo() async throws {
        let rt = try await dial()
        transport.drop()
        await rt.untilTheLineCloses()
        // Off this Mac in the same turn as the drop, before the deck answers.
        XCTAssertEqual(rt.phase, .ended)
        XCTAssertTrue(audio.stopped)
        XCTAssertNil(session.realtime)
        XCTAssertNil(session.call)
        XCTAssertEqual(session.notice?.contains("dropped"), true, session.notice ?? "")
        let ending = try XCTUnwrap(session.ending, "the deck is told the call is over")
        await ending.value
        XCTAssertEqual(calls.ended, ["call_1"])
    }

    func testAPassIsJudgedByTheSessionsClockNotByWhenTheSuiteGotHere() async throws {
        // The fixture's pass expired days ago by the wall clock; by the
        // session's clock it has a minute left, so the call dials.
        XCTAssertLessThan(offer.expiresAt!, Date())
        _ = try await dial()
        XCTAssertEqual(transport.connectedURL, offer.wsURL)
    }

    func testAPassPastItsExpiryOnTheSessionsClockIsRefused() async {
        let late = RealtimeCallSession(offer: offer, desk: "atlas", displayName: "Atlas", callID: "c", threadID: "t",
                                       transport: transport, audio: audio, calls: calls,
                                       now: { RealtimeCallTests.minted.addingTimeInterval(61) })
        let dialled = await late.start()
        XCTAssertFalse(dialled)
        XCTAssertNil(transport.connectedURL, "an expired pass never reaches the socket")
        XCTAssertEqual(late.problem, "The call's voice pass had already expired.")
    }

    func testNoLiveVoiceFallsBackToTheMacsOwnSpeechAndSaysWhy() async {
        calls.realtime = nil
        calls.realtimeError = "no OPENAI_API_KEY on the deck"
        await session.startCall()
        XCTAssertNil(session.realtime)
        XCTAssertEqual(input.starts, 1, "the Apple-speech call listens instead")
        XCTAssertEqual(session.state, .listening)
        XCTAssertEqual(session.notice?.contains("no OPENAI_API_KEY"), true, session.notice ?? "")
    }

    func testALineThatWontConnectFallsBackToo() async {
        transport.failConnect = true
        await session.startCall()
        XCTAssertNil(session.realtime)
        XCTAssertNotNil(session.call, "the call itself stays up")
        XCTAssertEqual(input.starts, 1)
        XCTAssertEqual(session.notice?.contains("Couldn't connect the live voice"), true)
    }

    func testHoldToTalkNeverDialsTheLiveVoice() async {
        await session.pressMic()
        input.say("status")
        await session.releaseMic()
        XCTAssertNil(session.realtime)
        XCTAssertNil(transport.connectedURL)
    }
}
