import XCTest
@testable import DeckKit

/// **A desk calls him** (owner, 2026-10-01: "settings to let Atlas call us by
/// himself"). The deck lists what is ringing on the alerts page both apps
/// already poll; the app shows it, Answer opens the SAME live call as his own
/// with the desk's reason as its first spoken line, Decline hands the reason
/// to his thread. A tap on the ntfy push (`agentdeck://call?ring=…`) or on the
/// local notification opens the app into that call.
@MainActor
final class IncomingCallModelTests: XCTestCase {

    private static let ringJSON = #"""
    {"id":"ring_abc","agent":"atlas","thread_id":"direct:atlas","reason":"prod db is down",
     "urgent":true,"state":"ringing","created_at":100.0,"expires_at":130.0}
    """#

    private func ring(_ id: String = "ring_abc", expires: Double = 130) -> IncomingRing {
        IncomingRing(id: id, agent: "atlas", reason: "prod db is down", urgent: true,
                     createdAt: expires - 30, expiresAt: expires)
    }

    // MARK: the feed

    func testTheAlertsPageCarriesWhatIsRingingAndAnOldPageStillDecodes() throws {
        let page = try JSONDecoder().decode(OwnerAlertPage.self, from: Data(#"""
        {"alerts":[{"id":"ring:ring_abc","kind":"needs_you","source":"ring","agent":"atlas",
          "thread_id":"direct:atlas","card_id":"","ring_id":"ring_abc","title":"Atlas is calling — prod db is down",
          "body":"Tap to answer.","ts":100.0,"cursor":"c1","urgent":true}],
         "next_since":"c1","ntfy":true,"ringing":[\#(Self.ringJSON)]}
        """#.utf8))
        XCTAssertEqual(page.ringing, [ring()])
        XCTAssertEqual(page.alerts.first?.ringID, "ring_abc")
        let old = try JSONDecoder().decode(OwnerAlertPage.self, from: Data(#"{"alerts":[],"next_since":"c0"}"#.utf8))
        XCTAssertEqual(old.ringing, [], "a deck that predates desk calls rings nothing")
    }

    func testTappingTheRingsNotificationOrItsNtfyLinkOpensTheCall() throws {
        let alert = OwnerAlert(id: "ring:ring_abc", kind: .needsYou, source: "ring", agent: "atlas",
                               threadID: "direct:atlas", cardID: "", title: "Atlas is calling", body: "",
                               ts: 1, cursor: "c", urgent: true)
        var withRing = alert
        withRing.ringID = "ring_abc"
        let tap = try XCTUnwrap(AlertRoute(userInfo: AlertNotificationText(withRing).userInfo))
        XCTAssertEqual(tap.ringID, "ring_abc")
        XCTAssertEqual(tap.agent, "atlas")
        XCTAssertNil(AlertRoute(userInfo: AlertNotificationText(alert).userInfo)?.ringID,
                     "an ordinary alert opens its thread, not a call")

        let link = try XCTUnwrap(AlertRoute(url: URL(string: "agentdeck://call?ring=ring_abc&thread=direct%3Aatlas")!))
        XCTAssertEqual(link.ringID, "ring_abc")
        XCTAssertEqual(link.threadID, "direct:atlas")
        XCTAssertNil(AlertRoute(url: URL(string: "agentdeck://open?thread=direct%3Aatlas&ring=x")!)?.ringID)
    }

    // MARK: which ring the screen shows

    func testTheScreenShowsARingUntilItRunsOutLeavesTheFeedOrHeTapped() {
        var screen = IncomingCallPresenter()
        let at = { (t: Double) in Date(timeIntervalSince1970: t) }
        XCTAssertEqual(screen.update([ring()], now: at(101))?.id, "ring_abc")
        XCTAssertEqual(ring().secondsLeft(at: at(101)), 29)
        XCTAssertNil(screen.update([ring()], now: at(130)), "30 seconds and it stops ringing")
        XCTAssertEqual(screen.update([ring()], now: at(105))?.id, "ring_abc")
        XCTAssertNil(screen.update([], now: at(106)), "answered or declined elsewhere")
        screen.update([ring()], now: at(107))
        screen.handle("ring_abc")
        XCTAssertNil(screen.showing)
        XCTAssertNil(screen.update([ring()], now: at(108)), "a late page does not ring it again")
        XCTAssertEqual(screen.update([ring(), ring("ring_def", expires: 140)], now: at(108))?.id, "ring_def")
    }

    func testTheDecksDefaultIsOffAndAtlasOnly() {
        XCTAssertEqual(CallOwnerSettings.deckDefault.when, .off)
        XCTAssertEqual(CallOwnerSettings.deckDefault.who, .chief)
        XCTAssertEqual(CallOwnerSettings.When.allCases.map(\.label), ["Off", "Urgent only", "Anytime"])
        XCTAssertEqual(CallOwnerSettings.Who.allCases.map(\.label), ["Atlas only", "Any desk"])
    }

}
