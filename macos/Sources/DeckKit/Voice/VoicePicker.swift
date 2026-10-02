import Foundation

/// One installed system voice, as the picker needs to see it. Plain values so
/// the choice is testable without the synthesiser.
public struct VoiceCandidate: Hashable, Sendable {
    public var identifier: String
    public var name: String
    /// BCP-47, `"en-GB"`.
    public var language: String
    /// `AVSpeechSynthesisVoiceQuality.rawValue`: 1 default, 2 enhanced, 3 premium.
    public var quality: Int
    /// Bells, Boing, Bubbles…: `voiceTraits.isNoveltyVoice`.
    public var isNovelty: Bool

    public init(identifier: String, name: String, language: String, quality: Int, isNovelty: Bool = false) {
        self.identifier = identifier
        self.name = name
        self.language = language
        self.quality = quality
        self.isNovelty = isNovelty
    }

    /// How good it sounds, best highest; `nil` for a voice no desk should get.
    ///
    /// Measured on his Mac: the only English `com.apple.voice.*` voices are
    /// compact Samantha and Daniel; the rest are super-compact regional
    /// voices, the Eloquence set (Rocko, Grandma… — robotic) and the legacy
    /// `speech.synthesis` voices (Fred, Zarvox…, half of them novelty).
    /// Premium and enhanced voices are free downloads in System Settings ›
    /// Accessibility › Spoken Content, and win as soon as one is installed.
    var tier: Int? {
        if isNovelty { return nil }
        if identifier.contains(".eloquence.") || identifier.contains(".speech.synthesis.") { return nil }
        switch quality {
        case 3...: return 4
        case 2: return 3
        default: break
        }
        if identifier.contains(".super-compact.") { return 1 }
        return 2
    }
}

/// **Which voice a desk speaks in.** K6's `voice.id` when the deck names one
/// that is installed and fits the language; otherwise a stable pick by the
/// desk's name among the best voices this Mac has, so the same desk sounds the
/// same every launch and two desks usually sound different.
public enum VoicePicker {

    /// `language` is `"en"` or `"he"` (`Speakable.language(of:)`). `nil` means
    /// let the system use its default voice for that language.
    public static func pick(
        desk: String, requested: DeskVoice?, language: String, among voices: [VoiceCandidate]
    ) -> VoiceCandidate? {
        let fits = voices.filter { speaks($0, language) }
        if let id = requested?.id, !id.isEmpty, let named = fits.first(where: { $0.identifier == id }) {
            return named
        }
        let rated = fits.compactMap { voice in voice.tier.map { (voice, $0) } }
        guard let best = rated.map(\.1).max() else { return nil }
        let pool = rated.filter { $0.1 == best }.map(\.0).sorted { $0.identifier < $1.identifier }
        let index = Int(AvatarLook.fnv1a(desk.lowercased()) % UInt64(pool.count))
        return pool[index]
    }

    private static func speaks(_ voice: VoiceCandidate, _ language: String) -> Bool {
        let code = voice.language.lowercased()
        let wanted = language.lowercased()
        return code == wanted || code.hasPrefix(wanted + "-")
    }
}
