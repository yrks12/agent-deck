import XCTest
@testable import DeckKit

/// **A desk's proposal on the lock screen** (§23, the needs-you item): the
/// alert carries its policy, its notification offers Approve / No, and each
/// button answers the policy through the owner's routes.
final class StandingAlertTests: XCTestCase {
    private let alertJSON = #"""
    {"id":"push:1","kind":"needs_you","source":"standing","agent":"scout","thread_id":"direct:scout",
     "card_id":"sa_3f9a1c2e","policy_id":"sa_3f9a1c2e","title":"scout needs you",
     "body":"Always, up to a limit? scout: send email, 80/day, to @acme.com",
     "approve_route":"/v1/standing-approvals/sa_3f9a1c2e/approve",
     "revoke_route":"/v1/standing-approvals/sa_3f9a1c2e","urgent":false,"count":1,"ts":1790000040.0,"cursor":"c1"}
    """#

    private func alert() throws -> OwnerAlert {
        try DeckCoding.decoder.decode(OwnerAlert.self, from: Data(alertJSON.utf8))
    }

    func testTheAlertCarriesItsPolicyAndTheBannerOffersApproveAndNo() throws {
        let a = try alert()
        XCTAssertEqual(a.policyID, "sa_3f9a1c2e")
        let text = AlertNotificationText(a)
        XCTAssertEqual(text.categoryIdentifier, StandingAlertAnswer.categoryIdentifier)
        XCTAssertEqual(text.userInfo["deck_policy"], "sa_3f9a1c2e")
        XCTAssertTrue(text.timeSensitive)
    }

    func testAnotherAlertOffersNoStandingButtons() {
        let plain = OwnerAlert(id: "p", kind: .needsYou, source: "approval", agent: "scout", threadID: "direct:scout",
                               cardID: "ask_1", title: "t", body: "b", ts: 0, cursor: "c")
        XCTAssertNotEqual(AlertNotificationText(plain).categoryIdentifier, StandingAlertAnswer.categoryIdentifier)
    }

    func testEachButtonIsReadBack() throws {
        let info = AlertNotificationText(try alert()).userInfo
        XCTAssertEqual(StandingAlertAnswer(actionIdentifier: StandingAlertAnswer.approveAction, userInfo: info),
                       StandingAlertAnswer(policyID: "sa_3f9a1c2e", verb: .approve))
        XCTAssertEqual(StandingAlertAnswer(actionIdentifier: StandingAlertAnswer.revokeAction, userInfo: info)?.verb, .revoke)
        XCTAssertNil(StandingAlertAnswer(actionIdentifier: "com.apple.UNNotificationDefaultActionIdentifier", userInfo: info))
        XCTAssertNil(StandingAlertAnswer(actionIdentifier: StandingAlertAnswer.approveAction, userInfo: [:]))
    }

    func testApproveAndNoUseTheOwnersRoutesAndASettledOneIsNotAnError() async {
        let stub = StubStanding()
        let yes = await StandingAlertAnswer(policyID: "sa_1", verb: .approve).send(via: stub)
        let no = await StandingAlertAnswer(policyID: "sa_2", verb: .revoke).send(via: stub)
        XCTAssertNil(yes); XCTAssertNil(no)
        XCTAssertEqual(stub.calls, ["approve sa_1", "revoke sa_2"])
        stub.failure = .standingRefused(.notProposed, detail: "")
        let settled = await StandingAlertAnswer(policyID: "sa_1", verb: .approve).send(via: stub)
        XCTAssertNil(settled, "already settled elsewhere: nothing left to do")
        stub.failure = .unauthorized
        let refused = await StandingAlertAnswer(policyID: "sa_1", verb: .approve).send(via: stub)
        XCTAssertEqual(refused, DeckError.unauthorized.userFacingText)
    }

    func testBothAppsAnswerTheButtons() throws {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        for path in ["macos/Sources/DeckUI/DeckStore.swift", "ios/AgentDeckPhone/PhoneNotifications.swift"] {
            let text = try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
            XCTAssertTrue(text.contains("router.onStanding"), "\(path) never answers Approve / No")
        }
    }
}
