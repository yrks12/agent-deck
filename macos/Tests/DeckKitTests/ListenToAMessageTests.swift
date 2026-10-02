import XCTest
@testable import DeckKit

/// Owner, 2026-10-01: "On messages from the AI I want to be able to listen and
/// control the speed, with our voice."
///
/// - Every agent bubble has a Listen control that plays the message in the
///   desk's CALL voice from the deck (`POST /v1/speech`); Apple's voice only
///   when the deck cannot.
/// - A strip while it plays: play/pause, a scrubber, a speed that cycles
///   1x, 1.25x, 1.5x, 1.75x, 2x, 0.75x and is remembered on this device.
/// - A long message is made speakable (no code, paths, links) and cut into
///   pieces the deck will accept (it keeps the first 1500 characters).
/// - Starting a call or a voice message stops it; "Spoken replies" is a
///   different switch and is not touched.
///
/// Nothing here makes a sound: the audio output is a fake, and the real one is
/// pinned silent by `AudioGate`.

// MARK: - Fake

@MainActor
final class FakeListenAudio: ListenAudio {
    var onFinish: (() -> Void)?
    var position: TimeInterval = 0
    var duration: TimeInterval = 60
    private(set) var played: [(audio: Data, rate: Float)] = []
    private(set) var rates: [Float] = []
    private(set) var pauses = 0, resumes = 0, stops = 0
    private(set) var seeks: [TimeInterval] = []

    func play(_ audio: Data, rate: Float) throws { played.append((audio, rate)) }
    func pause() { pauses += 1 }
    func resume() { resumes += 1 }
    func stop() { stops += 1 }
    func setRate(_ rate: Float) { rates.append(rate) }
    func seek(to seconds: TimeInterval) { seeks.append(seconds); position = seconds }
    func finish() { onFinish?() }
}

private func agentMessage(_ id: String, _ text: String, author: String = "atlas",
                          role: MessageRole = .agent) -> Message {
    Message(id: id, cursor: id, threadID: "direct:\(author)", author: author, role: role,
            sentAt: Date(), text: text)
}

private func freshDefaults() -> UserDefaults {
    let name = "listen-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: name)!
    defaults.removePersistentDomain(forName: name)
    return defaults
}

// MARK: - Speed

final class ListenSpeedTests: XCTestCase {
    func testTheSpeedCyclesInTheOrderTheOwnerNamedAndWrapsToNormal() {
        var speed = 1.0
        var seen: [Double] = []
        for _ in 0..<6 { speed = ListenSpeed.next(after: speed); seen.append(speed) }
        XCTAssertEqual(seen, [1.25, 1.5, 1.75, 2.0, 0.75, 1.0])
    }

    func testAnUnknownStoredSpeedStartsTheCycleFromNormal() {
        XCTAssertEqual(ListenSpeed.next(after: 3.3), 1.0)
    }

    func testLabelsAreShortAndReadable() {
        XCTAssertEqual(ListenSpeed.label(1.0), "1×")
        XCTAssertEqual(ListenSpeed.label(1.25), "1.25×")
        XCTAssertEqual(ListenSpeed.label(1.5), "1.5×")
        XCTAssertEqual(ListenSpeed.label(2.0), "2×")
        XCTAssertEqual(ListenSpeed.label(0.75), "0.75×")
    }

    func testTheSpeedIsRememberedOnThisDevice() {
        let defaults = freshDefaults()
        XCTAssertEqual(ListenSpeed.stored(in: defaults), 1.0, "normal speed until he changes it")
        ListenSpeed.store(1.75, in: defaults)
        XCTAssertEqual(ListenSpeed.stored(in: defaults), 1.75)
        XCTAssertEqual(defaults.object(forKey: ListenSpeed.key) as? Double, 1.75)
    }

    func testAGarbageStoredSpeedFallsBackToNormal() {
        let defaults = freshDefaults()
        defaults.set(9.0, forKey: ListenSpeed.key)
        XCTAssertEqual(ListenSpeed.stored(in: defaults), 1.0)
    }
}

// MARK: - What is spoken

final class ListenSpeakableTests: XCTestCase {
    func testTheWholeMessageIsSpokenNotJustTheFirstTwoSentences() {
        let raw = "One is first. Two is second. Three is third. Four is fourth."
        XCTAssertEqual(Speakable.listenChunks(raw).joined(separator: " "),
                       "One is first. Two is second. Three is third. Four is fourth.")
    }

    func testCodeLinksAndPathsAreNotRead() {
        let raw = """
        I fixed **the bug** in `~/Projects/acme/server/app.py`.

        ```swift
        let secret = 42
        ```

        See [the PR](https://github.com/x/y/pull/1) when you can.
        """
        let spoken = Speakable.listenChunks(raw).joined(separator: " ")
        XCTAssertFalse(spoken.contains("secret"), spoken)
        XCTAssertFalse(spoken.contains("github"), spoken)
        XCTAssertFalse(spoken.contains("app.py"), spoken)
        XCTAssertFalse(spoken.contains("**"), spoken)
        XCTAssertTrue(spoken.contains("I fixed the bug"), spoken)
        XCTAssertTrue(spoken.contains("the PR"), spoken)
    }

    func testAMessageThatIsOnlyCodeHasNothingToSay() {
        XCTAssertEqual(Speakable.listenChunks("```\nrm -rf build\n```"), [])
    }

    func testALongMessageIsCutIntoPiecesTheDeckWillAcceptAtSentenceEnds() {
        let sentence = "This sentence is exactly fifty characters long ok."   // 50
        XCTAssertEqual(sentence.count, 50)
        let raw = Array(repeating: sentence, count: 90).joined(separator: " ")  // ~4500
        let chunks = Speakable.listenChunks(raw, limit: 1200)
        XCTAssertGreaterThan(chunks.count, 3)
        for chunk in chunks {
            XCTAssertLessThanOrEqual(chunk.count, 1200)
            XCTAssertTrue(chunk.hasSuffix("ok."), "cut mid-sentence: …\(chunk.suffix(20))")
        }
        XCTAssertEqual(chunks.joined(separator: " "), raw, "nothing is lost between pieces")
    }

    func testASingleSentenceLongerThanTheLimitIsStillCutAndNothingIsLost() {
        let raw = Array(repeating: "word", count: 600).joined(separator: " ") + "."
        let chunks = Speakable.listenChunks(raw, limit: 500)
        XCTAssertGreaterThan(chunks.count, 1)
        XCTAssertTrue(chunks.allSatisfy { $0.count <= 500 })
        XCTAssertEqual(chunks.joined(separator: " ").replacingOccurrences(of: ".", with: ""),
                       raw.replacingOccurrences(of: ".", with: ""))
    }

    func testTheDefaultLimitIsUnderWhatTheDeckKeeps() {
        XCTAssertLessThan(Speakable.listenChunkLimit, 1500)
    }
}

// MARK: - The player

@MainActor
final class ListenPlayerTests: XCTestCase {
    private var client: FakeSpeechClient!
    private var audio: FakeListenAudio!
    private var fallback: FakeSpeechOutput!
    private var defaults: UserDefaults!
    private var player: ListenPlayer!

    override func setUp() async throws {
        client = FakeSpeechClient()
        audio = FakeListenAudio()
        fallback = FakeSpeechOutput(log: VoiceLog())
        defaults = freshDefaults()
        player = makePlayer()
    }

    private func makePlayer(hasDeck: Bool = true) -> ListenPlayer {
        ListenPlayer(client: { [client] in hasDeck ? client : nil }, fallback: fallback,
                     audio: audio, defaults: defaults)
    }

    private func listen(_ message: Message) async {
        player.toggle(message)
        await player.pending?.value
    }

    func testListenFetchesTheDeckVoiceForTheDeskAndPlaysIt() async {
        await listen(agentMessage("m1", "The deploy is done.", author: "atlas"))
        XCTAssertEqual(client.asked.map(\.text), ["The deploy is done."])
        XCTAssertEqual(client.asked.map(\.agent), ["atlas"], "the desk's own voice")
        XCTAssertEqual(audio.played.count, 1)
        XCTAssertEqual(player.state, .playing("m1"))
        XCTAssertTrue(fallback.spoken.isEmpty, "Apple's voice is only for when the deck cannot")
    }

    func testItShowsLoadingWhileTheDeckIsStillMakingTheAudio() {
        player.toggle(agentMessage("m1", "Hello there."))
        XCTAssertEqual(player.state, .loading("m1"))
    }

    func testAPieceIsSentPerChunkAndPlayedAsOneClip() async {
        // Distinct sentences: an identical piece would come from memory.
        let long = (100..<190).map { "Sentence number \($0) of the long report is here ok." }.joined(separator: " ")
        await listen(agentMessage("m1", long))
        XCTAssertGreaterThan(client.asked.count, 3)
        XCTAssertTrue(client.asked.allSatisfy { $0.text.count < 1500 })
        XCTAssertEqual(audio.played.count, 1, "one clip so the scrubber spans the whole message")
        XCTAssertEqual(audio.played[0].audio.count, client.audio.count * client.asked.count)
    }

    func testItPlaysAtTheRememberedSpeed() async {
        player.cycleSpeed()                                    // 1.25
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(audio.played.first?.rate, 1.25)
    }

    func testCyclingWhilePlayingChangesTheRateAtOnceAndRemembersIt() async {
        await listen(agentMessage("m1", "Hello there."))
        player.cycleSpeed()
        player.cycleSpeed()
        XCTAssertEqual(player.speed, 1.5)
        XCTAssertEqual(audio.rates.last, 1.5)
        XCTAssertEqual(ListenSpeed.stored(in: defaults), 1.5)
        XCTAssertEqual(makePlayer().speed, 1.5, "a new launch starts where he left it")
    }

    func testTapOnThePlayingMessagePausesThenResumes() async {
        let message = agentMessage("m1", "Hello there.")
        await listen(message)
        player.toggle(message)
        XCTAssertEqual(player.state, .paused("m1"))
        XCTAssertEqual(audio.pauses, 1)
        player.toggle(message)
        XCTAssertEqual(player.state, .playing("m1"))
        XCTAssertEqual(audio.resumes, 1)
    }

    func testStartingAnotherMessageReplacesTheFirst() async {
        await listen(agentMessage("m1", "First one."))
        await listen(agentMessage("m2", "Second one."))
        XCTAssertEqual(player.state, .playing("m2"))
        XCTAssertGreaterThanOrEqual(audio.stops, 1)
        XCTAssertEqual(audio.played.count, 2)
    }

    func testStopGoesIdleAndSilencesTheAudio() async {
        await listen(agentMessage("m1", "Hello there."))
        player.stop()
        XCTAssertEqual(player.state, .idle)
        XCTAssertGreaterThanOrEqual(audio.stops, 1)
    }

    func testStoppingWhileLoadingNeverPlaysTheLateAudio() async {
        player.toggle(agentMessage("m1", "Hello there."))
        player.stop()
        await player.pending?.value
        XCTAssertTrue(audio.played.isEmpty)
        XCTAssertEqual(player.state, .idle)
    }

    func testTheScrubberReportsProgressAndSeeks() async {
        await listen(agentMessage("m1", "Hello there."))
        audio.position = 15; audio.duration = 60
        player.tick()
        XCTAssertEqual(player.progress, 0.25, accuracy: 0.001)
        player.seek(toFraction: 0.5)
        XCTAssertEqual(audio.seeks, [30])
        XCTAssertEqual(player.progress, 0.5, accuracy: 0.001)
    }

    func testSeekIsClampedToTheClip() async {
        await listen(agentMessage("m1", "Hello there."))
        player.seek(toFraction: 4)
        XCTAssertEqual(audio.seeks.last, 60)
        player.seek(toFraction: -1)
        XCTAssertEqual(audio.seeks.last, 0)
    }

    func testReachingTheEndGoesIdle() async {
        await listen(agentMessage("m1", "Hello there."))
        audio.finish()
        XCTAssertEqual(player.state, .idle)
    }

    func testWhenTheDeckCannotApplesVoiceReadsIt() async {
        client.fail = true
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(fallback.spoken.map(\.text), ["Hello there."])
        XCTAssertTrue(audio.played.isEmpty)
        XCTAssertEqual(player.state, .playing("m1"))
        XCTAssertTrue(player.isFallback)
        fallback.finishCurrent()
        XCTAssertEqual(player.state, .idle)
    }

    func testWithNoDeckApplesVoiceReadsIt() {
        player = makePlayer(hasDeck: false)
        player.toggle(agentMessage("m1", "Hello there."))
        XCTAssertEqual(fallback.spoken.map(\.text), ["Hello there."])
    }

    func testAMessageWithNothingSpeakableSaysSoAndPlaysNothing() async {
        await listen(agentMessage("m1", "```\nls -la\n```"))
        XCTAssertEqual(player.state, .idle)
        XCTAssertNotNil(player.notice)
        XCTAssertTrue(client.asked.isEmpty)
    }

    func testAMessageIsFetchedOnceAndReplayedFromMemory() async {
        let message = agentMessage("m1", "Hello there.")
        await listen(message)
        audio.finish()
        await listen(message)
        XCTAssertEqual(client.asked.count, 1)
        XCTAssertEqual(audio.played.count, 2)
    }

    // Interplay

    func testAStartedCallOrVoiceMessageStopsPlayback() async {
        await listen(agentMessage("m1", "Hello there."))
        player.voiceBusy(true)
        XCTAssertEqual(player.state, .idle)
        XCTAssertGreaterThanOrEqual(audio.stops, 1)
    }

    func testNothingStartsWhileACallOrRecordingIsOn() async {
        player.voiceBusy(true)
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(player.state, .idle)
        XCTAssertTrue(client.asked.isEmpty)
        XCTAssertNotNil(player.notice)
        player.voiceBusy(false)
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(player.state, .playing("m1"))
    }

    func testListeningNeverTouchesTheSpokenRepliesSwitch() async {
        SpokenReplies.set(false, in: defaults)
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(player.state, .playing("m1"), "a tap on Listen is explicit: it plays even with replies off")
        XCTAssertFalse(SpokenReplies.isOn(in: defaults))
    }

    func testStartingTellsTheRepliesSpeakerToBeQuiet() async {
        var told = 0
        player.onStart = { told += 1 }
        await listen(agentMessage("m1", "Hello there."))
        XCTAssertEqual(told, 1)
    }

    // Play next

    func testPlayNextContinuesWithTheNextAgentMessageAndSkipsHis() async {
        player.messages = [agentMessage("m1", "First."), agentMessage("u1", "Mine.", author: "owner", role: .owner),
                           agentMessage("m2", "Second."), agentMessage("m3", "Third.")]
        player.setPlayNext(true)
        await listen(player.messages[0])
        audio.finish()
        await player.pending?.value
        XCTAssertEqual(player.state, .playing("m2"))
        audio.finish()
        await player.pending?.value
        XCTAssertEqual(player.state, .playing("m3"))
        audio.finish()
        XCTAssertEqual(player.state, .idle, "nothing after the last message")
    }

    func testWithoutPlayNextItStopsAtTheEndOfTheMessage() async {
        player.messages = [agentMessage("m1", "First."), agentMessage("m2", "Second.")]
        await listen(player.messages[0])
        audio.finish()
        XCTAssertEqual(player.state, .idle)
        XCTAssertEqual(audio.played.count, 1)
    }

    func testPlayNextIsRememberedOnThisDevice() {
        player.setPlayNext(true)
        XCTAssertTrue(makePlayer().playNext)
    }
}

// MARK: - Which messages get the control

@MainActor
final class ListenControlIsOnAgentMessagesTests: XCTestCase {
    func testOnlyAnAgentsTextMessageWithWordsCanBeListenedTo() {
        XCTAssertTrue(ListenPlayer.canListen(agentMessage("a", "Hello there.")))
        XCTAssertFalse(ListenPlayer.canListen(agentMessage("b", "Mine.", author: "owner", role: .owner)))
        XCTAssertFalse(ListenPlayer.canListen(agentMessage("d", "  ")))
        // Cheap on purpose: it runs for every bubble on every draw. A reply
        // that is only code shows the control and says so when tapped.
    }
}
