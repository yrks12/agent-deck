import XCTest
@testable import DeckKit

/// **Every desk has its own voice, the same one every launch, and it is one of
/// the good ones installed.** K6 may name it; otherwise it is a stable pick by
/// the desk's name among the best voices this Mac has.
final class VoicePickerTests: XCTestCase {

    /// The shape of the owner's Mac, measured: compact Samantha and Daniel,
    /// super-compact regional voices, the Eloquence set and the novelty voices.
    private let installed: [VoiceCandidate] = [
        VoiceCandidate(identifier: "com.apple.voice.compact.en-US.Samantha", name: "Samantha", language: "en-US", quality: 1),
        VoiceCandidate(identifier: "com.apple.voice.compact.en-GB.Daniel", name: "Daniel", language: "en-GB", quality: 1),
        VoiceCandidate(identifier: "com.apple.voice.super-compact.en-AU.Karen", name: "Karen", language: "en-AU", quality: 1),
        VoiceCandidate(identifier: "com.apple.eloquence.en-US.Rocko", name: "Rocko", language: "en-US", quality: 1),
        VoiceCandidate(identifier: "com.apple.speech.synthesis.voice.Fred", name: "Fred", language: "en-US", quality: 1),
        VoiceCandidate(identifier: "com.apple.speech.synthesis.voice.Bells", name: "Bells", language: "en-US", quality: 1, isNovelty: true),
        VoiceCandidate(identifier: "com.apple.voice.compact.he-IL.Carmit", name: "Carmit", language: "he-IL", quality: 1),
    ]

    private let goodOnes: Set<String> = [
        "com.apple.voice.compact.en-US.Samantha", "com.apple.voice.compact.en-GB.Daniel",
    ]

    func testTheVoiceTheDeckNamedWinsWhenItIsInstalled() {
        let pick = VoicePicker.pick(desk: "atlas", requested: DeskVoice(id: "com.apple.voice.compact.en-GB.Daniel"),
                                    language: "en", among: installed)
        XCTAssertEqual(pick?.identifier, "com.apple.voice.compact.en-GB.Daniel")
    }

    func testANamedVoiceThatIsNotInstalledFallsBackToAGoodOne() {
        let pick = VoicePicker.pick(desk: "atlas", requested: DeskVoice(id: "com.apple.voice.premium.en-GB.Malcolm"),
                                    language: "en", among: installed)
        XCTAssertTrue(goodOnes.contains(pick?.identifier ?? ""), pick?.identifier ?? "nil")
    }

    func testNeverANoveltyARobotOrALegacyVoice() {
        for desk in ["atlas", "cos", "chief", "acme", "globex", "scout", "ops", "qa", "writer", "x"] {
            let pick = VoicePicker.pick(desk: desk, requested: nil, language: "en", among: installed)
            XCTAssertTrue(goodOnes.contains(pick?.identifier ?? ""), "\(desk) got \(pick?.identifier ?? "nil")")
        }
    }

    func testTheSameDeskAlwaysGetsTheSameVoiceWhateverOrderTheSystemListsThem() {
        let first = VoicePicker.pick(desk: "atlas", requested: nil, language: "en", among: installed)
        let again = VoicePicker.pick(desk: "atlas", requested: DeskVoice(id: ""), language: "en",
                                     among: installed.reversed())
        XCTAssertEqual(first, again)
    }

    func testDesksDoNotAllShareOneVoice() {
        let picks = Set(["atlas", "cos", "chief", "acme", "globex", "scout", "ops", "qa", "writer"].compactMap {
            VoicePicker.pick(desk: $0, requested: nil, language: "en", among: installed)?.identifier
        })
        XCTAssertEqual(picks, goodOnes)
    }

    func testPremiumBeatsEnhancedBeatsCompact() {
        let withBetter = installed + [
            VoiceCandidate(identifier: "com.apple.voice.enhanced.en-US.Evan", name: "Evan", language: "en-US", quality: 2),
            VoiceCandidate(identifier: "com.apple.voice.premium.en-GB.Malcolm", name: "Malcolm", language: "en-GB", quality: 3),
            VoiceCandidate(identifier: "com.apple.voice.premium.en-US.Zoe", name: "Zoe", language: "en-US", quality: 3),
        ]
        for desk in ["atlas", "cos", "chief", "acme"] {
            let pick = VoicePicker.pick(desk: desk, requested: nil, language: "en", among: withBetter)
            XCTAssertTrue(pick?.identifier.contains(".premium.") ?? false, "\(desk) got \(pick?.identifier ?? "nil")")
        }
    }

    func testHebrewTextGetsAHebrewVoice() {
        let pick = VoicePicker.pick(desk: "atlas", requested: DeskVoice(id: "com.apple.voice.compact.en-GB.Daniel"),
                                    language: "he", among: installed)
        XCTAssertEqual(pick?.identifier, "com.apple.voice.compact.he-IL.Carmit")
    }

    func testNoVoiceInTheLanguageMeansTheSystemDefault() {
        XCTAssertNil(VoicePicker.pick(desk: "atlas", requested: nil, language: "fr", among: installed))
    }
}
