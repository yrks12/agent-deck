import Foundation
import AVFoundation

/// One thing to say, and whose mouth it comes out of.
public struct SpokenLine: Hashable, Sendable {
    public var text: String
    /// The desk's wire name: picks the voice when `voice` names none.
    public var desk: String
    /// K6, from the call or the roster. `nil` or `id == ""` = pick by name.
    public var voice: DeskVoice?
    /// `"en"` or `"he"`.
    public var language: String

    public init(text: String, desk: String, voice: DeskVoice? = nil, language: String? = nil) {
        self.text = text
        self.desk = desk
        self.voice = voice
        self.language = language ?? Speakable.language(of: text)
    }
}

/// **The speaker.** One line at a time: `VoiceSession` owns the queue, so the
/// order and barge-in are decided in one place and testable with a fake.
@MainActor
public protocol SpeechOutput: AnyObject {
    /// Called when the line last handed to `speak` reaches its end. **Not**
    /// called after `stop()` — the session already knows it stopped.
    var onFinish: (() -> Void)? { get set }
    /// How "loud" the voice is, 0...1, word by word — drives the character's
    /// mouth. 0 when a line ends or stops.
    var onLevel: ((Float) -> Void)? { get set }
    func speak(_ line: SpokenLine)
    /// Silence now, mid-word.
    func stop()
}

/// `AVSpeechSynthesizer`: free, on-device, ships with the OS.
@MainActor
public final class SystemSpeechOutput: NSObject, SpeechOutput {
    public var onFinish: (() -> Void)?
    public var onLevel: ((Float) -> Void)?
    private var wordCount = 0
    /// Built on first use: a thread opening must not wake the audio stack.
    private var synthesizer: AVSpeechSynthesizer?
    private var current: AVSpeechUtterance?

    public override init() { super.init() }

    /// For the test that pins `AudioGate`: true once a real synthesizer exists.
    public var hasBuiltSynthesizer: Bool { synthesizer != nil }

    public func speak(_ line: SpokenLine) {
        if AudioGate.isSilenced {
            onFinish?()
            return
        }
        let synthesizer = self.synthesizer ?? {
            let made = AVSpeechSynthesizer()
            made.delegate = self
            self.synthesizer = made
            return made
        }()
        let utterance = AVSpeechUtterance(string: line.text)
        let installed = AVSpeechSynthesisVoice.speechVoices().map {
            VoiceCandidate(identifier: $0.identifier, name: $0.name, language: $0.language,
                           quality: $0.quality.rawValue, isNovelty: $0.voiceTraits.contains(.isNoveltyVoice))
        }
        if let pick = VoicePicker.pick(desk: line.desk, requested: line.voice, language: line.language, among: installed) {
            utterance.voice = AVSpeechSynthesisVoice(identifier: pick.identifier)
        } else {
            utterance.voice = AVSpeechSynthesisVoice(language: line.language == "he" ? "he-IL" : "en-US")
        }
        let rate = Float(line.voice?.rate ?? 1.0) * AVSpeechUtteranceDefaultSpeechRate
        utterance.rate = min(max(rate, AVSpeechUtteranceMinimumSpeechRate), AVSpeechUtteranceMaximumSpeechRate)
        current = utterance
        synthesizer.speak(utterance)
    }

    public func stop() {
        current = nil
        synthesizer?.stopSpeaking(at: .immediate)
        onLevel?(0)
    }

    private func spokeWord(_ utterance: AVSpeechUtterance) {
        guard utterance === current else { return }
        wordCount += 1
        // No amplitude comes out of AVSpeechSynthesizer; a word boundary is
        // the honest beat, so the mouth opens on each word.
        onLevel?(wordCount.isMultiple(of: 2) ? 0.85 : 0.5)
    }

    private func finished(_ utterance: AVSpeechUtterance) {
        // A stopped line's late callback, or one from before a barge-in, is
        // not the end of the line being spoken now.
        guard utterance === current else { return }
        current = nil
        onLevel?(0)
        onFinish?()
    }
}

extension SystemSpeechOutput: AVSpeechSynthesizerDelegate {
    nonisolated public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let box = UncheckedBox(utterance)
        Task { @MainActor in self.finished(box.value) }
    }

    nonisolated public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                              willSpeakRangeOfSpeechString characterRange: NSRange,
                                              utterance: AVSpeechUtterance) {
        let box = UncheckedBox(utterance)
        Task { @MainActor in self.spokeWord(box.value) }
    }
}

/// AVSpeechUtterance is not `Sendable`; it is only compared by identity on
/// the main actor.
private struct UncheckedBox<T>: @unchecked Sendable {
    let value: T
    init(_ value: T) { self.value = value }
}
