import XCTest
import UserNotifications
@testable import DeckKit

/// **The grant card and its macOS notification say the same words and map the
/// same three taps.** One place holds the copy (`MacGrantCopy`) and one holds
/// the notification's actions (`MacGrantNotifications`), so the window card and
/// the banner cannot drift apart, and a tap on either becomes the same
/// `MacGrantDecision` the policy already understands.
final class MacUIGrantNotificationsTests: XCTestCase {
    private func request(desk: String = "atlas", summary: String = "sw_vers",
                         folders: [String] = ["~/Agent Deck Workspace"], unconfined: Bool = false) -> MacGrantRequest {
        MacGrantRequest(jobId: "mj_1", desk: desk, summary: summary, folders: folders, unconfined: unconfined)
    }

    func testTheTitleNamesTheDesk() {
        XCTAssertEqual(MacGrantCopy.title(desk: "atlas"), "Atlas wants to use this Mac")
        XCTAssertEqual(MacGrantCopy.title(desk: "Nova"), "Nova wants to use this Mac")
    }

    func testTheThreeButtonsAreThePlansWords() {
        XCTAssertEqual(MacGrantCopy.allowHour, "Allow for 1 hour")
        XCTAssertEqual(MacGrantCopy.alwaysAllow, "Always allow this desk")
        XCTAssertEqual(MacGrantCopy.deny, "Deny")
    }

    func testWhatAGrantMeansNamesTheFolders() {
        XCTAssertEqual(MacGrantCopy.scope(folders: ["~/Agent Deck Workspace"]),
                       "It can run commands as you and change files in: Agent Deck Workspace")
        XCTAssertEqual(MacGrantCopy.scope(folders: ["/Users/y/Projects/acme", "~/Notes"]),
                       "It can run commands as you and change files in: acme, Notes")
        XCTAssertTrue(MacGrantCopy.scope(folders: []).contains("No folder is chosen yet"),
                      "with no folder the card must say so instead of promising a scope it does not have")
        XCTAssertTrue(MacGrantCopy.suggestedFolder.hasSuffix("Agent Deck Workspace"))
    }

    func testAnUnfencedGrantIsSaidPlainly() {
        XCTAssertFalse(MacGrantCopy.unconfinedWarning.isEmpty)
        XCTAssertFalse(MacGrantCopy.unconfinedWarning.lowercased().contains("seatbelt"),
                       "no jargon on the card")
    }

    func testTheFirstRequestIsBoundedAndNeverEmpty() {
        XCTAssertEqual(MacGrantCopy.firstRequest("sw_vers"), "sw_vers")
        XCTAssertEqual(MacGrantCopy.firstRequest("   "), "Something on this Mac")
        let long = String(repeating: "x", count: 900)
        XCTAssertLessThanOrEqual(MacGrantCopy.firstRequest(long).count, 201)
        XCTAssertTrue(MacGrantCopy.firstRequest(long).hasSuffix("…"))
    }

    func testEachActionIsOneDecision() {
        typealias N = MacGrantNotifications
        XCTAssertEqual(N.decision(forAction: N.Action.hour.rawValue), .hour)
        XCTAssertEqual(N.decision(forAction: N.Action.always.rawValue), .always)
        XCTAssertEqual(N.decision(forAction: N.Action.deny.rawValue), .deny)
        XCTAssertNil(N.decision(forAction: UNNotificationDefaultActionIdentifier),
                     "clicking the banner itself opens the card; it must not grant or deny")
        XCTAssertNil(N.decision(forAction: UNNotificationDismissActionIdentifier),
                     "swiping the banner away is not a Deny")
    }

    func testTheCategoryCarriesThreeActionsInOrderWithDenyDestructive() throws {
        let category = MacGrantNotifications.category()
        XCTAssertEqual(category.identifier, MacGrantNotifications.categoryIdentifier)
        XCTAssertEqual(category.actions.map(\.title),
                       [MacGrantCopy.allowHour, MacGrantCopy.alwaysAllow, MacGrantCopy.deny])
        XCTAssertEqual(category.actions.map(\.identifier), MacGrantNotifications.Action.allCases.map(\.rawValue))
        XCTAssertTrue(try XCTUnwrap(category.actions.last).options.contains(.destructive))
        XCTAssertFalse(category.actions.contains { $0.options.contains(.foreground) },
                       "answering from the banner must not pull the app to the front")
    }

    func testTheBannerSaysWhoAndWhatAndCarriesTheDesk() {
        let content = MacGrantNotifications.content(for: request())
        XCTAssertEqual(content.title, "Atlas wants to use this Mac")
        XCTAssertEqual(content.body, "sw_vers")
        XCTAssertEqual(content.categoryIdentifier, MacGrantNotifications.categoryIdentifier)
        XCTAssertEqual(MacGrantNotifications.desk(from: content.userInfo), "atlas")
    }

    func testOneBannerPerDesk() {
        XCTAssertEqual(MacGrantNotifications.identifier(desk: "atlas"), MacGrantNotifications.identifier(desk: "atlas"))
        XCTAssertNotEqual(MacGrantNotifications.identifier(desk: "atlas"), MacGrantNotifications.identifier(desk: "nova"))
    }

    func testAForeignNotificationIsNotOurs() {
        XCTAssertNil(MacGrantNotifications.desk(from: ["something": "else"]))
        XCTAssertNil(MacGrantNotifications.desk(from: [:]))
    }
}
