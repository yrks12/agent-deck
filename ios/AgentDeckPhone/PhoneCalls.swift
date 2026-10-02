import SwiftUI
import AVFoundation
import DeckKit

/// **Wires the shared call core to this phone.** The call itself —
/// `VoiceSession`, `RealtimeCallSession`, `EngineCallAudio`, the barge gate —
/// is the Mac's, unchanged; `PhoneCallCenter` (DeckKit) adds the phone's rules.
/// This file only builds them with the real socket, engine, audio session and
/// the deck this app is paired with.
@MainActor
enum PhoneCalls {
    static func make(store: PhoneStore) -> PhoneCallCenter {
        let engines = EngineBox()
        let session = VoiceSession(
            input: PhoneSpeechInput(), output: PhoneVoice.callVoice(store: store),
            calls: { [weak store] in store?.callClient },
            realtime: { dial in
                let audio = EngineCallAudio()
                engines.current = audio
                return RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                           callID: dial.callID, threadID: dial.threadID,
                                           transport: URLSessionRealtimeTransport(), audio: audio,
                                           calls: dial.calls)
            })
        let audioSession = PhoneAudioSession()
        let center = PhoneCallCenter(
            session: session, audioSession: audioSession,
            working: { [weak store] desk in store?.agents[desk]?.state == .working },
            feed: { [weak store] threadID in threadFeed(client: store?.client, threadID: threadID) })
        center.resumeAudio = { engines.current?.resumeAfterInterruption() }
        audioSession.observe { [weak center] event in
            Task { await center?.handle(event) }
        }
        return center
    }

    /// The called desk's thread, as long as the call wants it: the same
    /// `ConversationSync` the thread screen uses, owned by the call.
    static func threadFeed(client: DeckClient?, threadID: String) -> AsyncStream<[Message]> {
        guard let client else {
            return AsyncStream { $0.yield([]); $0.finish() }
        }
        return AsyncStream { continuation in
            let sync = ConversationSync(client: client, threadID: threadID)
            let forward = Task {
                for await update in await sync.updates() { continuation.yield(update.messages) }
            }
            let run = Task {
                do { try await sync.run() } catch { continuation.yield([]) }
            }
            continuation.onTermination = { _ in forward.cancel(); run.cancel() }
        }
    }
}

/// The engine of the call that is up, for resuming after an interruption.
@MainActor
final class EngineBox {
    weak var current: EngineCallAudio?
}

/// **The phone's mic permission.** The live voice needs only the microphone;
/// the Mac's input also demands on-device speech recognition before a call
/// may start, which would refuse a live call on a phone without Apple's
/// on-device model. Listening itself (the fallback when the deck has no live
/// voice) is the Mac's `SystemSpeechInput`, unchanged.
@MainActor
final class PhoneSpeechInput: SpeechInput {
    private let speech = SystemSpeechInput()

    var cancelsEcho: Bool {
        get { speech.cancelsEcho }
        set { speech.cancelsEcho = newValue }
    }

    var onLevel: ((Float) -> Void)? {
        get { speech.onLevel }
        set { speech.onLevel = newValue }
    }

    func permission() async -> VoicePermission {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return .granted
        case .undetermined:
            let granted = await AVAudioApplication.requestRecordPermission()
            return granted ? .granted : .denied(Self.micOff)
        default:
            return .denied(Self.micOff)
        }
    }

    static let micOff = "The mic is off for Agent Deck — turn it on in Settings › Agent Deck. Typing still works."

    func start(heard: @escaping @MainActor (String) -> Void) throws { try speech.start(heard: heard) }
    func finish() async -> String { await speech.finish() }
    func cancel() { speech.cancel() }
}
