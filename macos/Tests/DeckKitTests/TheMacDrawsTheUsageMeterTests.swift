import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The Claude plan meter on the Mac.** `GET /v1/usage` is live and the
/// iPhone draws it; the Mac fetched it and drew nothing. Now the store asks
/// any client that can answer (`ClaudeUsageSource`) when the roster loads and
/// publishes what it heard; the sidebar draws it with the phone's own view.
@MainActor
final class TheMacDrawsTheUsageMeterTests: XCTestCase {

    func testTheStorePublishesTheMeterWhenTheRosterLoads() async {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        XCTAssertNil(store.usage)
        await store.loadRoster()
        let usage = store.usage
        XCTAssertEqual(usage?.available, true)
        XCTAssertEqual(usage?.plan, "Max 20×")
        XCTAssertEqual(usage?.windows.first?.key, "session", "the 5-hour window leads")
    }

    func testADeckThatCannotAnswerHasNoMeterRatherThanAnError() async {
        let store = DeckStore(client: ScriptedDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        XCTAssertNil(store.usage)
    }

    func testTheMeterIsThePhonesViewCompiledIntoBothApps() throws {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: repo.appendingPathComponent("ios/AgentDeckPhone/UsageMeterView.swift").path),
            "the phone keeps its own copy of the meter")
        let project = try String(contentsOf: repo.appendingPathComponent("ios/project.yml"), encoding: .utf8)
        XCTAssertTrue(project.contains("../macos/Sources/DeckUI/UsageMeterView.swift"))
        let sidebar = try String(contentsOf: repo.appendingPathComponent("macos/Sources/DeckUI/SidebarView.swift"),
                                 encoding: .utf8)
        XCTAssertTrue(sidebar.contains("UsageMeterView("), "the Mac roster does not draw the meter")
    }
}
