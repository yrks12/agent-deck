import XCTest
@testable import DeckKit

/// **"Max 20×" never showed.** The deck sends `plan` as an object —
/// `{"name": "Max 20×", "subscription": "max", "tier": "default_claude_max_20x"}`
/// (`tests/test_usage_api.py::test_body_matches_contract_c4`) — and the client
/// decoded it as a String, so the `try?` swallowed it and the meter drew no
/// plan at all, on the Mac and the iPhone both.
final class UsagePlanIsReadTests: XCTestCase {

    private func decode(_ json: String) throws -> ClaudeUsage {
        try DeckCoding.decoder.decode(ClaudeUsage.self, from: Data(json.utf8))
    }

    func testThePlanObjectTheDeckSendsIsReadAsItsName() throws {
        let usage = try decode(#"""
        {"available":true,"stale":false,"reason":null,
         "plan":{"name":"Max 20×","subscription":"max","tier":"default_claude_max_20x"},
         "windows":[{"key":"session","label":"5-hour","percent":38,"severity":"normal","resets_at":1790800000}]}
        """#)
        XCTAssertEqual(usage.plan, "Max 20×")
        XCTAssertEqual(usage.windows.map(\.key), ["session"], "the rest of the body still reads")
    }

    func testAPlanObjectWithNoNameFallsBackToTheSubscription() throws {
        XCTAssertEqual(try decode(#"{"available":true,"plan":{"subscription":"pro"},"windows":[]}"#).plan, "Pro")
    }

    func testAnOlderDecksStringPlanAndANullPlanStillRead() throws {
        XCTAssertEqual(try decode(#"{"available":true,"plan":"max","windows":[]}"#).plan, "max")
        XCTAssertNil(try decode(#"{"available":false,"plan":null,"windows":[]}"#).plan)
    }
}
