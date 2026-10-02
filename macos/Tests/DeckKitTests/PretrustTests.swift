import XCTest
@testable import DeckKit

/// `pretrust` on `POST /v1/agents/interview`'s 201: whether the deck managed
/// to pre-accept Claude Code's workspace-trust dialog before the window it
/// opens ever draws it. When it did not, the owner is already looking at the
/// one screen that matters — the new chat thread — so that is where this has
/// to land, not a fact he finds later in a panel.
final class PretrustTests: XCTestCase {

    func testAnOkPretrustHasNothingToSay() {
        XCTAssertNil(PretrustOutcome(ok: true, reason: "trusted").problemSentence)
        // Even a nonsense reason string is irrelevant once `ok` is true.
        XCTAssertNil(PretrustOutcome(ok: true, reason: "").problemSentence)
    }

    /// Same vocabulary as `Blocked.reason` — both come from the one trust
    /// check pretrust.py runs, just reported at two different moments.
    func testEveryKnownReasonProducesTheSameActionableSentenceAsTheBlockedBadge() {
        for reason in BlockedReason.allCases {
            let outcome = PretrustOutcome(ok: false, reason: reason.rawValue)

            XCTAssertEqual(outcome.problemSentence, reason.sentence)
        }
    }

    func testAnUnrecognisedReasonNeverRendersAsTheSlugItself() {
        let outcome = PretrustOutcome(ok: false, reason: "some_future_reason")

        XCTAssertNotEqual(outcome.problemSentence, "some_future_reason")
        // No `detail` travels on this response (unlike `Blocked`), so an
        // unknown reason has nowhere to fall but the plain sentence.
        XCTAssertEqual(outcome.problemSentence, Blocked.fallbackSentence)
    }

    func testDecodingTheWireShape() throws {
        let json = Data(#"{"ok": false, "reason": "engine_not_covered"}"#.utf8)

        let outcome = try DeckCoding.decoder.decode(PretrustOutcome.self, from: json)

        XCTAssertFalse(outcome.ok)
        XCTAssertEqual(outcome.reason, "engine_not_covered")
        XCTAssertEqual(outcome.problemSentence, BlockedReason.engineNotCovered.sentence)
    }
}
