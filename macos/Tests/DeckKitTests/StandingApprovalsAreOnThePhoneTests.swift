import XCTest
@testable import DeckKit

/// **Standing approvals on the iPhone**: the Mac's screen and limit sheet,
/// compiled into the phone; a line on the roster and a menu entry that wear
/// the proposed count; and "Always, up to a limit…" on the phone's card.
final class StandingApprovalsAreOnThePhoneTests: XCTestCase {
    private func read(_ path: String) throws -> String {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheWordsForTheCountAreDeckKits() {
        XCTAssertEqual(StandingPresentation.waitingLine(1), "1 standing approval waits on you")
        XCTAssertEqual(StandingPresentation.waitingLine(3), "3 standing approvals wait on you")
        XCTAssertEqual(StandingPresentation.menuTitle(0), "Standing approvals")
        XCTAssertEqual(StandingPresentation.menuTitle(2), "Standing approvals (2)")
    }

    func testThePhoneCompilesTheSharedScreenAndSheet() throws {
        let project = try read("ios/project.yml")
        for file in ["StandingApprovalsView.swift", "StandingApprovalsModel.swift", "StandingLimitSheet.swift"] {
            XCTAssertTrue(project.contains("../macos/Sources/DeckUI/\(file)"), "\(file) is not in the iPhone target")
        }
    }

    func testTheRosterOpensItAndWearsTheCount() throws {
        let roster = try read("ios/AgentDeckPhone/RosterView.swift")
        XCTAssertTrue(roster.contains("StandingApprovalsView("))
        XCTAssertTrue(roster.contains("StandingPresentation.waitingLine(store.standingProposed)"))
        XCTAssertTrue(roster.contains("StandingPresentation.menuTitle(store.standingProposed)"))
        let store = try read("ios/AgentDeckPhone/PhoneStore.swift")
        XCTAssertTrue(store.contains("var standingClient: StandingApprovalsClient?"))
        XCTAssertTrue(store.contains("await refreshStanding()"), "the count is kept fresh")
    }

    func testThePhoneCardOffersTheLimit() throws {
        let card = try read("ios/AgentDeckPhone/AttentionView.swift")
        XCTAssertTrue(card.contains("item.offersStanding"))
        XCTAssertTrue(card.contains("StandingLimitSheet("))
        XCTAssertTrue(card.contains("store.stand("))
        XCTAssertTrue(try read("ios/AgentDeckPhone/PhoneStore.swift").contains("standFromCard(route:"))
    }
}
