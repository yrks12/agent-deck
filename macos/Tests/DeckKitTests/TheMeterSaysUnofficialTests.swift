import XCTest
@testable import DeckKit

/// **The plan meter is labelled unofficial wherever it is drawn.** It reads an
/// undocumented Anthropic endpoint (the API says `unofficial: true`), and it is
/// on by default (owner ruling 2026-10-01), so every heading the Mac and the
/// iPhone draw says so: `UsageMeterView` names the meter only through
/// `ClaudeUsage.heading`, never a bare "Claude usage".
final class TheMeterSaysUnofficialTests: XCTestCase {

    func testTheHeadingSaysUnofficial() {
        XCTAssertEqual(ClaudeUsage.heading, "Claude usage · unofficial")
    }

    func testTheMeterViewNeverDrawsABareHeading() throws {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let view = try String(contentsOf: repo.appendingPathComponent(
            "macos/Sources/DeckUI/UsageMeterView.swift"), encoding: .utf8)
        XCTAssertFalse(view.contains("\"Claude usage\""), "a heading drops the unofficial label")
        XCTAssertFalse(view.contains("\"Claude usage ·"), "a heading spells its own label")
        XCTAssertGreaterThanOrEqual(view.components(separatedBy: "ClaudeUsage.heading").count - 1, 3,
                                    "each of the three meter layouts names it through the heading")
    }
}
