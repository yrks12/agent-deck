import XCTest

/// **The Money screen is the Mac's view, compiled into the iPhone.** One file
/// draws it on both (like `UsageMeterView`), and the phone's roster menu opens it.
final class TheMoneyScreenIsOnThePhoneTests: XCTestCase {
    private func read(_ path: String) throws -> String {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheProjectCompilesTheSharedMoneyViewAndItsFlatLabel() throws {
        let project = try read("ios/project.yml")
        XCTAssertTrue(project.contains("../macos/Sources/DeckUI/MoneyView.swift"))
        XCTAssertTrue(project.contains("../macos/Sources/DeckUI/FlatLabel.swift"))
    }

    func testTheRosterMenuOpensMoney() throws {
        let roster = try read("ios/AgentDeckPhone/RosterView.swift")
        XCTAssertTrue(roster.contains("MoneyView("))
        XCTAssertTrue(roster.contains("\"Money\""))
        XCTAssertTrue(try read("ios/AgentDeckPhone/PhoneStore.swift").contains("moneyClient"))
    }
}
