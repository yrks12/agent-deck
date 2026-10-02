import Foundation
import AVFoundation

/// Plays one clip of encoded audio with a speed, a position and a pause.
/// Pitch stays put at any speed.
@MainActor
public protocol ListenAudio: AnyObject {
    /// The clip reached its end. Not called after `stop()`.
    var onFinish: (() -> Void)? { get set }
    var position: TimeInterval { get }
    var duration: TimeInterval { get }
    func play(_ audio: Data, rate: Float) throws
    func pause()
    func resume()
    func stop()
    func setRate(_ rate: Float)
    func seek(to seconds: TimeInterval)
}

/// `AVAudioPlayer` with `enableRate`: it keeps the voice's pitch at 0.75× to
/// 2×. Never built in a test process (`AudioGate`).
@MainActor
public final class SystemListenAudio: NSObject, ListenAudio {
    public var onFinish: (() -> Void)?
    private var player: AVAudioPlayer?
    private var rate: Float = 1

    public override init() { super.init() }

    public var position: TimeInterval { player?.currentTime ?? 0 }
    public var duration: TimeInterval { player?.duration ?? 0 }

    public func play(_ audio: Data, rate: Float) throws {
        stop()
        if AudioGate.isSilenced {
            onFinish?()
            return
        }
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        if session.category != .playAndRecord {
            try? session.setCategory(.playback, mode: .spokenAudio)
            try? session.setActive(true)
        }
        #endif
        let made = try AVAudioPlayer(data: audio)
        made.enableRate = true
        made.delegate = self
        made.prepareToPlay()
        made.rate = rate
        self.rate = rate
        player = made
        made.play()
    }

    public func pause() { player?.pause() }

    public func resume() {
        player?.rate = rate
        player?.play()
    }

    public func stop() {
        player?.delegate = nil
        player?.stop()
        player = nil
    }

    public func setRate(_ rate: Float) {
        self.rate = rate
        player?.rate = rate
    }

    public func seek(to seconds: TimeInterval) {
        guard let player else { return }
        player.currentTime = max(0, min(seconds, player.duration))
    }

    fileprivate func ended(_ finished: AVAudioPlayer) {
        guard finished === player else { return }
        player = nil
        onFinish?()
    }
}

extension SystemListenAudio: AVAudioPlayerDelegate {
    nonisolated public func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let box = ListenBox(player)
        Task { @MainActor in self.ended(box.value) }
    }
}

private struct ListenBox<T>: @unchecked Sendable {
    let value: T
    init(_ value: T) { self.value = value }
}

/// The position in the clip, for the strip's scrubber and time only.
@MainActor
public final class ListenClock: ObservableObject {
    @Published public private(set) var progress: Double = 0
    @Published public private(set) var elapsed: TimeInterval = 0
    @Published public private(set) var duration: TimeInterval = 0

    func set(elapsed: TimeInterval, duration: TimeInterval) {
        self.elapsed = elapsed
        self.duration = duration
        progress = duration > 0 ? max(0, min(1, elapsed / duration)) : 0
    }
}

/// **Listen to a message from a desk, in the desk's call voice.**
///
/// A tap on a bubble's Listen control asks the deck to say the message
/// (`POST /v1/speech`, the OpenAI key stays on the box), one request per piece
/// of at most `Speakable.listenChunkLimit` characters, plays the pieces as one
/// clip so the strip's scrubber spans the whole message, and keeps what it
/// fetched. Only when the deck cannot is the message read in Apple's voice
/// (no scrubber or speed then; traced as `tts.fallback`).
///
/// One message at a time. A call or a voice message in progress (`voiceBusy`)
/// stops it and keeps it from starting. The "Spoken replies" switch is a
/// different thing and is not read or written here.
@MainActor
public final class ListenPlayer: ObservableObject {
    public enum State: Equatable, Sendable {
        case idle
        case loading(String)
        case playing(String)
        case paused(String)

        public var messageID: String? {
            switch self {
            case .idle: return nil
            case .loading(let id), .playing(let id), .paused(let id): return id
            }
        }
    }

    @Published public private(set) var state: State = .idle
    @Published public private(set) var speed: Double
    @Published public private(set) var playNext: Bool
    /// Where the clip is, four times a second. Its own object: every bubble
    /// observes the player, and none of them may redraw for the clock.
    public let clock = ListenClock()
    public var progress: Double { clock.progress }
    public var elapsed: TimeInterval { clock.elapsed }
    public var duration: TimeInterval { clock.duration }
    /// Apple's voice is reading (the deck could not): no speed, no scrubber.
    @Published public private(set) var isFallback = false
    /// One plain line when a tap could not start anything.
    @Published public private(set) var notice: String?

    /// The thread on screen, so "play next" knows what follows.
    public var messages: [Message] = []
    /// Just before a message starts: the replies speaker goes quiet.
    public var onStart: (() -> Void)?
    /// The fetch for the message being loaded, for a test to wait on.
    public private(set) var pending: Task<Void, Never>?

    private let client: () -> SpeechClient?
    private let fallback: SpeechOutput
    private let audio: ListenAudio
    private let cache: SpeechAudioCache
    private let defaults: UserDefaults
    private var turn = 0
    private var busy = false
    private var ticker: Timer?

    public init(client: @escaping () -> SpeechClient?, fallback: SpeechOutput,
                audio: ListenAudio? = nil, cache: SpeechAudioCache? = nil,
                defaults: UserDefaults = .standard) {
        self.client = client
        self.fallback = fallback
        self.audio = audio ?? SystemListenAudio()
        self.cache = cache ?? SpeechAudioCache()
        self.defaults = defaults
        self.speed = ListenSpeed.stored(in: defaults)
        self.playNext = defaults.bool(forKey: ListenSpeed.playNextKey)
        self.audio.onFinish = { [weak self] in self?.finished() }
        self.fallback.onFinish = { [weak self] in self?.finished() }
    }

    /// An agent's own words get the control. Cheap: it runs on every draw.
    public static func canListen(_ message: Message) -> Bool {
        message.role == .agent && message.kind == .text
            && !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public var isActive: Bool { state != .idle }

    // MARK: - Controls

    /// The Listen control: start, or pause / resume this message's own audio.
    public func toggle(_ message: Message) {
        switch state {
        case .playing(message.id) where !isFallback:
            audio.pause()
            state = .paused(message.id)
            stopTicker()
        case .playing(message.id), .loading(message.id):
            stop()
        case .paused(message.id):
            audio.resume()
            state = .playing(message.id)
            startTicker()
        default:
            start(message)
        }
    }

    /// The strip's play / pause.
    public func pauseOrResume() {
        switch state {
        case .playing(let id) where !isFallback:
            audio.pause(); state = .paused(id); stopTicker()
        case .paused(let id):
            audio.resume(); state = .playing(id); startTicker()
        default: break
        }
    }

    public func stop() {
        turn += 1
        pending?.cancel()
        pending = nil
        audio.stop()
        fallback.stop()
        stopTicker()
        state = .idle
        isFallback = false
        clock.set(elapsed: 0, duration: 0)
    }

    public func cycleSpeed() {
        speed = ListenSpeed.next(after: speed)
        ListenSpeed.store(speed, in: defaults)
        if !isFallback, state != .idle { audio.setRate(Float(speed)) }
    }

    public func setPlayNext(_ on: Bool) {
        playNext = on
        defaults.set(on, forKey: ListenSpeed.playNextKey)
    }

    public func seek(toFraction fraction: Double) {
        guard !isFallback else { return }
        switch state {
        case .playing, .paused: moveTo(fraction)
        default: return
        }
    }

    private func moveTo(_ fraction: Double) {
        let clamped = max(0, min(1, fraction))
        audio.seek(to: clamped * audio.duration)
        clock.set(elapsed: clamped * audio.duration, duration: audio.duration)
    }

    public func dismissNotice() { notice = nil }

    /// A call or a voice message is starting (`true`) or over (`false`).
    public func voiceBusy(_ on: Bool) {
        busy = on
        if on { stop() }
    }

    /// Reads the clock; the timer calls this, a test calls it by hand.
    public func tick() {
        guard case .playing = state, !isFallback else { return }
        clock.set(elapsed: audio.position, duration: audio.duration)
    }

    // MARK: - Starting

    private func start(_ message: Message) {
        guard !busy else {
            notice = "Finish the call or voice message first, then listen."
            return
        }
        let chunks = Speakable.listenChunks(message.text)
        guard !chunks.isEmpty else {
            notice = "There is nothing to read aloud in that message — it is all code or links."
            return
        }
        stop()
        notice = nil
        onStart?()
        turn += 1
        let mine = turn
        let lines = chunks.map { SpokenLine(text: $0, desk: message.author) }
        state = .loading(message.id)

        if lines.allSatisfy({ cache.audio(for: $0) != nil }) {
            play(lines.compactMap { cache.audio(for: $0) }, message: message, lines: lines)
            return
        }
        guard let client = client() else {
            useFallback(message, lines: lines, why: "no deck")
            return
        }
        pending = Task { [weak self] in
            do {
                var parts: [Data] = []
                for line in lines {
                    if let kept = self?.cache.audio(for: line) { parts.append(kept); continue }
                    let data = try await client.speech(text: line.text, agent: line.desk)
                    guard let self, self.turn == mine else { return }
                    self.cache.keep(data, for: line)
                    parts.append(data)
                }
                guard let self, self.turn == mine else { return }
                self.play(parts, message: message, lines: lines)
            } catch {
                guard let self, self.turn == mine else { return }
                self.useFallback(message, lines: lines,
                                 why: (error as? DeckError).map { "\($0)" } ?? "\(type(of: error))")
            }
        }
    }

    private func play(_ parts: [Data], message: Message, lines: [SpokenLine]) {
        // mp3 is a stream of frames: the pieces join end to end.
        let clip = parts.reduce(into: Data()) { $0.append($1) }
        state = .playing(message.id)
        do {
            try audio.play(clip, rate: Float(speed))
            VoiceTrace.note("tts.listen", "desk=\(message.author) pieces=\(parts.count) bytes=\(clip.count)")
            if state == .playing(message.id) { startTicker() }
        } catch {
            useFallback(message, lines: lines, why: "unplayable audio")
        }
    }

    private func useFallback(_ message: Message, lines: [SpokenLine], why: String) {
        VoiceTrace.fail("tts.fallback", "desk=\(message.author) \(why) — Apple voice")
        isFallback = true
        state = .playing(message.id)
        fallback.speak(SpokenLine(text: lines.map(\.text).joined(separator: " "), desk: message.author))
    }

    private func finished() {
        let id = state.messageID
        stop()
        guard playNext, let id, let index = messages.firstIndex(where: { $0.id == id }),
              let next = messages[messages.index(after: index)...].first(where: { Self.canListen($0) && !Speakable.listenChunks($0.text).isEmpty })
        else { return }
        start(next)
    }

    // MARK: - Clock

    private func startTicker() {
        stopTicker()
        guard !AudioGate.isSilenced else { return }
        ticker = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    private func stopTicker() {
        ticker?.invalidate()
        ticker = nil
    }
}
