import XCTest
import AVFoundation
@testable import DeckKit

/// **"the voice you did not working" — why not one word reached the deck.**
///
/// MEASURED on the owner's Mac, with a probe signed as his app:
///
///     tap format with voice processing on: 9 ch, 48000 Hz, Float32, deinterleaved
///     recogniser fed a spoken sentence as 1 ch  → "Hello agent please check the …"
///     the SAME sentence as 9 ch, voice in ch 0  → kAFAssistantErrorDomain 1110
///                                                  "No speech detected"
///
/// `SystemSpeechInput` switched voice processing on and appended the tap's
/// buffers as they came, so the recogniser heard nothing, `finish()` returned
/// "", and `deliver("")` quietly settled — no voice message was ever posted.
/// His running app's log shows exactly that: a 42-second call with the engine
/// running and not a single voice POST, and nine hold-to-talk presses that
/// each ended with an empty transcript.
@MainActor
final class VoiceMicFeedTests: XCTestCase {

    private func nineChannelBuffer(voice: [Float]) throws -> AVAudioPCMBuffer {
        let layout = try XCTUnwrap(AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 9))
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channelLayout: layout)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(voice.count)))
        buffer.frameLength = AVAudioFrameCount(voice.count)
        let channels = try XCTUnwrap(buffer.floatChannelData)
        for c in 0..<9 {
            for i in 0..<voice.count { channels[c][i] = c == 0 ? voice[i] : Float((i * 7 + c) % 13) / 100 }
        }
        return buffer
    }

    // MARK: the detector

    /// What the recogniser is handed is one channel, and it is the voice.
    func testAVoiceProcessingBufferReachesTheRecogniserAsChannelZeroMono() throws {
        let voice = (0..<480).map { Float(sin(Double($0) / 8)) * 0.4 }
        let mono = try XCTUnwrap(SpeechFeed.mono(try nineChannelBuffer(voice: voice)))
        XCTAssertEqual(mono.format.channelCount, 1, "nine channels in means 'No speech detected' out")
        XCTAssertEqual(mono.format.sampleRate, 48_000)
        XCTAssertEqual(Int(mono.frameLength), voice.count)
        let out = Array(UnsafeBufferPointer(start: try XCTUnwrap(mono.floatChannelData)[0], count: voice.count))
        XCTAssertEqual(out, voice, "channel 0 carries the processed voice; nothing else may be mixed in")
    }

    /// And the live mic path really goes through it. A source rule, because the
    /// tap only runs against a real microphone: the regression is someone
    /// appending the tap's buffer straight to the request again.
    func testTheLiveMicPathNeverAppendsTheRawTapBuffer() throws {
        for file in ["Sources/DeckKit/Voice/SpeechInput.swift", "Sources/DeckKit/Voice/CallAudio.swift"] {
            let source = try Self.source(file)
            XCTAssertFalse(source.contains("request.append(buffer)"),
                           "\(file) feeds the recogniser the raw (9-channel) tap buffer")
            XCTAssertTrue(source.contains("SpeechFeed.mono(buffer)"),
                          "\(file) must reduce the tap to channel 0 before anyone hears it")
        }
    }

    /// MEASURED with a probe signed as his app: voice processing switched on
    /// before the engine built its output side fails `start()` with -10875
    /// (`PerformCommand(*outputNode, kAUInitialize)`) — a phone call with no
    /// audio at all. Touching the mixer and output first starts cleanly.
    func testThePhoneCallBuildsItsOutputBeforeVoiceProcessing() throws {
        let source = try Self.source("Sources/DeckKit/Voice/CallAudio.swift")
        let mixer = try XCTUnwrap(source.range(of: "_ = engine.mainMixerNode"))
        let vp = try XCTUnwrap(source.range(of: "setVoiceProcessingEnabled(true)"))
        XCTAssertLessThan(mixer.lowerBound, vp.lowerBound)
    }

    func testAMonoBufferPassesThroughUntouched() throws {
        let format = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000,
                                                 channels: 1, interleaved: false))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16))
        buffer.frameLength = 16
        XCTAssertTrue(SpeechFeed.mono(buffer) === buffer)
    }

    // MARK: diagnosable next time

    /// The first build logged nothing he could read — os_log redacts dynamic
    /// strings unless they are marked public.
    func testEveryVoiceLineIsPublicInTheSystemLog() throws {
        let source = try Self.source("Sources/DeckKit/Voice/VoiceTrace.swift")
        XCTAssertTrue(source.contains("privacy: .public"))
        XCTAssertFalse(source.contains("privacy: .private"))
    }

    func testAPushToTalkTurnLeavesATraceOfEveryStep() async {
        VoiceTrace.reset()
        let log = VoiceLog()
        let input = FakeSpeechInput(log: log)
        let session = VoiceSession(input: input, output: FakeSpeechOutput(log: log), calls: { FakeCalls(log: log) })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        await session.pressMic()
        input.say("status of acme")
        await session.releaseMic()
        let trace = VoiceTrace.recent.joined(separator: "\n")
        XCTAssertTrue(trace.contains("session.state idle->listening"), trace)
        XCTAssertTrue(trace.contains("call.started call=call_1"), trace)
        XCTAssertTrue(trace.contains("session.sent call=call_1"), trace)
        XCTAssertFalse(trace.contains("status of acme"), "his words stay out of the system log")
    }

    func testACallThatCannotStartLeavesATrace() async {
        VoiceTrace.reset()
        let log = VoiceLog()
        let calls = FakeCalls(log: log)
        calls.failStart = true
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { calls })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        await session.startCall()
        XCTAssertTrue(VoiceTrace.recent.contains { $0.hasPrefix("call.start_failed") }, "\(VoiceTrace.recent)")
    }

    // MARK: the hold that heard nothing says so

    func testAHoldThatHeardNothingSaysSoInsteadOfDoingNothing() async {
        let log = VoiceLog()
        let calls = FakeCalls(log: log)
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { calls })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        await session.pressMic()
        await session.releaseMic()
        XCTAssertTrue(calls.sent.isEmpty)
        XCTAssertEqual(session.notice?.contains("didn't catch anything"), true, session.notice ?? "no notice")
    }

    /// Echo cancellation cost ~1 s of main-thread set-up per press (measured:
    /// "Timeout waiting for streams" on every hold). A hold has no speaker to
    /// cancel; an open-mic call does.
    func testOnlyAnOpenMicCallPaysForEchoCancellation() async {
        let log = VoiceLog()
        let input = FakeSpeechInput(log: log)
        let session = VoiceSession(input: input, output: FakeSpeechOutput(log: log), calls: { FakeCalls(log: log) })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        await session.pressMic()
        input.say("hi")
        await session.releaseMic()
        await session.hangUp()
        await session.startCall()
        XCTAssertEqual(input.echoAtStart, [false, true])
    }

    // MARK: levels that stop when the sound does

    func testTheLevelGateSendsOneZeroAtSilenceThenNothing() {
        var gate = LevelGate()
        XCTAssertEqual(gate.offer(0.5, at: 0), 0.5)
        XCTAssertNil(gate.offer(0.6, at: 0.01), "faster than ~15 a second is skipped")
        XCTAssertEqual(gate.offer(0.6, at: 0.1), 0.6)
        XCTAssertEqual(gate.offer(0.0, at: 0.2), 0)
        for step in 3..<50 { XCTAssertNil(gate.offer(0.01, at: Double(step) / 10), "silence must not keep publishing") }
        XCTAssertEqual(gate.offer(0.4, at: 6), 0.4)
    }

    func testTheMicLevelReachesTheSession() {
        let log = VoiceLog()
        let input = FakeSpeechInput(log: log)
        let session = VoiceSession(input: input, output: FakeSpeechOutput(log: log), calls: { nil })
        input.onLevel?(0.7)
        XCTAssertEqual(session.micLevel, 0.7)
    }

    func testPCM16RoundTripsAndLevelsReadSensibly() {
        let samples: [Float] = [0, 0.5, -0.5, 1, -1]
        let back = PCM16.decode(PCM16.encode(samples))
        for (a, b) in zip(samples, back) { XCTAssertEqual(a, b, accuracy: 0.001) }
        XCTAssertEqual(PCM16.encode(samples).count, 10)
        XCTAssertEqual(AudioLevel.of([Float](repeating: 0, count: 100)), 0)
        XCTAssertGreaterThan(AudioLevel.of([Float](repeating: 0.1, count: 100)), 0.5)
    }

    static func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }
}
