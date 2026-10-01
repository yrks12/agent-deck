import XCTest
@testable import DeckKit

/// Owner, 2026-09-30:
/// (a) "when I chat with voice it answers in voice automatically, but we should
///     be able to switch it off, and use normal voice like in the call."
/// (b) "I want also to be able to just record a message and not call it."
///
/// - **Spoken replies** is a per-device switch, on by default. Off means a
///   reply is text only, even right after he spoke — the speaker is never
///   handed a line.
/// - On, a reply is said in the desk's CALL voice, fetched from the deck
///   (`POST /v1/speech`, the key stays on the box), cached per line; Apple's
///   voice only when the deck cannot, and the trace says so.
/// - A **voice message** is recorded, uploaded, transcribed on the deck and
///   lands as his own message; his bubble plays the recording back. No call.
///
/// Nothing here makes a sound or opens the mic: every speaker, player and
/// recorder is a fake, and the real ones are pinned silent by `AudioGate`.

// MARK: - Fakes

final class FakeSpeechClient: SpeechClient, @unchecked Sendable {
    private let lock = NSLock()
    private var _asked: [(text: String, agent: String)] = []
    var fail = false
    var audio = Data("ID3 fake mp3".utf8)
    var asked: [(text: String, agent: String)] { lock.lock(); defer { lock.unlock() }; return _asked }

    func speech(text: String, agent: String) async throws -> Data {
        lock.lock(); _asked.append((text, agent)); lock.unlock()
        if fail { throw DeckError.http(502, reason: "speech_failed") }
        return audio
    }
}

@MainActor
final class FakeClipPlayer: AudioClipPlayer {
    var onFinish: (() -> Void)?
    var onLevel: ((Float) -> Void)?
    private(set) var played: [Data] = []
    private(set) var stops = 0

    func play(_ audio: Data) throws { played.append(audio) }
    func stop() { stops += 1 }
    func finish() { onFinish?() }
}

final class FakeVoiceNotes: VoiceNoteClient, @unchecked Sendable {
    private let lock = NSLock()
    private var _uploads: [(threadID: String, audio: Data)] = []
    private var _fetches: [String] = []
    var fail = false
    var transcript = "תשלח לי את הדוח, and the numbers"
    var uploads: [(threadID: String, audio: Data)] { lock.lock(); defer { lock.unlock() }; return _uploads }
    var fetches: [String] { lock.lock(); defer { lock.unlock() }; return _fetches }

    func sendVoiceNote(threadID: String, audio: Data) async throws -> VoiceNoteSent {
        if fail { throw DeckError.http(502, reason: "transcription_failed") }
        lock.lock(); _uploads.append((threadID, audio)); lock.unlock()
        let message = Message(id: "m-note", cursor: "m-note", threadID: threadID, author: DeckOwner.name,
                              role: .owner, sentAt: Date(), text: transcript,
                              voiceNote: VoiceNoteRef(id: "vn_1", url: "/v1/voice-notes/vn_1"))
        return VoiceNoteSent(message: message, transcript: transcript)
    }

    func voiceNoteAudio(id: String) async throws -> Data {
        lock.lock(); _fetches.append(id); lock.unlock()
        return Data("recording \(id)".utf8)
    }
}

@MainActor
final class FakeRecorder: VoiceRecorder {
    var onLevel: ((Float) -> Void)?
    var permissionAnswer: VoicePermission = .granted
    var recording = Data("m4a bytes".utf8)
    private(set) var starts = 0
    private(set) var cancels = 0
    private(set) var finishes = 0

    func permission() async -> VoicePermission { permissionAnswer }
    func start() throws { starts += 1 }
    func finish() async throws -> Data { finishes += 1; return recording }
    func cancel() { cancels += 1 }
}

private func msg(_ id: String, _ author: String, _ role: MessageRole, _ text: String) -> Message {
    Message(id: id, cursor: id, threadID: "direct:atlas", author: author, role: role,
            sentAt: Date(), text: text)
}

// MARK: - The switch

final class SpokenRepliesSettingTests: XCTestCase {
    private func freshDefaults() -> UserDefaults {
        let name = "spoken-replies-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    func testOnByDefault() {
        XCTAssertTrue(SpokenReplies.isOn(in: freshDefaults()))
    }

    func testOffIsRememberedOnThisDevice() {
        let defaults = freshDefaults()
        SpokenReplies.set(false, in: defaults)
        XCTAssertFalse(SpokenReplies.isOn(in: defaults))
        XCTAssertEqual(defaults.object(forKey: SpokenReplies.key) as? Bool, false,
                       "stored under the key the views bind with @AppStorage")
        SpokenReplies.set(true, in: defaults)
        XCTAssertTrue(SpokenReplies.isOn(in: defaults))
    }
}

// MARK: - Hold to talk obeys the switch

@MainActor
final class HoldToTalkObeysSpokenRepliesTests: XCTestCase {
    private var log: VoiceLog!
    private var input: FakeSpeechInput!
    private var output: FakeSpeechOutput!
    private var calls: FakeCalls!
    private var spoken = true
    private var session: VoiceSession!
    private var history: [Message] = []

    override func setUp() async throws {
        log = VoiceLog()
        input = FakeSpeechInput(log: log)
        output = FakeSpeechOutput(log: log)
        calls = FakeCalls(log: log)
        session = VoiceSession(input: input, output: output, calls: { [calls] in calls },
                               spokenReplies: { [unowned self] in self.spoken })
        history = [msg("h1", "atlas", .agent, "Old news.")]
        feed()
    }

    private func feed(working: Bool = false) {
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: working, messages: history)
    }

    private func pushToTalk(_ words: String) async {
        await session.pressMic()
        input.say(words)
        await session.releaseMic()
    }

    func testSpokenRepliesOffNeverSpeaksEvenAfterHeSpoke() async {
        spoken = false
        await pushToTalk("what's the status of acme")
        XCTAssertEqual(calls.sent.map(\.text), ["what's the status of acme"], "what he said still goes")
        history.append(msg("r1", "atlas", .agent, "Acme is green. Tests pass."))
        feed()
        XCTAssertTrue(output.spoken.isEmpty, "the speaker was handed \(output.spoken.map(\.text))")
        XCTAssertEqual(session.state, .idle, "a text-only answer still ends the turn")
    }

    func testSpokenRepliesOnSpeaksTheAnswer() async {
        spoken = true
        await pushToTalk("what's the status of acme")
        output.finishCurrent()
        history.append(msg("r1", "atlas", .agent, "Acme is green. Tests pass."))
        feed()
        XCTAssertEqual(output.spoken.map(\.text), ["Asking Atlas…", "Acme is green. Tests pass."])
    }

    func testTheSwitchIsReadAtReplyTimeNotWhenHeSpoke() async {
        spoken = true
        await pushToTalk("status?")
        output.finishCurrent()
        spoken = false
        history.append(msg("r1", "atlas", .agent, "All green."))
        feed()
        XCTAssertEqual(output.spoken.map(\.text), ["Asking Atlas…"])
    }

    func testAHandsFreeCallIsACallAndStillSpeaks() async {
        spoken = false
        await session.startCall()
        XCTAssertEqual(session.call?.handsFree, true)
        history.append(msg("r1", "atlas", .agent, "On a call you hear me."))
        feed()
        XCTAssertEqual(output.spoken.map(\.text), ["On a call you hear me."])
    }
}

// MARK: - The call's voice, from the deck

@MainActor
final class DeckSpeechOutputTests: XCTestCase {
    private var client: FakeSpeechClient!
    private var player: FakeClipPlayer!
    private var fallback: FakeSpeechOutput!
    private var output: DeckSpeechOutput!

    override func setUp() async throws {
        client = FakeSpeechClient()
        player = FakeClipPlayer()
        fallback = FakeSpeechOutput(log: VoiceLog())
        output = DeckSpeechOutput(client: { [client] in client }, fallback: fallback, player: player)
        VoiceTrace.reset()
    }

    private func say(_ text: String, desk: String = "atlas") async {
        output.speak(SpokenLine(text: text, desk: desk))
        await output.pending?.value
    }

    func testALineIsSpokenInTheDecksVoice() async {
        await say("Acme is green.")
        XCTAssertEqual(client.asked.map(\.text), ["Acme is green."])
        XCTAssertEqual(client.asked.map(\.agent), ["atlas"])
        XCTAssertEqual(player.played, [client.audio])
        XCTAssertTrue(fallback.spoken.isEmpty, "Apple's voice is only the fallback")
    }

    func testTheSameLineIsFetchedOnce() async {
        await say("Acme is green.")
        player.finish()
        await say("Acme is green.")
        XCTAssertEqual(client.asked.count, 1, "the second time comes from the cache")
        XCTAssertEqual(player.played.count, 2)
    }

    func testAnotherDeskSayingTheSameWordsIsAnotherVoice() async {
        await say("Done.", desk: "atlas")
        await say("Done.", desk: "acme")
        XCTAssertEqual(client.asked.map(\.agent), ["atlas", "acme"])
    }

    func testWhenTheDeckCannotSpeakAppleDoesAndTheTraceSaysSo() async {
        client.fail = true
        await say("Acme is green.")
        XCTAssertTrue(player.played.isEmpty)
        XCTAssertEqual(fallback.spoken.map(\.text), ["Acme is green."])
        XCTAssertTrue(VoiceTrace.recent.contains { $0.hasPrefix("tts.fallback") }, "\(VoiceTrace.recent)")
    }

    func testFinishingThePlayerFinishesTheLine() async {
        var finished = 0
        output.onFinish = { finished += 1 }
        await say("Acme is green.")
        player.finish()
        XCTAssertEqual(finished, 1)
    }

    func testFallbackFinishingAlsoFinishesTheLine() async {
        var finished = 0
        output.onFinish = { finished += 1 }
        client.fail = true
        await say("Acme is green.")
        fallback.finishCurrent()
        XCTAssertEqual(finished, 1)
    }

    func testStoppedBeforeTheAudioArrivesNothingPlays() async {
        output.speak(SpokenLine(text: "Too late.", desk: "atlas"))
        output.stop()
        await output.pending?.value
        XCTAssertTrue(player.played.isEmpty)
        XCTAssertTrue(fallback.spoken.isEmpty)
    }

    func testTheRealPlayerIsSilentInATestProcess() {
        let real = SystemAudioClipPlayer()
        var finished = false
        real.onFinish = { finished = true }
        try? real.play(Data("not audio".utf8))
        XCTAssertTrue(finished, "a silenced clip still reports finished")
        XCTAssertFalse(real.hasBuiltPlayer, "no AVAudioPlayer may exist inside a test process")
    }
}

// MARK: - After a voice message, the answer is spoken (if he wants it)

@MainActor
final class ReplySpeakerTests: XCTestCase {
    private var output: FakeSpeechOutput!
    private var spoken = true
    private var speaker: ReplySpeaker!
    private var history: [Message] = []

    override func setUp() async throws {
        output = FakeSpeechOutput(log: VoiceLog())
        speaker = ReplySpeaker(output: output, spokenReplies: { [unowned self] in self.spoken })
        history = [msg("h1", "atlas", .agent, "Old news."),
                   msg("m-note", DeckOwner.name, .owner, "send me the report")]
    }

    private func deskSays(_ id: String, _ text: String) {
        history.append(msg(id, "atlas", .agent, text))
        speaker.sync(desk: "atlas", messages: history)
    }

    func testTheFirstAnswerAfterHisVoiceMessageIsSpoken() {
        speaker.arm(desk: "atlas", after: "m-note")
        speaker.sync(desk: "atlas", messages: history)
        XCTAssertTrue(output.spoken.isEmpty, "his own message and history are not read out")
        deskSays("r1", "Sent it. Check your inbox. Here is `bin/x.py`.")
        XCTAssertEqual(output.spoken.map(\.text), ["Sent it. Check your inbox."])
        XCTAssertEqual(output.spoken.first?.desk, "atlas")
        deskSays("r2", "Anything else?")
        XCTAssertEqual(output.spoken.count, 1, "one answer per voice message")
    }

    func testSpokenRepliesOffNeverSpeaksAfterAVoiceMessage() {
        spoken = false
        speaker.arm(desk: "atlas", after: "m-note")
        deskSays("r1", "Sent it.")
        XCTAssertTrue(output.spoken.isEmpty)
    }

    func testATypedMessageArmsNothing() {
        deskSays("r1", "Sent it.")
        XCTAssertTrue(output.spoken.isEmpty)
    }

    func testAnotherDeskIsNotSpokenForThisOne() {
        speaker.arm(desk: "atlas", after: "m-note")
        speaker.sync(desk: "acme", messages: [msg("p1", "acme", .agent, "Hi from Acme.")])
        XCTAssertTrue(output.spoken.isEmpty)
    }
}

// MARK: - Recording a voice message

@MainActor
final class VoiceNoteComposerTests: XCTestCase {
    private var recorder: FakeRecorder!
    private var notes: FakeVoiceNotes!
    private var composer: VoiceNoteComposer!
    private var sent: [Message] = []

    override func setUp() async throws {
        recorder = FakeRecorder()
        notes = FakeVoiceNotes()
        composer = VoiceNoteComposer(recorder: recorder, client: { [notes] in notes })
        sent = []
        composer.onSent = { [unowned self] in self.sent.append($0) }
    }

    func testRecordThenSendUploadsAndLandsAsHisMessage() async {
        await composer.start(threadID: "direct:atlas")
        XCTAssertEqual(composer.state, .recording)
        recorder.onLevel?(0.4)
        recorder.onLevel?(0.9)
        XCTAssertEqual(composer.levels, [0.4, 0.9], "the waveform is his voice")
        await composer.send()
        XCTAssertEqual(notes.uploads.map(\.threadID), ["direct:atlas"])
        XCTAssertEqual(notes.uploads.first?.audio, recorder.recording)
        XCTAssertEqual(sent.map(\.text), [notes.transcript])
        XCTAssertEqual(sent.first?.voiceNote?.id, "vn_1")
        XCTAssertEqual(sent.first?.role, .owner)
        XCTAssertEqual(composer.state, .idle)
        XCTAssertTrue(composer.levels.isEmpty)
    }

    func testCancelUploadsNothing() async {
        await composer.start(threadID: "direct:atlas")
        composer.cancel()
        XCTAssertEqual(recorder.cancels, 1)
        XCTAssertTrue(notes.uploads.isEmpty)
        XCTAssertEqual(composer.state, .idle)
    }

    func testMicOffSaysWhyAndRecordsNothing() async {
        recorder.permissionAnswer = .denied("The mic is off.")
        await composer.start(threadID: "direct:atlas")
        XCTAssertEqual(recorder.starts, 0)
        XCTAssertEqual(composer.notice, "The mic is off.")
        XCTAssertEqual(composer.state, .idle)
    }

    func testAFailedUploadSaysSoAndSendsNothing() async {
        notes.fail = true
        await composer.start(threadID: "direct:atlas")
        await composer.send()
        XCTAssertTrue(sent.isEmpty)
        XCTAssertNotNil(composer.notice)
        XCTAssertEqual(composer.state, .idle)
    }

    func testTheRealRecorderNeverOpensTheMicInATestProcess() async {
        let real = SystemVoiceRecorder()
        XCTAssertThrowsError(try real.start())
        XCTAssertFalse(real.hasBuiltRecorder, "no AVAudioRecorder may exist inside a test process")
    }
}

// MARK: - Playing his own voice message back

@MainActor
final class VoiceNotePlayerTests: XCTestCase {
    func testTapPlaysTheRecordingAndTapAgainStops() async {
        let notes = FakeVoiceNotes()
        let clip = FakeClipPlayer()
        let player = VoiceNotePlayer(client: { notes }, player: clip)
        let note = VoiceNoteRef(id: "vn_1", url: "/v1/voice-notes/vn_1")
        await player.toggle(note)
        XCTAssertEqual(clip.played, [Data("recording vn_1".utf8)])
        XCTAssertEqual(player.playing, "vn_1")
        await player.toggle(note)
        XCTAssertNil(player.playing)
        XCTAssertEqual(clip.stops, 1)
        await player.toggle(note)
        XCTAssertEqual(notes.fetches, ["vn_1"], "played again from memory")
        clip.finish()
        XCTAssertNil(player.playing)
    }
}

// MARK: - On the wire

final class SpeechAndVoiceNoteRoutesTests: XCTestCase {
    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    func testSpeechPostsTheWordsAndReturnsTheAudio() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data("ID3 mp3".utf8)
        let audio = try await client(performer).speech(text: "Acme is green.", agent: "atlas")
        XCTAssertEqual(audio, Data("ID3 mp3".utf8))
        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.url?.path, "/v1/speech")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sekret")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: String])
        XCTAssertEqual(body, ["text": "Acme is green.", "agent": "atlas"])
    }

    func testAVoiceNoteUploadsTheRecordingAsTheBody() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(#"""
        {"ok":true,"delivered":false,"transcript":"hello there",
         "message":{"id":"m1","thread_id":"direct:atlas","author":"owner","role":"owner","ts":1.0,
          "text":"hello there","channel":"text","kind":"text",
          "voice_note":{"id":"vn_0123456789abcdef","url":"/v1/voice-notes/vn_0123456789abcdef"}}}
        """#.utf8)
        let sent = try await client(performer).sendVoiceNote(threadID: "direct:atlas", audio: Data("m4a".utf8))
        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.url?.path, "/v1/threads/direct:atlas/voice-notes")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "audio/mp4")
        XCTAssertEqual(request.httpBody, Data("m4a".utf8))
        XCTAssertEqual(sent.transcript, "hello there")
        XCTAssertEqual(sent.message.voiceNote?.id, "vn_0123456789abcdef")
        XCTAssertEqual(sent.message.role, .owner)
    }

    func testPlaybackFetchesTheRecording() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data("m4a".utf8)
        let audio = try await client(performer).voiceNoteAudio(id: "vn_0123456789abcdef")
        XCTAssertEqual(audio, Data("m4a".utf8))
        XCTAssertEqual(performer.paths, ["/v1/voice-notes/vn_0123456789abcdef"])
    }

    func testAMessageWithoutANoteDecodesWithout() throws {
        let data = Data(#"{"id":"m1","author":"owner","role":"owner","text":"typed"}"#.utf8)
        XCTAssertNil(try DeckCoding.decoder.decode(Message.self, from: data).voiceNote)
    }
}
