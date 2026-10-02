import Foundation
import AVFoundation
import Speech

/// Whether he can be heard, and if not, the one sentence that says why. The
/// sentence is what the composer shows; typing keeps working either way.
public enum VoicePermission: Equatable, Sendable {
    case granted
    case denied(String)

    public static let microphoneOff =
        "The mic is off for Shaliach — turn it on in System Settings › Privacy & Security › Microphone. Typing still works."
    public static let speechOff =
        "Speech recognition is off for Shaliach — turn it on in System Settings › Privacy & Security › Speech Recognition. Typing still works."
    public static let noMicrophone =
        "This Mac has no microphone Shaliach can use. Typing still works."

    /// On-device only, by the plan's default: without Apple's on-device model
    /// for the language, listening would send his voice to Apple's servers.
    public static func notOnDevice(_ localeID: String) -> String {
        let name = Locale(identifier: "en").localizedString(forIdentifier: localeID) ?? localeID
        return "Listening in \(name) would send your voice to Apple, so it is off. Typing still works."
    }
}

/// **The microphone, turned into words.** `start` begins hearing and reports
/// each partial transcript; `finish` stops and returns the final one.
@MainActor
public protocol SpeechInput: AnyObject {
    /// May show the macOS prompts — so only ever called from his press.
    func permission() async -> VoicePermission
    /// Echo cancellation (voice processing) for an open-mic call. Off for hold-
    /// to-talk: it costs ~1 s of set-up on the main thread (measured) and
    /// there is no speaker to cancel while he holds the button.
    var cancelsEcho: Bool { get set }
    /// His loudness, 0...1, while listening — already thinned by `LevelGate`,
    /// so it stops arriving when he is silent.
    var onLevel: ((Float) -> Void)? { get set }
    func start(heard: @escaping @MainActor (String) -> Void) throws
    func finish() async -> String
    /// Stop and discard.
    func cancel()
}

/// C-4: whether this Mac recognises a language without sending audio off it.
/// Creating a recogniser asks nothing of him.
public enum OnDeviceSpeech {
    public static func supports(_ localeID: String) -> Bool {
        SFSpeechRecognizer(locale: Locale(identifier: localeID))?.supportsOnDeviceRecognition ?? false
    }
}

/// `SFSpeechRecognizer` on `AVAudioEngine`, on-device only.
@MainActor
public final class SystemSpeechInput: SpeechInput {
    /// `deckVoiceLocale` in the app's defaults, else `en-US` (measured: on-
    /// device on his Mac; `he-IL` is not).
    public let localeID: String
    public var cancelsEcho = false
    public var onLevel: ((Float) -> Void)?
    private var engine: AVAudioEngine?
    private var configObserver: NSObjectProtocol?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var latest = ""
    private var final: CheckedContinuation<String, Never>?
    /// Which listen a recogniser callback belongs to. A finished request can
    /// still call back; its words must not land in the next utterance.
    private var generation = 0

    public init(localeID: String? = nil) {
        self.localeID = localeID ?? UserDefaults.standard.string(forKey: "deckVoiceLocale") ?? "en-US"
    }

    public func permission() async -> VoicePermission {
        let answer = await askPermission()
        switch answer {
        case .granted: VoiceTrace.note("permission.granted", "locale=\(localeID)")
        case .denied(let why): VoiceTrace.fail("permission.denied", why)
        }
        return answer
    }

    private func askPermission() async -> VoicePermission {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)) else {
            return .denied(VoicePermission.notOnDevice(localeID))
        }
        guard recognizer.supportsOnDeviceRecognition else {
            return .denied(VoicePermission.notOnDevice(localeID))
        }
        var speech = SFSpeechRecognizer.authorizationStatus()
        if speech == .notDetermined {
            speech = await withCheckedContinuation { done in
                SFSpeechRecognizer.requestAuthorization { done.resume(returning: $0) }
            }
        }
        guard speech == .authorized else { return .denied(VoicePermission.speechOff) }

        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            guard granted else { return .denied(VoicePermission.microphoneOff) }
        default:
            return .denied(VoicePermission.microphoneOff)
        }
        guard AVCaptureDevice.default(for: .audio) != nil else { return .denied(VoicePermission.noMicrophone) }
        return .granted
    }

    public func start(heard: @escaping @MainActor (String) -> Void) throws {
        cancel()
        // A test process never opens his mic (AudioGate).
        if AudioGate.isSilenced { return }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)),
              recognizer.supportsOnDeviceRecognition else {
            throw DeckError.transport(VoicePermission.notOnDevice(localeID))
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        request.addsPunctuation = true

        let engine = AVAudioEngine()
        let node = engine.inputNode
        // Echo cancellation: on a call the Mac's own speaker must not count as
        // him talking, or the desk would interrupt itself.
        if cancelsEcho {
            do { try node.setVoiceProcessingEnabled(true) } catch {
                VoiceTrace.fail("input.vp_failed", "\(error)")
            }
        }
        let format = node.outputFormat(forBus: 0)
        VoiceTrace.note("input.start", "tap=\(format.channelCount)ch/\(Int(format.sampleRate))Hz vp=\(cancelsEcho)")
        let levels = LevelRelay(gate: LevelGate()) { [weak self] level in self?.onLevel?(level) }
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            // Channel 0 only — see `SpeechFeed`. Nine channels in means "No
            // speech detected" out, and not one word ever reached the desk.
            guard let mono = SpeechFeed.mono(buffer) else { return }
            request.append(mono)
            if let samples = mono.floatChannelData {
                levels.offer(AudioLevel.of(samples[0], count: Int(mono.frameLength)))
            }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            VoiceTrace.fail("input.engine_failed", "\(error)")
            node.removeTap(onBus: 0)
            throw error
        }
        // Turning voice processing on reconfigures the device under a running
        // engine (measured: "iounit configuration changed > stopping the
        // engine"). Nobody restarted it, so the mic went deaf mid-call.
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak engine] _ in
            guard let engine, !engine.isRunning else { return }
            VoiceTrace.note("input.config_changed", "restarting")
            do { try engine.start() } catch { VoiceTrace.fail("input.restart_failed", "\(error)") }
        }

        latest = ""
        generation += 1
        let mine = generation
        self.engine = engine
        self.request = request
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let isFinal = (result?.isFinal ?? false) || error != nil
            if let error { VoiceTrace.note("input.recognizer_ended", "\((error as NSError).domain)#\((error as NSError).code)") }
            Task { @MainActor in
                guard let self, self.generation == mine else { return }
                if let text {
                    self.latest = text
                    heard(text)
                }
                if isFinal { self.deliverFinal() }
            }
        }
    }

    public func finish() async -> String {
        guard request != nil else { return latest }
        stopAudio()
        request?.endAudio()
        // The last words are still being recognised; wait for them, briefly.
        let text = await withCheckedContinuation { (done: CheckedContinuation<String, Never>) in
            final = done
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 800_000_000)
                self?.deliverFinal()
            }
        }
        task = nil
        request = nil
        generation += 1
        VoiceTrace.note("input.finish", text.isEmpty ? "heard=nothing" : "heard=\(text.count)chars")
        return text
    }

    public func cancel() {
        generation += 1
        task?.cancel()
        task = nil
        stopAudio()
        request = nil
        deliverFinal()
    }

    private func deliverFinal() {
        final?.resume(returning: latest)
        final = nil
    }

    private func stopAudio() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
        onLevel?(0)
    }
}
