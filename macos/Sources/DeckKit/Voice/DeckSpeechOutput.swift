import Foundation
import AVFoundation

/// `POST /v1/speech`: a line's words -> mp3 in the desk's call voice. The deck
/// holds the OpenAI key; this side only ever sees audio.
public protocol SpeechClient: Sendable {
    func speech(text: String, agent: String) async throws -> Data
}

/// Plays one clip of encoded audio (mp3 or m4a).
@MainActor
public protocol AudioClipPlayer: AnyObject {
    /// The clip reached its end. Not called after `stop()`.
    var onFinish: (() -> Void)? { get set }
    var onLevel: ((Float) -> Void)? { get set }
    func play(_ audio: Data) throws
    func stop()
}

/// `AVAudioPlayer`, built only when there is something to play, and never in
/// a test process (`AudioGate`).
@MainActor
public final class SystemAudioClipPlayer: NSObject, AudioClipPlayer {
    public var onFinish: (() -> Void)?
    public var onLevel: ((Float) -> Void)?
    private var player: AVAudioPlayer?
    private var meter: Timer?
    public private(set) var hasBuiltPlayer = false

    public override init() { super.init() }

    public func play(_ audio: Data) throws {
        stop()
        if AudioGate.isSilenced {
            onFinish?()
            return
        }
        #if os(iOS)
        // Out of a call, play through the speaker even with the ring switch
        // off; on a call the call's own session is left alone.
        let session = AVAudioSession.sharedInstance()
        if session.category != .playAndRecord {
            try? session.setCategory(.playback, mode: .spokenAudio)
            try? session.setActive(true)
        }
        #endif
        let made = try AVAudioPlayer(data: audio)
        hasBuiltPlayer = true
        made.delegate = self
        made.isMeteringEnabled = true
        player = made
        made.play()
        meter = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    public func stop() {
        meter?.invalidate(); meter = nil
        player?.delegate = nil
        player?.stop()
        player = nil
        onLevel?(0)
    }

    private func tick() {
        guard let player else { return }
        player.updateMeters()
        let db = player.averagePower(forChannel: 0)          // -160...0
        onLevel?(max(0, min(1, (db + 50) / 50)))
    }

    fileprivate func ended(_ finished: AVAudioPlayer) {
        guard finished === player else { return }
        meter?.invalidate(); meter = nil
        player = nil
        onLevel?(0)
        onFinish?()
    }
}

extension SystemAudioClipPlayer: AVAudioPlayerDelegate {
    nonisolated public func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let box = ClipBox(player)
        Task { @MainActor in self.ended(box.value) }
    }
}

private struct ClipBox<T>: @unchecked Sendable {
    let value: T
    init(_ value: T) { self.value = value }
}

/// A few dozen spoken lines, newest kept: a replayed reply or the recurring
/// "Asking Atlas…" never goes back to the deck.
@MainActor
public final class SpeechAudioCache {
    private var store: [String: Data] = [:]
    private var order: [String] = []
    private let limit: Int

    public init(limit: Int = 64) { self.limit = limit }

    static func key(_ line: SpokenLine) -> String { "\(line.desk)\u{0}\(line.text)" }

    public func audio(for line: SpokenLine) -> Data? { store[Self.key(line)] }

    public func keep(_ audio: Data, for line: SpokenLine) {
        let key = Self.key(line)
        if store[key] == nil { order.append(key) }
        store[key] = audio
        while order.count > limit { store[order.removeFirst()] = nil }
    }
}

/// **The desk's call voice, for replies in chat.** Asks the deck to say the
/// line (the same OpenAI voice the live call uses), plays it, and remembers
/// it. When the deck cannot — no key, offline, refused — the line is said by
/// `fallback` (Apple's voice) and the trace says `tts.fallback`.
@MainActor
public final class DeckSpeechOutput: SpeechOutput {
    public var onFinish: (() -> Void)?
    public var onLevel: ((Float) -> Void)? {
        didSet { player.onLevel = onLevel; fallback.onLevel = onLevel }
    }

    private let client: () -> SpeechClient?
    private let fallback: SpeechOutput
    private let player: AudioClipPlayer
    private let cache: SpeechAudioCache
    private var turn = 0
    /// The fetch for the line being spoken, for a test to wait on.
    public private(set) var pending: Task<Void, Never>?

    public init(client: @escaping () -> SpeechClient?, fallback: SpeechOutput,
                player: AudioClipPlayer? = nil, cache: SpeechAudioCache? = nil) {
        self.client = client
        self.fallback = fallback
        self.player = player ?? SystemAudioClipPlayer()
        self.cache = cache ?? SpeechAudioCache()
        self.player.onFinish = { [weak self] in self?.onFinish?() }
        self.fallback.onFinish = { [weak self] in self?.onFinish?() }
    }

    public func speak(_ line: SpokenLine) {
        turn += 1
        let mine = turn
        if let audio = cache.audio(for: line) {
            pending = nil
            play(audio, line: line)
            return
        }
        guard let client = client() else {
            useFallback(line, why: "no deck")
            return
        }
        pending = Task { [weak self] in
            do {
                let audio = try await client.speech(text: line.text, agent: line.desk)
                guard let self, self.turn == mine else { return }
                self.cache.keep(audio, for: line)
                self.play(audio, line: line)
            } catch {
                guard let self, self.turn == mine else { return }
                self.useFallback(line, why: (error as? DeckError).map { "\($0)" } ?? "\(type(of: error))")
            }
        }
    }

    public func stop() {
        turn += 1
        pending?.cancel()
        player.stop()
        fallback.stop()
    }

    private func play(_ audio: Data, line: SpokenLine) {
        do {
            try player.play(audio)
            VoiceTrace.note("tts.deck", "desk=\(line.desk) bytes=\(audio.count)")
        } catch {
            useFallback(line, why: "unplayable audio")
        }
    }

    private func useFallback(_ line: SpokenLine, why: String) {
        VoiceTrace.fail("tts.fallback", "desk=\(line.desk) \(why) — Apple voice")
        fallback.speak(line)
    }
}
