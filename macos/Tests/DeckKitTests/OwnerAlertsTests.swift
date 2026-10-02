import XCTest
@testable import DeckKit

/// **"Notify me when they need me and when they send messages intended to me."**
///
/// The deck decides WHAT may notify him (`server/owner_alerts.py`, served at
/// `GET /v1/owner/alerts`). What the apps own, and what is tested here:
///
/// 1. Reading that page, and asking for it with the cursor.
/// 2. **Dedupe**: the same alert never posts twice — not on a re-poll, not when
///    the background refresh and the foreground poll both see it, and not at
///    all on a first run (a fresh install is not buzzed with history).
/// 3. Not buzzing him about the thread he is looking at.
/// 4. **Routing a tap**: a notification (or an ntfy click link) opens that
///    desk's thread, with the card when there is one.
final class OwnerAlertsTests: XCTestCase {

    static let page = Data(#"""
    {"alerts":[
      {"id":"msg:a1","kind":"for_you","source":"message","agent":"atlas",
       "thread_id":"direct:atlas","card_id":"","message_id":"a1","urgent":false,
       "title":"Atlas","body":"Deployed. /healthz is 200.","ts":1790790000.5,
       "cursor":"001790790000500000-a1"},
      {"id":"ask:k7x2p","kind":"needs_you","source":"approval","agent":"scout",
       "thread_id":"direct:scout","card_id":"k7x2p","message_id":"","urgent":false,
       "title":"Scout needs you","body":"Run a command: make deploy","ts":1790790001,
       "cursor":"001790790001000000-approval-k7x2p"}],
     "next_since":"001790790001000000-approval-k7x2p","generated_at":1790790002}
    """#.utf8)

    private func decoded() throws -> OwnerAlertPage {
        try DeckCoding.decoder.decode(OwnerAlertPage.self, from: Self.page)
    }

    // MARK: 1. the page

    func testThePageDecodesWithEverythingANotificationNeeds() throws {
        let page = try decoded()
        XCTAssertEqual(page.nextSince, "001790790001000000-approval-k7x2p")
        XCTAssertEqual(page.alerts.map(\.id), ["msg:a1", "ask:k7x2p"])
        let ask = page.alerts[1]
        XCTAssertEqual(ask.kind, .needsYou)
        XCTAssertEqual(ask.threadID, "direct:scout")
        XCTAssertEqual(ask.cardID, "k7x2p")
        XCTAssertEqual(ask.title, "Scout needs you")
    }

    func testTheClientAsksWithTheCursor() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Self.page
        let store = InMemoryTokenStore()
        try store.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store,
                                    performer: performer)

        let page = try await client.ownerAlerts(since: "001790790000500000-a1")

        XCTAssertEqual(page.alerts.count, 2)
        let url = try XCTUnwrap(performer.requests.first?.url)
        XCTAssertEqual(url.path, "/v1/owner/alerts")
        XCTAssertEqual(URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "since" }?.value, "001790790000500000-a1")

        _ = try await client.ownerAlerts(since: nil)
        let first = try XCTUnwrap(performer.requests.last?.url)
        XCTAssertNil(URLComponents(url: first, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "since" }, "a first poll sends no cursor")
    }

    // MARK: 2. dedupe

    func testAFirstRunPostsNothingAndStartsAtTheHead() throws {
        var ledger = AlertLedger()
        XCTAssertEqual(ledger.take(try decoded(), openThread: nil), [])
        XCTAssertEqual(ledger.since, "001790790001000000-approval-k7x2p")
    }

    func testEachAlertPostsOnceEvenIfTheDeckSendsItAgain() throws {
        var ledger = AlertLedger(since: "000000000000000000-0")
        let posted = ledger.take(try decoded(), openThread: nil)
        XCTAssertEqual(posted.map(\.id), ["msg:a1", "ask:k7x2p"])
        XCTAssertEqual(ledger.since, "001790790001000000-approval-k7x2p")

        // The background task and the foreground poll racing, or an older
        // cursor restored: the same page again posts nothing.
        XCTAssertEqual(ledger.take(try decoded(), openThread: nil), [])
    }

    func testTheLedgerSurvivesARelaunch() throws {
        var ledger = AlertLedger(since: "000000000000000000-0")
        _ = ledger.take(try decoded(), openThread: nil)
        let saved = try JSONEncoder().encode(ledger)
        var again = try JSONDecoder().decode(AlertLedger.self, from: saved)
        XCTAssertEqual(again.take(try decoded(), openThread: nil), [])
    }

    func testTheRememberedIDsAreBounded() {
        var ledger = AlertLedger(since: "0")
        for n in 0..<(AlertLedger.memory + 50) {
            let alert = OwnerAlert(id: "msg:\(n)", kind: .forYou, source: "message", agent: "atlas",
                                   threadID: "direct:atlas", cardID: "", title: "Atlas", body: "x",
                                   ts: Double(n), cursor: String(format: "%018d-%d", n, n))
            _ = ledger.take(OwnerAlertPage(alerts: [alert], nextSince: alert.cursor), openThread: nil)
        }
        XCTAssertEqual(ledger.posted.count, AlertLedger.memory)
        XCTAssertEqual(ledger.posted.last, "msg:\(AlertLedger.memory + 49)")
    }

    // MARK: 3. not the thread he is reading

    func testTheThreadHeIsReadingDoesNotBuzzButIsStillCountedAsTold() throws {
        var ledger = AlertLedger(since: "000000000000000000-0")
        let posted = ledger.take(try decoded(), openThread: "direct:atlas")
        XCTAssertEqual(posted.map(\.id), ["ask:k7x2p"])
        XCTAssertTrue(ledger.posted.contains("msg:a1"),
                      "leaving the thread must not replay what he already read there")
    }

    // MARK: 4. a tap goes to the right thread

    func testANotificationCarriesItsRouteAndReadsItBack() throws {
        let ask = try decoded().alerts[1]
        let text = AlertNotificationText(ask)
        XCTAssertEqual(text.identifier, "ask:k7x2p", "the system also dedupes on this id")
        XCTAssertEqual(text.threadIdentifier, "direct:scout")
        XCTAssertEqual(text.title, "Scout needs you")
        XCTAssertEqual(text.body, "Run a command: make deploy")
        XCTAssertTrue(text.timeSensitive, "a thing that needs him breaks through a focus summary")
        XCTAssertFalse(AlertNotificationText(try decoded().alerts[0]).timeSensitive)

        let route = try XCTUnwrap(AlertRoute(userInfo: text.userInfo))
        XCTAssertEqual(route, AlertRoute(threadID: "direct:scout", agent: "scout", cardID: "k7x2p"))
    }

    func testAnNtfyClickLinkOpensTheSameThread() throws {
        let url = try XCTUnwrap(URL(string:
            "agentdeck://open?thread=direct%3Aatlas&card=dec_1&alert=decision%3Adec_1"))
        XCTAssertEqual(AlertRoute(url: url),
                       AlertRoute(threadID: "direct:atlas", agent: "atlas", cardID: "dec_1"))
        XCTAssertEqual(AlertRoute(url: URL(string: "agentdeck://open?thread=direct%3Aatlas")!)?.cardID, nil)
    }

    func testALinkThatIsNotOursGoesNowhere() {
        XCTAssertNil(AlertRoute(url: URL(string: "https://open?thread=direct%3Aatlas")!))
        XCTAssertNil(AlertRoute(url: URL(string: "agentdeck://settings?thread=direct%3Aatlas")!))
        XCTAssertNil(AlertRoute(url: URL(string: "agentdeck://open")!))
        XCTAssertNil(AlertRoute(userInfo: ["something": "else"]))
    }

    // MARK: 5. one policy, decided on the deck (owner complaint: too many)

    func testThePageSaysWhetherNtfyDeliversAndAnOldDeckReadsAsNo() throws {
        XCTAssertFalse(try decoded().ntfy, "a deck without the field does not deliver by ntfy")
        let withNtfy = Data(#"{"alerts":[],"next_since":"0","ntfy":true}"#.utf8)
        XCTAssertTrue(try DeckCoding.decoder.decode(OwnerAlertPage.self, from: withNtfy).ntfy)
    }

    func testThePhoneDoesNotBuzzTwiceWhenNtfyAlreadyDelivers() throws {
        let page = try decoded()
        let viaNtfy = OwnerAlertPage(alerts: page.alerts, nextSince: page.nextSince, ntfy: true)
        var ledger = AlertLedger(since: "000000000000000000-0")
        XCTAssertEqual(ledger.take(viaNtfy, openThread: nil, deferToNtfy: true), [],
                       "ntfy already buzzed this phone")
        XCTAssertEqual(ledger.take(page, openThread: nil), [], "and it is counted as told")

        // The good signal: the Mac, which ntfy does not reach, still posts.
        var mac = AlertLedger(since: "000000000000000000-0")
        XCTAssertEqual(mac.take(viaNtfy, openThread: nil).map(\.id), ["msg:a1", "ask:k7x2p"])
    }

    func testAnAppInFrontSaysSoSoTheDeckHoldsPushes() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Self.page
        let store = InMemoryTokenStore()
        try store.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store,
                                    performer: performer)
        _ = try await client.ownerAlerts(since: "0", active: true)
        _ = try await client.ownerAlerts(since: "0", active: false)
        func active(_ r: URLRequest) -> String? {
            URLComponents(url: r.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "active" }?.value
        }
        XCTAssertEqual(active(performer.requests[0]), "1")
        XCTAssertNil(active(performer.requests[1]))
    }
}
