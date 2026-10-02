import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The Mac's way in to Standing approvals**: a sidebar row that wears the
/// number of proposals waiting on him, and a sheet that opens the screen.
@MainActor
final class TheMacOpensStandingApprovalsTests: XCTestCase {
    private func source(_ path: String) throws -> String {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheStoreOpensTheScreenWhenTheDeckCanAnswer() {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        XCTAssertNotNil(store.standingClient)
        store.showStanding()
        XCTAssertTrue(store.isShowingStanding)
        store.dismissStanding()
        XCTAssertFalse(store.isShowingStanding)
    }

    func testAClientWithoutTheRoutesHasNoRow() {
        let store = DeckStore(client: ScriptedDeckClient(), approvalPollInterval: 600)
        XCTAssertNil(store.standingClient)
        store.showStanding()
        XCTAssertFalse(store.isShowingStanding)
    }

    func testTheBadgeCountsTheProposalsWaiting() async {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        XCTAssertEqual(store.standingProposed, 0)
        await store.refreshStandingProposed(force: true)
        XCTAssertEqual(store.standingProposed, 1, "the fixture holds one desk's proposal")
    }

    func testTheSidebarRowWearsTheBadgeAndTheRootMountsTheScreen() throws {
        let sidebar = try source("macos/Sources/DeckUI/SidebarView.swift")
        XCTAssertTrue(sidebar.contains("onOpenStanding"))
        XCTAssertTrue(sidebar.contains("StandingPresentation.badge(store.standingProposed)"))
        XCTAssertTrue(try source("macos/Sources/DeckUI/DeckRootView.swift").contains("StandingApprovalsView("))
        XCTAssertTrue(try source("macos/Sources/DeckUI/DeckStore.swift").contains("await store.refreshStandingProposed()"),
                      "the count is kept fresh on the deck-wide watch")
    }
}
