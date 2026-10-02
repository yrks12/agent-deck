import XCTest
@testable import DeckKit

/// "when not in mute the call gets interrupted all the time".
///
/// MEASURED on his Mac (built-in speakers into the built-in mic, voice
/// processing on): the echo-cancelled mic still carries the agent's own voice
/// at -70..-35 dBFS, with rare bursts to -18 dBFS, and OpenAI's server VAD
/// fires `speech_started` on it — 6 false barge-ins in 75 s of agent speech.
/// `semantic_vad` fired on the same audio just as often. The gate below,
/// replayed over the same recordings, left 1 (an AEC burst) and still let a
/// real voice through at the same millisecond.
final class BargeInGateTests: XCTestCase {
    /// 20 ms of 24 kHz PCM16 at a given loudness (a sine, so RMS is exact).
    private func frame(db: Float, ms: Int = 20) -> Data {
        let count = 24 * ms
        let amplitude = powf(10, db / 20) * 2.0.squareRoot().float
        let samples = (0..<count).map { amplitude * sinf(Float($0) * 2 * .pi * 220 / 24_000) }
        return PCM16.encode(samples)
    }

    private func feed(_ gate: inout BargeInGate, db: Float, ms: Int, playing: Bool) -> Data {
        var out = Data()
        for _ in 0..<(ms / 20) { out.append(gate.pass(frame(db: db), agentPlaying: playing)) }
        return out
    }

    private func isSilence(_ data: Data) -> Bool { data.allSatisfy { $0 == 0 } }

    func testWithTheAgentQuietEverythingGoesThroughUntouched() {
        var gate = BargeInGate()
        let chunk = frame(db: -60, ms: 21)
        XCTAssertEqual(gate.pass(chunk, agentPlaying: false), chunk)
    }

    func testTheAgentsOwnEchoNeverReachesTheServer() {
        var gate = BargeInGate()
        // Echo residual as measured: quiet, with 200 ms bumps to -36 dBFS.
        var out = Data()
        for _ in 0..<10 {
            out.append(feed(&gate, db: -62, ms: 800, playing: true))
            out.append(feed(&gate, db: -36, ms: 200, playing: true))
        }
        XCTAssertFalse(out.isEmpty, "the server's clock keeps running: silence goes out, not nothing")
        XCTAssertTrue(isSilence(out), "echo during playback goes out as silence")
        XCTAssertFalse(gate.isOpen)
    }

    func testHisVoiceOverTheAgentGetsThroughWithItsStart() {
        var gate = BargeInGate()
        var out = feed(&gate, db: -65, ms: 1_000, playing: true)
        out.append(feed(&gate, db: -24, ms: 400, playing: true))
        out.append(feed(&gate, db: -60, ms: 100, playing: true))
        XCTAssertTrue(gate.isOpen, "300 ms of him above the echo floor is him")
        // Everything he said is in the stream, onset included, in order.
        let his = frame(db: -24)
        let occurrences = stride(from: 0, to: out.count - his.count + 1, by: his.count)
            .filter { out.subdata(in: $0..<($0 + his.count)) == his }.count
        XCTAssertEqual(occurrences, 20, "all 400 ms of his voice, including the 300 ms that opened the gate")
    }

    func testTheStreamKeepsItsLengthSoTheServersClockStaysTrue() {
        var gate = BargeInGate()
        var out = feed(&gate, db: -65, ms: 1_000, playing: true)
        out.append(feed(&gate, db: -24, ms: 400, playing: true))
        out.append(feed(&gate, db: -65, ms: 2_000, playing: false))
        XCTAssertEqual(out.count, 24 * 2 * 3_400, "withheld audio is replaced, never dropped or duplicated")
    }

    func testAShortBurstIsNotABargeIn() {
        var gate = BargeInGate()
        let out = feed(&gate, db: -20, ms: 200, playing: true) + feed(&gate, db: -65, ms: 1_000, playing: true)
        XCTAssertFalse(gate.isOpen)
        XCTAssertTrue(isSilence(out))
    }

    func testTheSpeakersTailIsStillEcho() {
        // MEASURED: the echo reaches the mic ~400 ms after the player says it
        // is done. A server that hears that tail answers the agent's own words.
        var gate = BargeInGate()
        _ = feed(&gate, db: -65, ms: 500, playing: true)
        let tail = feed(&gate, db: -45, ms: 700, playing: false)
        XCTAssertTrue(isSilence(tail), "within the tail the echo is still withheld")
        let after = feed(&gate, db: -45, ms: 200, playing: false)
        XCTAssertFalse(isSilence(after), "past the tail the mic is his again")
    }

    func testTheGateClosesAgainAfterASecondOfQuiet() {
        var gate = BargeInGate()
        _ = feed(&gate, db: -65, ms: 500, playing: true)
        _ = feed(&gate, db: -24, ms: 400, playing: true)
        XCTAssertTrue(gate.isOpen)
        _ = feed(&gate, db: -65, ms: 1_100, playing: true)
        XCTAssertFalse(gate.isOpen, "an AEC burst that opened it does not leave the echo flowing")
    }

    func testTheEchoFloorRisesWithALouderRoom() {
        // A louder echo floor raises the bar: -38 dBFS steady is the room,
        // not him.
        var gate = BargeInGate()
        let out = feed(&gate, db: -38, ms: 2_000, playing: true)
        XCTAssertFalse(gate.isOpen)
        XCTAssertTrue(isSilence(out.suffix(24 * 2 * 1_000)))
    }

    func testOddSizedTapChunksAreFramedWithoutLoss() {
        var gate = BargeInGate()
        let chunk = frame(db: -65, ms: 21) + Data([0, 0]) // 1 010 bytes, like the tap's
        var total = 0
        for _ in 0..<50 { total += gate.pass(chunk, agentPlaying: true).count }
        // A second of quiet after the agent: past the tail, everything is out.
        for _ in 0..<50 { total += gate.pass(chunk, agentPlaying: false).count }
        XCTAssertEqual(total, chunk.count * 100)
    }

    func testResetForgetsHeldAudio() {
        var gate = BargeInGate()
        _ = feed(&gate, db: -24, ms: 200, playing: true)
        gate.reset()
        XCTAssertFalse(gate.isOpen)
        XCTAssertEqual(gate.pass(frame(db: -60), agentPlaying: false), frame(db: -60))
    }
}

private extension Double {
    var float: Float { Float(self) }
}
