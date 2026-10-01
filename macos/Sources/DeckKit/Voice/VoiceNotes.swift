import Foundation
import AVFoundation

/// A recorded voice message on an owner message: `voice_note` on the wire.
public struct VoiceNoteRef: Hashable, Codable, Sendable {
    public var id: String
    public var url: String

    public init(id: String, url: String) {
        self.id = id
        self.url = url
    }
}

/// What the deck answered a voice message with: his message, and what it
/// heard.
public struct VoiceNoteSent: Sendable {
    public var message: Message
    public var transcript: String

    public init(message: Message, transcript: String) {
        self.message = message
        self.transcript = transcript
    }
}

/// `POST /v1/threads/{id}/voice-notes` and `GET /v1/voice-notes/{id}`. The
/// deck transcribes (Hebrew and English) with the key it keeps.
public protocol VoiceNoteClient: Sendable {
    func sendVoiceNote(threadID: String, audio: Data) async throws -> VoiceNoteSent
    func voiceNoteAudio(id: String) async throws -> Data
}

/// **The microphone, kept as audio** (not turned into words here).
@MainActor
public protocol VoiceRecorder: AnyObject {
    /// His loudness 0...1 while recording — draws the waveform.
    var onLevel: ((Float) -> Void)? { get set }
    func permission() async -> VoicePermission
    func start() throws
    /// Stops and hands back the recording (AAC in an m4a container).
    func finish() async throws -> Data
    func cancel()
}

/// `AVAudioRecorder` to a temporary m4a, deleted once read. Never built in a
/// test process (`AudioGate`): `start` throws there and the mic stays shut.
@MainActor
public final class SystemVoiceRecorder: VoiceRecorder {
    public var onLevel: ((Float) -> Void)?
    private var recorder: AVAudioRecorder?
    private var meter: Timer?
    public private(set) var hasBuiltRecorder = false

    public init() {}

    public func permission() async -> VoicePermission {
        if AudioGate.isSilenced { return .denied("The mic is off in a test process.") }
        #if os(iOS)
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return .granted
        case .undetermined:
            return await AVAudioApplication.requestRecordPermission()
                ? .granted : .denied(Self.micOffPhone)
        default: return .denied(Self.micOffPhone)
        }
        #else
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .audio)
                ? .granted : .denied(VoicePermission.microphoneOff)
        default: return .denied(VoicePermission.microphoneOff)
        }
        #endif
    }

    static let micOffPhone = "The mic is off for Agent Deck — turn it on in Settings › Agent Deck. Typing still works."

    public func start() throws {
        if AudioGate.isSilenced { throw DeckError.transport("the mic is off in a test process") }
        cancel()
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetooth])
        try session.setActive(true)
        #endif
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voice-note-\(UUID().uuidString).m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 24_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
        let made = try AVAudioRecorder(url: url, settings: settings)
        hasBuiltRecorder = true
        made.isMeteringEnabled = true
        guard made.record() else { throw DeckError.transport("the recorder did not start") }
        recorder = made
        meter = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    public func finish() async throws -> Data {
        guard let recorder else { throw DeckError.transport("nothing was recording") }
        meter?.invalidate(); meter = nil
        recorder.stop()
        self.recorder = nil
        let url = recorder.url
        defer {
            try? FileManager.default.removeItem(at: url)
            release()
        }
        return try Data(contentsOf: url)
    }

    public func cancel() {
        meter?.invalidate(); meter = nil
        guard let recorder else { return }
        recorder.stop()
        recorder.deleteRecording()
        self.recorder = nil
        release()
    }

    private func release() {
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private func tick() {
        guard let recorder else { return }
        recorder.updateMeters()
        let db = recorder.averagePower(forChannel: 0)
        onLevel?(max(0, min(1, (db + 50) / 50)))
    }
}

/// **Record a voice message, not a call.** Tap to record (the waveform is his
/// voice), tap to send, or throw it away. The deck turns it into words and it
/// lands as his own message — the desk wakes for it as for anything he types.
@MainActor
public final class VoiceNoteComposer: ObservableObject {
    public enum State: Equatable, Sendable { case idle, recording, sending }

    @Published public private(set) var state: State = .idle
    /// His loudness, one sample per tick, newest last.
    @Published public private(set) var levels: [Float] = []
    @Published public private(set) var notice: String?
    @Published public private(set) var startedAt: Date?
    /// His message, as the deck stored it.
    public var onSent: ((Message) -> Void)?

    public static let keepLevels = 120

    private let recorder: VoiceRecorder
    private let client: () -> VoiceNoteClient?
    private var threadID: String?

    public init(recorder: VoiceRecorder, client: @escaping () -> VoiceNoteClient?) {
        self.recorder = recorder
        self.client = client
        recorder.onLevel = { [weak self] level in
            guard let self, self.state == .recording else { return }
            self.levels.append(level)
            if self.levels.count > Self.keepLevels { self.levels.removeFirst(self.levels.count - Self.keepLevels) }
        }
    }

    public func start(threadID: String) async {
        guard state == .idle else { return }
        notice = nil
        if case .denied(let sentence) = await recorder.permission() {
            notice = sentence
            return
        }
        do {
            try recorder.start()
        } catch {
            VoiceTrace.fail("note.record_failed", "\(error)")
            notice = "Couldn't start recording. Typing still works."
            return
        }
        self.threadID = threadID
        levels = []
        startedAt = Date()
        state = .recording
        VoiceTrace.note("note.recording")
    }

    public func send() async {
        guard state == .recording, let threadID else { return }
        state = .sending
        defer {
            state = .idle
            levels = []
            startedAt = nil
        }
        do {
            let audio = try await recorder.finish()
            guard let client = client() else { throw DeckError.transport("no deck") }
            let sent = try await client.sendVoiceNote(threadID: threadID, audio: audio)
            VoiceTrace.note("note.sent", "bytes=\(audio.count) chars=\(sent.transcript.count)")
            onSent?(sent.message)
        } catch {
            VoiceTrace.fail("note.send_failed", "\(error)")
            notice = Self.sentence(for: error)
        }
    }

    public func cancel() {
        guard state == .recording else { return }
        recorder.cancel()
        state = .idle
        levels = []
        startedAt = nil
        VoiceTrace.note("note.cancelled")
    }

    public func dismissNotice() { notice = nil }

    static func sentence(for error: Error) -> String {
        if case .http(_, let reason)? = error as? DeckError {
            switch reason {
            case "nothing_heard": return "Nothing could be heard in that recording — nothing was sent."
            case "transcription_unavailable":
                return "The deck can't transcribe voice messages (no OpenAI key on the box). Typing still works."
            default: break
            }
        }
        let why = (error as? DeckError)?.userFacingText ?? "the deck did not answer"
        return "That voice message didn't send — \(why)"
    }
}

/// **His own voice message, played back from his bubble.** One at a time;
/// tapping the one playing stops it. A recording is fetched once.
@MainActor
public final class VoiceNotePlayer: ObservableObject {
    @Published public private(set) var playing: String?
    @Published public private(set) var loading: String?

    private let client: () -> VoiceNoteClient?
    private let player: AudioClipPlayer
    private var audio: [String: Data] = [:]

    public init(client: @escaping () -> VoiceNoteClient?, player: AudioClipPlayer? = nil) {
        self.client = client
        self.player = player ?? SystemAudioClipPlayer()
        self.player.onFinish = { [weak self] in self?.playing = nil }
    }

    public func toggle(_ note: VoiceNoteRef) async {
        if playing == note.id {
            player.stop()
            playing = nil
            return
        }
        if playing != nil {
            player.stop()
            playing = nil
        }
        do {
            let data: Data
            if let kept = audio[note.id] {
                data = kept
            } else {
                guard let client = client() else { return }
                loading = note.id
                defer { loading = nil }
                data = try await client.voiceNoteAudio(id: note.id)
                audio[note.id] = data
            }
            playing = note.id
            try player.play(data)
        } catch {
            playing = nil
            VoiceTrace.fail("note.play_failed", "\(error)")
        }
    }
}
