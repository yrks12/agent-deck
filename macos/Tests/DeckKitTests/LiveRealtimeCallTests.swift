import XCTest
import AVFoundation
@testable import DeckKit

/// **One real phone call, end to end, without a microphone.** Skipped unless
/// `DECK_LIVE_URL`, `DECK_LIVE_TOKEN` and `DECK_LIVE_SPEECH` (a 24 kHz mono
/// WAV of a spoken request) are set. It dials the deck's real `POST /v1/calls`
/// for the `wake-probe` desk, talks to OpenAI's Realtime API over the real
/// socket, and requires: the voice answered out loud, it called
/// `send_to_desk`, the request landed in the desk's thread, and the call was
/// ended on the deck.
///
///     say -o /tmp/ask.wav --file-format=WAVE --data-format=LEI16@24000 \
///       "Please ask the desk to reply with the single word pong."
///     DECK_LIVE_URL=http://10.99.0.1:7789 DECK_LIVE_TOKEN=… DECK_LIVE_SPEECH=/tmp/ask.wav \
///       swift test --filter LiveRealtimeCallTests
@MainActor
final class LiveRealtimeCallTests: XCTestCase {

    /// Plays a recorded request into the call as if he said it, then silence
    /// so the server's VAD hears him finish; keeps what the agent says.
    final class RecordedCallAudio: CallAudio {
        var onMicChunk: ((Data) -> Void)?
        var onMicLevel: ((Float) -> Void)?
        var onOutputLevel: ((Float) -> Void)?
        let speech: Data
        private(set) var playedBytes = 0
        private(set) var flushes = 0
        private var feeder: Task<Void, Never>?
        var isPlaying: Bool { false }

        init(speech: Data) { self.speech = speech }

        func start() throws {
            let speech = self.speech
            feeder = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                let chunk = 4_800   // 100 ms
                var offset = 0
                let silence = Data(count: 24_000 * 2 * 3)
                let all = speech + silence
                while offset < all.count, !Task.isCancelled {
                    let end = min(offset + chunk, all.count)
                    self?.onMicChunk?(all.subdata(in: offset..<end))
                    offset = end
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
            }
        }

        func play(_ pcm16: Data) { playedBytes += pcm16.count }
        func flushPlayback() { flushes += 1 }
        func stop() { feeder?.cancel() }
    }

    func testARealPhoneCallReachesTheDeskAndTalksBack() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let raw = env["DECK_LIVE_URL"], let base = URL(string: raw), let token = env["DECK_LIVE_TOKEN"],
              let wav = env["DECK_LIVE_SPEECH"] else {
            throw XCTSkip("set DECK_LIVE_URL, DECK_LIVE_TOKEN and DECK_LIVE_SPEECH for a live call")
        }
        let file = try AVAudioFile(forReading: URL(fileURLWithPath: wav))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                                    frameCapacity: AVAudioFrameCount(file.length)))
        try file.read(into: buffer)
        XCTAssertEqual(file.processingFormat.sampleRate, 24_000)
        let speech = PCM16.encode(try XCTUnwrap(buffer.floatChannelData)[0], count: Int(buffer.frameLength))

        VoiceTrace.reset()
        let client = HTTPDeckClient(baseURL: base, tokens: InMemoryTokenStore(token: token))
        let audio = RecordedCallAudio(speech: speech)
        let log = VoiceLog()
        let session = VoiceSession(
            input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log), calls: { client },
            realtime: { dial in
                RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                    callID: dial.callID, threadID: dial.threadID,
                                    transport: URLSessionRealtimeTransport(), audio: audio, calls: dial.calls)
            })
        session.sync(desk: "wake-probe", displayName: "Wake probe", voice: nil, working: false, messages: [])
        await session.startCall()
        let live = try XCTUnwrap(session.realtime, "no live voice: \(session.notice ?? "")")

        let deadline = Date().addingTimeInterval(60)
        func trace() -> String { VoiceTrace.recent.joined(separator: "\n") }
        while Date() < deadline,
              !(trace().contains("send_to_desk.ok") && audio.playedBytes > 24_000) {
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        let seen = trace()
        let callID = session.call?.id
        await session.hangUp()
        print("LIVE CALL TRACE\n\(seen)\nplayed=\(audio.playedBytes) bytes phase=\(live.phase)")
        XCTAssertTrue(seen.contains("call.realtime.session.created") || seen.contains("call.realtime.session.updated"), seen)
        XCTAssertTrue(seen.contains("call.realtime.send_to_desk.ok"), "the voice never handed the request to the desk\n\(seen)")
        XCTAssertGreaterThan(audio.playedBytes, 24_000, "the voice never answered out loud")
        XCTAssertFalse(seen.contains("call.realtime.error"), seen)
        XCTAssertNotNil(callID)
    }
}
