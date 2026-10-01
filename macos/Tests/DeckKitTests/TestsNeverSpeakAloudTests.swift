import XCTest
@testable import DeckKit

/// MEASURED 2026-09-30: a whole `swift test` run spoke through the owner's
/// speakers in the Mac's system voice while he was working -- a view under test
/// built the real `SystemSpeechOutput`. A test process must never make a sound.
@MainActor
final class TestsNeverSpeakAloudTests: XCTestCase {
    func testATestProcessIsSilencedByDefault() {
        XCTAssertTrue(AudioGate.isSilenced,
                      "the test process must be recognised as one that may not make sound")
    }

    func testTheSystemVoiceFinishesWithoutEverBuildingASynthesizer() {
        let output = SystemSpeechOutput()
        var finished = false
        output.onFinish = { finished = true }
        output.speak(SpokenLine(text: "this must never be heard", desk: "atlas"))
        XCTAssertTrue(finished, "a silenced line still reports finished, so callers move on")
        XCTAssertFalse(output.hasBuiltSynthesizer,
                       "no AVSpeechSynthesizer may be created inside a test process")
    }
}
