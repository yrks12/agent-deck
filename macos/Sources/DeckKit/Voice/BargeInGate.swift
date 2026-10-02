import Foundation

/// **Only his voice interrupts the agent — never the agent's own echo.**
///
/// "when not in mute the call gets interrupted all the time". MEASURED on his
/// Mac (built-in speakers into the built-in mic, voice processing on, see
/// `EngineCallAudio`): echo cancellation leaves the agent's voice in the mic
/// at -70..-35 dBFS with rare bursts to -18 dBFS, and OpenAI's server VAD
/// fires `speech_started` on that residue however quiet it is — 6 false
/// barge-ins in 75 s of agent speech (`semantic_vad`: the same 6). Each one
/// cancels the reply mid-word, and the server then answers its own words.
///
/// So while the agent is playing, and for `tailMS` after (the echo reaches the
/// mic ~400 ms after the player finishes), the mic goes out as silence unless
/// `holdMS` of it is clearly louder than the echo floor — then the held
/// `prefixMS` is released so the server hears him from his first syllable, and
/// the server's own VAD takes it from there. Replayed over the recordings:
/// 1 false barge-in left (an echo-canceller burst), his voice detected at the
/// same millisecond as ungated.
///
/// Withheld audio is replaced with zeros, never dropped: the stream keeps its
/// length so the server's clock stays true. 20 ms frames of 24 kHz PCM16.
public struct BargeInGate: Sendable {
    public static let frameBytes = 960
    static let frameMS = 20

    /// Louder than this, and than the echo floor by `marginDB`, is him.
    public var absoluteDB: Float = -40
    public var marginDB: Float = 12
    public var holdMS = 300
    public var tailMS = 800
    public var prefixMS = 500
    /// Open, this much quiet closes it again.
    public var closeMS = 1_000

    public private(set) var isOpen = false
    private var floorDB: Float = -60
    private var loudMS = 0
    private var quietMS = 0
    private var tailLeftMS = 0
    private var partial = Data()
    private var held: [Data] = []

    public init() {}

    /// What to send for this mic chunk. Same total length as what came in,
    /// once the guard is over; up to `prefixMS` later while it holds.
    public mutating func pass(_ chunk: Data, agentPlaying: Bool) -> Data {
        if agentPlaying { tailLeftMS = tailMS }
        guard agentPlaying || tailLeftMS > 0 || !held.isEmpty || !partial.isEmpty else {
            return chunk
        }
        partial.append(chunk)
        var out = Data()
        while partial.count >= Self.frameBytes {
            let frame = partial.prefix(Self.frameBytes)
            partial.removeFirst(Self.frameBytes)
            if !agentPlaying { tailLeftMS -= Self.frameMS }
            guard agentPlaying || tailLeftMS > 0 else {
                // The guard is over: what it still held was echo.
                release(into: &out, asSilence: true)
                out.append(frame)
                continue
            }
            judge(Data(frame), into: &out)
        }
        if !agentPlaying, tailLeftMS <= 0 {
            release(into: &out, asSilence: true)
            out.append(partial)
            partial.removeAll()
        }
        return out
    }

    /// A mute, a hang-up: forget everything held.
    public mutating func reset() {
        self = BargeInGate()
    }

    private mutating func judge(_ frame: Data, into out: inout Data) {
        let db = Self.decibels(frame)
        let loud = db >= max(absoluteDB, floorDB + marginDB)
        loudMS = loud ? loudMS + Self.frameMS : max(0, loudMS - Self.frameMS)
        if isOpen {
            quietMS = loud ? 0 : quietMS + Self.frameMS
            if quietMS < closeMS {
                out.append(frame)
                return
            }
            isOpen = false
            loudMS = 0
            VoiceTrace.note("call.barge_gate.closed")
        } else {
            // The echo floor follows the room while the gate is shut.
            floorDB = 0.95 * floorDB + 0.05 * max(db, -90)
        }
        held.append(frame)
        if loudMS >= holdMS {
            isOpen = true
            quietMS = 0
            VoiceTrace.note("call.barge_gate.open", "db=\(Int(db)) floor=\(Int(floorDB))")
            release(into: &out, asSilence: false)
        } else if held.count > prefixMS / Self.frameMS {
            held.removeFirst()
            out.append(Data(count: Self.frameBytes))
        }
    }

    private mutating func release(into out: inout Data, asSilence: Bool) {
        if asSilence {
            out.append(Data(count: held.reduce(0) { $0 + $1.count }))
            isOpen = false
            loudMS = 0
        } else {
            for frame in held { out.append(frame) }
        }
        held.removeAll()
    }

    static func decibels(_ pcm16: Data) -> Float {
        let count = pcm16.count / 2
        guard count > 0 else { return -120 }
        var sum: Double = 0
        pcm16.withUnsafeBytes { raw in
            for i in 0..<count {
                let sample = Double(Int16(bitPattern: UInt16(raw[i * 2]) | UInt16(raw[i * 2 + 1]) << 8))
                sum += sample * sample
            }
        }
        let rms = (sum / Double(count)).squareRoot() / Double(Int16.max)
        return rms > 0 ? Float(20 * log10(rms)) : -120
    }
}
