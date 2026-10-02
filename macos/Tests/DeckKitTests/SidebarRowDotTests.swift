import XCTest
@testable import DeckKit

/// The dot at the right of a sidebar row: orange when a desk needs him (even
/// with nothing unread), blue for plain unread, nothing when all is read.
final class SidebarRowDotTests: XCTestCase {

    private func row(state: AgentState = .idle, unread: Int, blocked: Bool = false) -> SidebarRow {
        var agent = Agent(name: "acme product", title: "", state: state)
        if blocked { agent.blocked = Blocked(what: "Sign in to Google", reason: "dialog_unrelayed") }
        return SidebarRow(
            agent: agent, threadID: "t", threads: [], preview: ThreadPreview(text: "hi"),
            timestamp: nil, unreadCount: unread)
    }

    func testAWaitingDeskGetsAnOrangeDotEvenWhenRead() {
        XCTAssertEqual(row(unread: 0, blocked: true).dot, .waiting)
        XCTAssertEqual(row(state: .needsYou, unread: 2).dot, .waiting)
    }

    func testUnreadIsABlueDotAndReadIsNone() {
        XCTAssertEqual(row(unread: 3).dot, .unread)
        XCTAssertEqual(row(unread: 0).dot, .none)
    }
}
