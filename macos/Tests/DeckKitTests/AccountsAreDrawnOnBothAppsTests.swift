import XCTest

/// **The account surfaces exist on both apps, from one file.** The decisions
/// are `AccountsPresentation`'s (tested without a window); this pins that each
/// surface is actually mounted where he looks, the way
/// `TheMacDrawsTheUsageMeterTests` does for the plan meter: a view nobody
/// mounts passes every other test and shows him nothing.
final class AccountsAreDrawnOnBothAppsTests: XCTestCase {

    private var repo: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private func source(_ path: String) throws -> String {
        try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheSharedAccountViewsAreCompiledIntoThePhoneToo() throws {
        let project = try source("ios/project.yml")
        XCTAssertTrue(project.contains("../macos/Sources/DeckUI/AccountViews.swift"))
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: repo.appendingPathComponent("ios/AgentDeckPhone/AccountViews.swift").path),
            "the phone keeps its own copy")
    }

    func testTheMacSidebarMountsTheBadgeTheMoveMenuAndTheExpiryBanner() throws {
        let sidebar = try source("macos/Sources/DeckUI/SidebarView.swift")
        for view in ["MoveToAccountMenu(", "AccountExpiryBanner(", "accountBadge:"] {
            XCTAssertTrue(sidebar.contains(view), "the Mac sidebar does not mount \(view)")
        }
        XCTAssertTrue(sidebar.contains(".contextMenu"), "the move menu is a context menu on the Mac")
    }

    func testThePhoneRosterMountsTheSameThreeAndTheMenuIsALongPress() throws {
        let roster = try source("ios/AgentDeckPhone/RosterView.swift")
        for view in ["MoveToAccountMenu(", "AccountExpiryBanner(", "accountBadge:"] {
            XCTAssertTrue(roster.contains(view), "the phone roster does not mount \(view)")
        }
        XCTAssertTrue(roster.contains(".contextMenu"), "a long-press menu")
    }

    func testTheRowDrawsTheBadgeNextToTheTitle() throws {
        let row = try source("macos/Sources/DeckUI/RosterViews.swift")
        XCTAssertTrue(row.contains("accountBadge"))
        XCTAssertTrue(row.contains("AccountBadgeChip("))
    }

    func testTheMetersDrawOnePerAccountFromTheSharedPresentation() throws {
        let meter = try source("macos/Sources/DeckUI/UsageMeterView.swift")
        XCTAssertTrue(meter.contains("AccountsPresentation.meters("))
    }

    func testTheAutoSwitchIsInSettingsAndReadOnly() throws {
        let settings = try source("macos/Sources/DeckUI/SettingsPanelView.swift")
        XCTAssertTrue(settings.contains("AccountsPresentation.autoSwitch("))
    }
}
