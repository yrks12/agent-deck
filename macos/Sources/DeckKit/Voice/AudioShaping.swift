import Foundation
import AVFoundation

/// **What the microphone hands us, made into what a listener can hear.**
///
/// MEASURED on his Mac (the reason the first voice build never sent a word):
/// with `setVoiceProcessingEnabled(true)` the input node's tap format is
/// `9 ch, 48000 Hz, Float32, deinterleaved` — the processed voice in channel 0
/// and the raw mic array beside it. `SFSpeechAudioBufferRecognitionRequest`
/// fed those 9-channel buffers answers `kAFAssistantErrorDomain 1110 "No
/// speech detected"` for a sentence it transcribes word for word as one
/// channel. So every buffer is reduced to channel 0 before anyone hears it.
public enum SpeechFeed {
    /// Channel 0 as its own mono buffer at the same rate. A mono buffer is
    /// returned as it came.
    public static func mono(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let format = buffer.format
        guard format.channelCount > 1 else { return buffer }
        guard let source = buffer.floatChannelData,
              let monoFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate,
                                             channels: 1, interleaved: false),
              let out = AVAudioPCMBuffer(pcmFormat: monoFormat, frameCapacity: buffer.frameLength),
              let target = out.floatChannelData else { return nil }
        let frames = Int(buffer.frameLength)
        out.frameLength = buffer.frameLength
        if format.isInterleaved {
            let stride = Int(format.channelCount)
            for i in 0..<frames { target[0][i] = source[0][i * stride] }
        } else {
            target[0].update(from: source[0], count: frames)
        }
        return out
    }
}

/// **How loud, as 0...1**, for the rings and the mouth. Root-mean-square in
/// decibels, -50 dB and quieter reading 0 and -10 dB and louder reading 1, so
/// a normal speaking voice lands mid-scale instead of hugging the floor.
public enum AudioLevel {
    public static let floorDB: Float = -50
    public static let ceilingDB: Float = -10

    public static func of(_ samples: UnsafePointer<Float>, count: Int) -> Float {
        guard count > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<count { sum += samples[i] * samples[i] }
        return fromRMS((sum / Float(count)).squareRoot())
    }

    public static func of(_ samples: [Float]) -> Float {
        samples.withUnsafeBufferPointer { ptr in
            guard let base = ptr.baseAddress else { return 0 }
            return of(base, count: ptr.count)
        }
    }

    public static func of(pcm16 data: Data) -> Float {
        of(PCM16.decode(data))
    }

    static func fromRMS(_ rms: Float) -> Float {
        guard rms > 0 else { return 0 }
        let db = 20 * log10(rms)
        return min(1, max(0, (db - floorDB) / (ceilingDB - floorDB)))
    }
}

/// **Level updates that stop when the sound does.** Audio arrives ~47 times a
/// second; the view needs ~15, and nothing at all once it is quiet — a ring
/// that redraws forever at silence is the permanent-animation trap this app
/// has paid for before. `offer` returns the value to publish, or nil to skip.
public struct LevelGate: Sendable {
    public var minInterval: TimeInterval = 1.0 / 15
    public var silence: Float = 0.04
    public private(set) var published: Float = 0
    private var lastAt: TimeInterval = -.infinity

    public init() {}

    public mutating func offer(_ level: Float, at now: TimeInterval) -> Float? {
        let quiet = level < silence
        if quiet {
            // One zero, then nothing until there is sound again.
            guard published != 0 else { return nil }
            published = 0
            lastAt = now
            return 0
        }
        guard now - lastAt >= minInterval, abs(level - published) >= 0.02 else { return nil }
        published = level
        lastAt = now
        return level
    }

    /// Forget the last value (a new call, a mute).
    public mutating func reset() -> Float? {
        guard published != 0 else { return nil }
        published = 0
        return 0
    }
}

/// **The Realtime API's audio: 16-bit little-endian PCM, mono.**
public enum PCM16 {
    public static let realtimeRate: Double = 24_000

    public static func encode(_ samples: UnsafePointer<Float>, count: Int) -> Data {
        var data = Data(count: count * 2)
        data.withUnsafeMutableBytes { raw in
            let out = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let clamped = max(-1, min(1, samples[i]))
                out[i] = Int16(clamped * Float(Int16.max)).littleEndian
            }
        }
        return data
    }

    public static func encode(_ samples: [Float]) -> Data {
        samples.withUnsafeBufferPointer { ptr in
            guard let base = ptr.baseAddress else { return Data() }
            return encode(base, count: ptr.count)
        }
    }

    public static func decode(_ data: Data) -> [Float] {
        let count = data.count / 2
        var out = [Float](repeating: 0, count: count)
        data.withUnsafeBytes { raw in
            for i in 0..<count {
                let lo = UInt16(raw[i * 2]), hi = UInt16(raw[i * 2 + 1])
                out[i] = Float(Int16(bitPattern: lo | (hi << 8))) / Float(Int16.max)
            }
        }
        return out
    }
}

/// Carries level readings from the audio thread to the main actor, thinned by
/// a `LevelGate` so a silent mic sends nothing at all.
final class LevelRelay: @unchecked Sendable {
    private let lock = NSLock()
    private var gate: LevelGate
    private let deliver: @MainActor (Float) -> Void

    init(gate: LevelGate, deliver: @escaping @MainActor (Float) -> Void) {
        self.gate = gate
        self.deliver = deliver
    }

    func offer(_ level: Float) {
        lock.lock()
        let out = gate.offer(level, at: ProcessInfo.processInfo.systemUptime)
        lock.unlock()
        guard let out else { return }
        let deliver = self.deliver
        Task { @MainActor in deliver(out) }
    }
}
