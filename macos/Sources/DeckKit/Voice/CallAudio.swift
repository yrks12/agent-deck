import Foundation
import AVFoundation

/// **The Mac's side of a phone call**: his voice out as 24 kHz PCM16 chunks,
/// the agent's voice in, and how loud each of them is — for the character on
/// screen. A protocol so the call's rules are tested with a fake.
@MainActor
public protocol CallAudio: AnyObject {
    /// 24 kHz mono PCM16 little-endian, as the Realtime API wants it.
    var onMicChunk: ((Data) -> Void)? { get set }
    /// His loudness 0...1, thinned by `LevelGate`: nothing while he is silent.
    var onMicLevel: ((Float) -> Void)? { get set }
    /// The agent's loudness 0...1 as it plays, 0 once playback runs dry.
    var onOutputLevel: ((Float) -> Void)? { get set }
    func start() throws
    /// Queue one delta of the agent's voice (24 kHz mono PCM16).
    func play(_ pcm16: Data)
    /// Barge-in: silence whatever is queued, now.
    func flushPlayback()
    var isPlaying: Bool { get }
    func stop()
}

/// `AVAudioEngine` with voice processing on the input node, so the agent's
/// voice coming out of the speaker is cancelled before it reaches the mic and
/// is not heard as him (voice processing covers the engine's output too).
@MainActor
public final class EngineCallAudio: CallAudio {
    public var onMicChunk: ((Data) -> Void)?
    public var onMicLevel: ((Float) -> Void)?
    public var onOutputLevel: ((Float) -> Void)?

    private var engine: AVAudioEngine?
    private let player = AVAudioPlayerNode()
    private let playFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: PCM16.realtimeRate,
                                           channels: 1, interleaved: false)!
    private var configObserver: NSObjectProtocol?
    /// Levels of the buffers queued on the player, in play order.
    private var queuedLevels: [Float] = []
    /// Bumped on every flush: a flushed buffer's completion is not news.
    private var epoch = 0
    /// The agent's level to the face at most ~15 times a second, not once per
    /// streamed buffer (dozens a second, each one a redraw of the call screen).
    private var outputGate = LevelGate()

    public init() {}

    public var isPlaying: Bool { !queuedLevels.isEmpty }

    public func start() throws {
        // A test process never opens his mic or plays through his speakers.
        guard !AudioGate.isSilenced else {
            VoiceTrace.note("call.audio.silenced", "test process")
            return
        }
        let engine = AVAudioEngine()
        // MEASURED on his Mac: enabling voice processing before the engine
        // has built its output side fails the start with -10875 (output node
        // kAUInitialize). Touching the mixer and output first makes it start.
        _ = engine.mainMixerNode
        _ = engine.outputNode
        let input = engine.inputNode
        do { try input.setVoiceProcessingEnabled(true) } catch {
            VoiceTrace.fail("call.audio.vp_failed", "\(error)")
        }
        let tapFormat = input.outputFormat(forBus: 0)
        VoiceTrace.note("call.audio.start", "tap=\(tapFormat.channelCount)ch/\(Int(tapFormat.sampleRate))Hz")

        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playFormat)

        let converter = MicConverter(inputRate: tapFormat.sampleRate)
        let levels = LevelRelay(gate: LevelGate()) { [weak self] level in self?.onMicLevel?(level) }
        input.installTap(onBus: 0, bufferSize: 1024, format: tapFormat) { [weak self] buffer, _ in
            guard let mono = SpeechFeed.mono(buffer) else { return }
            if let samples = mono.floatChannelData {
                levels.offer(AudioLevel.of(samples[0], count: Int(mono.frameLength)))
            }
            guard let chunk = converter.pcm16(mono), !chunk.isEmpty else { return }
            Task { @MainActor [weak self] in self?.onMicChunk?(chunk) }
        }
        engine.prepare()
        try engine.start()
        player.play()
        self.engine = engine
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.restartAfterReconfiguration() }
        }
    }

    private func restartAfterReconfiguration() {
        guard let engine, !engine.isRunning else { return }
        VoiceTrace.note("call.audio.config_changed", "restarting")
        do {
            try engine.start()
            player.play()
        } catch {
            VoiceTrace.fail("call.audio.restart_failed", "\(error)")
        }
    }

    #if os(iOS)
    /// A phone call or Siri stopped the engine; the session is back.
    public func resumeAfterInterruption() {
        restartAfterReconfiguration()
    }
    #endif

    public func play(_ pcm16: Data) {
        guard engine != nil else { return }
        let samples = PCM16.decode(pcm16)
        guard !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData else { return }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { channel[0].update(from: $0.baseAddress!, count: samples.count) }
        let level = AudioLevel.of(samples)
        let wasIdle = queuedLevels.isEmpty
        queuedLevels.append(level)
        if wasIdle { emitOutput(level) }
        let mine = epoch
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            Task { @MainActor [weak self] in self?.finishedOne(epoch: mine) }
        }
        if engine?.isRunning == true, !player.isPlaying { player.play() }
    }

    private func finishedOne(epoch mine: Int) {
        guard mine == epoch, !queuedLevels.isEmpty else { return }
        queuedLevels.removeFirst()
        guard let next = queuedLevels.first else {
            // Playback ran dry: always said, it moves the call on to listening.
            _ = outputGate.reset()
            onOutputLevel?(0)
            return
        }
        emitOutput(next)
    }

    private func emitOutput(_ level: Float) {
        if let shown = outputGate.offer(level, at: ProcessInfo.processInfo.systemUptime) { onOutputLevel?(shown) }
    }

    public func flushPlayback() {
        epoch += 1
        queuedLevels.removeAll()
        player.stop()
        if engine?.isRunning == true { player.play() }
        _ = outputGate.reset()
        onOutputLevel?(0)
    }

    public func stop() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        epoch += 1
        queuedLevels.removeAll()
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        engine.stop()
        self.engine = nil
        onMicLevel?(0)
        onOutputLevel?(0)
        VoiceTrace.note("call.audio.stop")
    }
}

/// Mono float at the mic's rate → 24 kHz PCM16, on the audio thread.
final class MicConverter: @unchecked Sendable {
    private let converter: AVAudioConverter?
    private let target: AVAudioFormat
    private let inputRate: Double

    init(inputRate: Double) {
        self.inputRate = inputRate
        let source = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inputRate, channels: 1, interleaved: false)!
        target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: PCM16.realtimeRate, channels: 1, interleaved: false)!
        converter = AVAudioConverter(from: source, to: target)
    }

    func pcm16(_ mono: AVAudioPCMBuffer) -> Data? {
        guard let converter else { return nil }
        let capacity = AVAudioFrameCount(Double(mono.frameLength) * PCM16.realtimeRate / inputRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return mono
        }
        guard error == nil, let samples = out.floatChannelData else { return nil }
        return PCM16.encode(samples[0], count: Int(out.frameLength))
    }
}
