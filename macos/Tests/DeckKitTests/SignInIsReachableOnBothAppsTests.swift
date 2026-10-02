import XCTest

/// **The sign-in surfaces are mounted where he looks**, on both apps, from the
/// one shared view file. A button nobody mounts passes every other test.
final class SignInIsReachableOnBothAppsTests: XCTestCase {

    private var repo: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private func source(_ path: String) throws -> String {
        try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheSharedSheetAndAccountListAreAlsoCompiledIntoThePhone() throws {
        let views = try source("macos/Sources/DeckUI/AccountSignInViews.swift")
        for name in ["AccountSignInSheet", "ClaudeAccountsList"] {
            XCTAssertTrue(views.contains("struct \(name)"), name)
        }
        XCTAssertTrue(try source("ios/project.yml").contains("../macos/Sources/DeckUI/AccountSignInViews.swift"))
    }

    func testTheExpiryBannerHasASignInAgainButton() throws {
        let views = try source("macos/Sources/DeckUI/AccountViews.swift")
        XCTAssertTrue(views.contains("onSignIn"))
        XCTAssertTrue(views.contains("Sign in again"))
    }

    func testTheMacMountsTheBannerButtonTheSettingsListAndTheSheet() throws {
        let sidebar = try source("macos/Sources/DeckUI/SidebarView.swift")
        XCTAssertTrue(sidebar.contains("AccountSignInSheet("), "no sheet on the Mac")
        XCTAssertTrue(sidebar.contains("onSignIn:"), "the banner has no button on the Mac")
        let settings = try source("macos/Sources/DeckUI/SettingsPanelView.swift")
        XCTAssertTrue(settings.contains("ClaudeAccountsList("), "no Claude accounts section in Settings")
    }

    func testThePhoneMountsTheBannerButtonTheListAndTheSheet() throws {
        let roster = try source("ios/AgentDeckPhone/RosterView.swift")
        XCTAssertTrue(roster.contains("AccountSignInSheet("))
        XCTAssertTrue(roster.contains("ClaudeAccountsList("))
        XCTAssertTrue(roster.contains("onSignIn:"))
    }

    func testTheBrowserOpensWithTheSystemOpenerOnEachPlatform() throws {
        XCTAssertTrue(try source("macos/Sources/DeckUI/AccountSignInViews.swift").contains("NSWorkspace.shared.open"))
        XCTAssertTrue(try source("macos/Sources/DeckUI/AccountSignInViews.swift").contains("UIApplication.shared.open"))
    }
}
