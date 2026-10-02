import XCTest
import Combine
@testable import DeckKit

// MARK: - Fakes

/// One ordered record of everything the session did to the outside world, so
/// "the acknowledgement is spoken before the deck is asked" is a fact about
/// order and not about timing.
final class VoiceLog: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [String] = []
    func add(_ entry: String) { lock.lock(); entries.append(entry); lock.unlock() }
    var all: [String] { lock.lock(); defer { lock.unlock() }; return entries }
}

@MainActor
final class FakeSpeechInput: SpeechInput {
    var permissionAnswer: VoicePermission = .granted
    var cancelsEcho = false
    var onLevel: ((Float) -> Void)?
    /// `cancelsEcho` at each `start`, in order.
    private(set) var echoAtStart: [Bool] = []
    var transcript = ""
    private(set) var starts = 0
    private(set) var finishes = 0
    private(set) var cancels = 0
    private(set) var permissionAsks = 0
    private(set) var isRunning = false
    private var heard: ((String) -> Void)?
    let log: VoiceLog

    init(log: VoiceLog) { self.log = log }

    var holdPermission = false
    private var gate: CheckedContinuation<Void, Never>?

    func permission() async -> VoicePermission {
        permissionAsks += 1
        if holdPermission { await withCheckedContinuation { gate = $0 } }
        return permissionAnswer
    }

    /// He clicks a button on the system prompt.
    func answerPermission() {
        gate?.resume()
        gate = nil
    }

    func start(heard: @escaping @MainActor (String) -> Void) throws {
        starts += 1
        echoAtStart.append(cancelsEcho)
        isRunning = true
        self.heard = heard
        log.add("listen")
    }

    func finish() async -> String {
        finishes += 1
        isRunning = false
        return transcript
    }

    func cancel() {
        cancels += 1
        isRunning = false
    }

    /// He says something: the recogniser's partial result arrives.
    func say(_ text: String) {
        transcript = text
        heard?(text)
    }
}

@MainActor
final class FakeSpeechOutput: SpeechOutput {
    var onFinish: (() -> Void)?
    var onLevel: ((Float) -> Void)?
    private(set) var spoken: [SpokenLine] = []
    private(set) var stops = 0
    private(set) var current: SpokenLine?
    let log: VoiceLog

    init(log: VoiceLog) { self.log = log }

    func speak(_ line: SpokenLine) {
        spoken.append(line)
        current = line
        log.add("speak:\(line.text)")
    }

    func stop() {
        stops += 1
        current = nil
    }

    /// The synthesiser reaches the end of the line it was given.
    func finishCurrent() {
        guard current != nil else { return }
        current = nil
        onFinish?()
    }
}

final class FakeCalls: CallClient, @unchecked Sendable {
    let log: VoiceLog
    var voice: DeskVoice? = DeskVoice(id: "", rate: 1.0)
    var failStart = false
    var failSend = false
    /// C3: what the 201 carries about the live voice.
    var realtime: RealtimeOffer?
    var realtimeError: String?
    private let lock = NSLock()
    private var counter = 0
    private(set) var sent: [(threadID: String, text: String, callID: String)] = []
    private(set) var ended: [String] = []
    private(set) var started: [String] = []

    init(log: VoiceLog) { self.log = log }

    func startCall(agent: String) async throws -> CallStart {
        log.add("start:\(agent)")
        if failStart { throw DeckError.transport("offline") }
        lock.lock(); counter += 1; let n = counter; started.append(agent); lock.unlock()
        return CallStart(callID: "call_\(n)", threadID: "direct:\(agent)", voice: voice,
                         realtime: realtime, realtimeError: realtimeError)
    }

    func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        log.add("send:\(text)")
        if failSend { throw DeckError.transport("offline") }
        lock.lock(); sent.append((threadID, text, callID)); lock.unlock()
        return Message(id: "m-sent-\(sent.count)", cursor: "", threadID: threadID, author: DeckOwner.name,
                       role: .owner, sentAt: Date(), text: text, channel: .voice)
    }

    func endCall(id: String) async throws {
        log.add("end:\(id)")
        lock.lock(); ended.append(id); lock.unlock()
    }

    private(set) var transcript: [(callID: String, role: String, text: String)] = []

    func postTranscript(callID: String, lines: [CallLine]) async throws {
        lock.lock(); transcript += lines.map { (callID, $0.role.rawValue, $0.text) }; lock.unlock()
    }
}

// MARK: - Tests

@MainActor
final class VoiceSessionTests: XCTestCase {
    private var log: VoiceLog!
    private var input: FakeSpeechInput!
    private var output: FakeSpeechOutput!
    private var calls: FakeCalls!
    private var session: VoiceSession!
    private var history: [Message] = []
    private var states: [VoiceSession.State] = []
    private var watch: AnyCancellable?

    override func setUp() async throws {
        log = VoiceLog()
        input = FakeSpeechInput(log: log)
        output = FakeSpeechOutput(log: log)
        calls = FakeCalls(log: log)
        session = VoiceSession(input: input, output: output, calls: { [calls] in calls })
        history = [
            line("h1", author: "atlas", role: .agent, "Yesterday's news. Nobody should hear it again."),
            line("h2", author: DeckOwner.name, role: .owner, "ok"),
        ]
        states = []
        watch = session.$state.removeDuplicates().sink { [weak self] in self?.states.append($0) }
        feed(working: false)
    }

    override func tearDown() async throws {
        watch = nil
    }

    private func line(_ id: String, author: String, role: MessageRole, _ text: String,
                      kind: MessageKind = .text) -> Message {
        Message(id: id, cursor: id, threadID: "direct:atlas", author: author, role: role,
                sentAt: Date(), text: text, kind: kind)
    }

    private func feed(working: Bool, voice: DeskVoice? = nil) {
        session.sync(desk: "atlas", displayName: "Atlas", voice: voice, working: working, messages: history)
    }

    private func deskSays(_ id: String, _ text: String, working: Bool = false) {
        history.append(line(id, author: "atlas", role: .agent, text))
        feed(working: working)
    }

    private func pushToTalk(_ words: String) async {
        await session.pressMic()
        input.say(words)
        await session.releaseMic()
    }

    // C-2: the whole push-to-talk loop, state by state.

    func testPushToTalkWalksIdleListeningSendingWaitingSpeakingIdle() async {
        await session.pressMic()
        XCTAssertEqual(session.state, .listening)
        input.say("what's the status of acme")
        await session.releaseMic()
        XCTAssertEqual(session.state, .waiting)
        output.finishCurrent() // the "Asking Atlas…" acknowledgement
        XCTAssertEqual(session.state, .waiting, "the acknowledgement is not the desk's answer")

        deskSays("r1", "Acme is green. Tests pass on main. Deploying next.")
        XCTAssertEqual(session.state, .speaking)
        XCTAssertEqual(output.spoken.last?.text, "Acme is green. Tests pass on main.")
        output.finishCurrent()
        XCTAssertEqual(session.state, .idle)
        XCTAssertEqual(states, [.idle, .listening, .sending, .waiting, .speaking, .idle])
    }

    func testTheAcknowledgementIsSpokenBeforeTheDeckIsAsked() async {
        await pushToTalk("what's the status of acme")
        let order = log.all
        let ack = order.firstIndex(of: "speak:Asking Atlas…")
        let start = order.firstIndex { $0.hasPrefix("start:") }
        let send = order.firstIndex { $0.hasPrefix("send:") }
        XCTAssertNotNil(ack, "\(order)")
        XCTAssertLessThan(ack!, start!, "\(order)")
        XCTAssertLessThan(ack!, send!, "\(order)")
    }

    func testTheUtteranceGoesOutOnTheVoiceChannelWithTheCallsID() async {
        await pushToTalk("what's the status of acme")
        XCTAssertEqual(calls.sent.count, 1)
        XCTAssertEqual(calls.sent.first?.threadID, "direct:atlas")
        XCTAssertEqual(calls.sent.first?.text, "what's the status of acme")
        XCTAssertEqual(calls.sent.first?.callID, "call_1")
    }

    func testPushToTalkOpensOneCallAndReusesIt() async {
        await pushToTalk("one")
        await pushToTalk("two")
        XCTAssertEqual(calls.started, ["atlas"])
        XCTAssertEqual(calls.sent.map(\.callID), ["call_1", "call_1"])
        XCTAssertEqual(session.call?.handsFree, false)
    }

    func testSilenceSendsNothing() async {
        await pushToTalk("   ")
        XCTAssertTrue(calls.sent.isEmpty)
        XCTAssertTrue(output.spoken.isEmpty)
        XCTAssertEqual(session.state, .idle)
    }

    // What is spoken, and what is not.

    func testHistoryIsNeverReadOutWhenAThreadOpens() async {
        await pushToTalk("hi")
        XCTAssertFalse(output.spoken.contains { $0.text.contains("Yesterday") })
    }

    func testEverySayLineIsSpokenInTheDesksVoice() async {
        await pushToTalk("status of acme")
        output.finishCurrent()
        deskSays("s1", "On it — checking acme now.", working: true)
        XCTAssertEqual(output.spoken.last?.text, "On it — checking acme now.")
        XCTAssertEqual(output.spoken.last?.desk, "atlas")
        output.finishCurrent()
        XCTAssertEqual(session.state, .waiting, "the desk is still working on the answer")
        deskSays("s2", "Acme is green. Nothing to do. Details in the chat.")
        XCTAssertEqual(output.spoken.last?.text, "Acme is green. Nothing to do.")
    }

    func testLinesThatArriveWhileSpeakingWaitTheirTurn() async {
        await pushToTalk("status")
        output.finishCurrent()
        deskSays("s1", "One.", working: true)
        deskSays("s2", "Two.", working: true)
        XCTAssertEqual(output.spoken.map(\.text).suffix(1), ["One."])
        output.finishCurrent()
        XCTAssertEqual(output.spoken.map(\.text).suffix(1), ["Two."])
    }

    func testOnlyTheDesksOwnLinesAreSpoken() async {
        await pushToTalk("status")
        output.finishCurrent()
        history.append(line("o1", author: DeckOwner.name, role: .owner, "status"))
        history.append(line("d1", author: "deck", role: .system, "[Agent Deck] The owner started a live voice call."))
        history.append(line("p1", author: "scout", role: .agent, "Scout here, not Atlas."))
        history.append(line("c1", author: "atlas", role: .agent, "```\nls\n```"))
        feed(working: true)
        XCTAssertEqual(output.spoken.map(\.text), ["Asking Atlas…"])
    }

    func testNothingIsSpokenWithoutACallOrAPushToTalk() {
        deskSays("r1", "An ordinary reply to a typed message.")
        XCTAssertTrue(output.spoken.isEmpty)
    }

    func testTheDesksVoiceComesFromTheCallThenTheRoster() async {
        calls.voice = DeskVoice(id: "com.apple.voice.premium.en-GB.Malcolm", rate: 1.1)
        feed(working: false, voice: DeskVoice(id: "roster-voice", rate: 0.9))
        await pushToTalk("status")
        output.finishCurrent()
        deskSays("r1", "Green.")
        XCTAssertEqual(output.spoken.last?.voice, DeskVoice(id: "com.apple.voice.premium.en-GB.Malcolm", rate: 1.1))

        calls.voice = DeskVoice(id: "", rate: 1.0)
        let other = VoiceSession(input: input, output: output, calls: { [calls] in calls })
        other.sync(desk: "atlas", displayName: "Atlas", voice: DeskVoice(id: "roster-voice", rate: 0.9),
                   working: false, messages: history)
        await other.pressMic(); input.say("again"); await other.releaseMic()
        output.finishCurrent()
        history.append(line("r2", author: "atlas", role: .agent, "Still green."))
        other.sync(desk: "atlas", displayName: "Atlas", voice: DeskVoice(id: "roster-voice", rate: 0.9),
                   working: false, messages: history)
        XCTAssertEqual(output.spoken.last?.voice, DeskVoice(id: "roster-voice", rate: 0.9))
    }

    // Barge-in.

    func testPressingTheMicStopsTheDeskMidSentence() async {
        await pushToTalk("status")
        output.finishCurrent()
        deskSays("s1", "A long answer that he does not want to hear the end of.", working: true)
        deskSays("s2", "And more.", working: true)
        let stopsBefore = output.stops
        await session.pressMic()
        XCTAssertEqual(output.stops, stopsBefore + 1)
        XCTAssertEqual(session.state, .listening)
        input.say("stop, different question")
        await session.releaseMic()
        XCTAssertFalse(output.spoken.contains { $0.text == "And more." }, "the queue is dropped too")
    }

    func testTalkingOnACallStopsTheDeskMidSentence() async {
        await session.startCall()
        input.say("status")
        await session.endUtterance()
        output.finishCurrent()
        deskSays("s1", "A long answer.", working: true)
        XCTAssertEqual(session.state, .speaking)
        let stopsBefore = output.stops
        input.say("wait")
        XCTAssertEqual(output.stops, stopsBefore + 1)
        XCTAssertEqual(session.state, .listening)
    }

    // Calls.

    func testACallListensHandsFreeAndHangsUp() async {
        await session.startCall()
        XCTAssertEqual(session.call?.id, "call_1")
        XCTAssertEqual(session.call?.handsFree, true)
        XCTAssertEqual(session.state, .listening)
        XCTAssertTrue(input.isRunning)

        input.say("what's the status of acme")
        await session.endUtterance()
        XCTAssertEqual(calls.sent.map(\.callID), ["call_1"])
        XCTAssertTrue(input.isRunning, "a call keeps listening while the desk thinks")

        await session.hangUp()
        XCTAssertNil(session.call)
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertFalse(input.isRunning)
        XCTAssertEqual(session.state, .idle)
    }

    func testAPauseEndsTheUtteranceOnACall() async throws {
        session.silenceWindow = 0.05
        await session.startCall()
        input.say("status of acme")
        try await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertEqual(calls.sent.map(\.text), ["status of acme"])
    }

    func testMuteStopsListeningAndIgnoresWhatIsHeard() async throws {
        session.silenceWindow = 0.05
        await session.startCall()
        session.toggleMute()
        XCTAssertTrue(session.isMuted)
        XCTAssertFalse(input.isRunning)
        input.say("private aside")
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertTrue(calls.sent.isEmpty)
        session.toggleMute()
        XCTAssertTrue(input.isRunning)
    }

    func testLeavingTheDeskKeepsThePhoneCall() async {
        // Was C4 "switching desks hangs up"; the owner: "when i move to other
        // agent the call gets disconnected". A phone call belongs to the app.
        await session.startCall()
        session.sync(desk: "scout", displayName: "Scout", voice: nil, working: false, messages: [])
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(session.call?.desk, "atlas")
        XCTAssertTrue(calls.ended.isEmpty)
    }

    func testACallThatCannotStartSaysSoAndTypingIsUntouched() async {
        calls.failStart = true
        await session.startCall()
        XCTAssertNil(session.call)
        XCTAssertNotNil(session.notice)
        XCTAssertEqual(session.state, .idle)
    }

    // C-1: permission.

    func testDeniedPermissionIsOnePlainSentenceAndNothingElseHappens() async {
        input.permissionAnswer = .denied(VoicePermission.microphoneOff)
        await session.pressMic()
        await session.releaseMic()
        XCTAssertEqual(session.notice, VoicePermission.microphoneOff)
        XCTAssertTrue(session.notice?.contains("Typing still works") ?? false)
        XCTAssertEqual(input.starts, 0)
        XCTAssertTrue(calls.started.isEmpty)
        XCTAssertEqual(session.state, .idle)

        await session.startCall()
        XCTAssertNil(session.call, "no call without a microphone")
    }

    func testTheNoticeClearsOnceHeCanBeHeard() async {
        input.permissionAnswer = .denied(VoicePermission.microphoneOff)
        await session.pressMic(); await session.releaseMic()
        input.permissionAnswer = .granted
        await session.pressMic()
        XCTAssertNil(session.notice)
    }

    func testAReleaseWhileThePermissionPromptIsUpStartsNothing() async {
        // A tap, or a prompt he answers after letting go: the mic must not be
        // left open with nobody holding the button.
        input.holdPermission = true
        let press = Task { await session.pressMic() }
        for _ in 0..<50 where input.permissionAsks == 0 { await Task.yield() }
        XCTAssertEqual(input.permissionAsks, 1)
        await session.releaseMic()
        input.answerPermission()
        await press.value
        XCTAssertEqual(input.starts, 0)
        XCTAssertFalse(input.isRunning)
        XCTAssertEqual(session.state, .idle)
        XCTAssertTrue(calls.sent.isEmpty)
    }

    // C-4: the measurement is reachable from the app.

    /// Reported, not gated here: this suite also runs on machines that are
    /// not his. Creating a recogniser asks for nothing.
    func testOnDeviceSupportCanBeAskedWithoutAPrompt() {
        let english = OnDeviceSpeech.supports("en-US")
        let hebrew = OnDeviceSpeech.supports("he-IL")
        print("C-4 supportsOnDeviceRecognition en-US=\(english) he-IL=\(hebrew)")
    }
}

extension VoiceSessionTests {
    /// The Mac's own speaker picked up by the mic must neither interrupt the
    /// desk nor be sent back to it as if he had said it.
    func testTheDesksOwnWordsThroughTheMicAreNotHim() async {
        await session.startCall()
        input.say("status")
        await session.endUtterance()
        output.finishCurrent()
        deskSays("s1", "Acme is green and deploying.", working: true)
        let stops = output.stops
        input.say("acme is green")
        XCTAssertEqual(output.stops, stops)
        XCTAssertEqual(session.state, .speaking)
    }
}
