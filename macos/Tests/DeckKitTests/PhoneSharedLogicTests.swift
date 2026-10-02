import XCTest
@testable import DeckKit

/// **The logic the iPhone app shares with the Mac, tested where it lives.**
///
/// Three things the phone needed that DeckKit did not have: typing a deck's
/// address and token by hand (no pairing code), the Claude usage meter
/// (`GET /v1/usage`, which may not be mounted — a 404 is "no meter", never an
/// error), and one "needs your attention" queue built from approvals and
/// handoffs. No socket, no Keychain: an in-memory token store and a private
/// defaults suite.
final class PhoneSharedLogicTests: XCTestCase {

    // MARK: manual entry

    func testABareHostAndPortBecomesAnHTTPAddress() throws {
        let entry = try ManualDeckEntry.parse(url: "  10.99.0.1:7789 ", token: " adt_x \n")
        XCTAssertEqual(entry.url.absoluteString, "http://10.99.0.1:7789")
        XCTAssertEqual(entry.token, "adt_x")
    }

    func testAPastedAPIPathAndTrailingSlashAreDropped() throws {
        XCTAssertEqual(try ManualDeckEntry.parse(url: "https://deck.example/v1/", token: "t").url.absoluteString,
                       "https://deck.example")
        XCTAssertEqual(try ManualDeckEntry.parse(url: "http://10.0.0.2:7788/", token: "t").url.absoluteString,
                       "http://10.0.0.2:7788")
    }

    func testNoAddressOrNoTokenIsRefusedWithAReason() {
        XCTAssertThrowsError(try ManualDeckEntry.parse(url: "  ", token: "t")) {
            XCTAssertEqual($0 as? ManualDeckEntry.Problem, .noAddress)
        }
        XCTAssertThrowsError(try ManualDeckEntry.parse(url: "10.0.0.1", token: "   ")) {
            XCTAssertEqual($0 as? ManualDeckEntry.Problem, .noToken)
        }
        XCTAssertThrowsError(try ManualDeckEntry.parse(url: "ftp://x", token: "t")) {
            XCTAssertEqual($0 as? ManualDeckEntry.Problem, .notAnAddress)
        }
        XCTAssertFalse(ManualDeckEntry.Problem.noToken.userFacingText.isEmpty)
    }

    func testSavingAManualEntryIsWhatTheLiveClientReadsBack() throws {
        let suite = "dev.agentdeck.app.test\(UUID().uuidString.prefix(8))"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("oldpin", forKey: DeckConnection.pinKey)
        let tokens = InMemoryTokenStore()
        let connection = DeckConnection(defaults: defaults, tokens: tokens)

        try connection.save(try ManualDeckEntry.parse(url: "10.99.0.1:7789", token: "adt_y"))

        XCTAssertEqual(try tokens.token(), "adt_y")
        XCTAssertEqual(connection.saved?.url.absoluteString, "http://10.99.0.1:7789")
        XCTAssertNil(connection.saved?.pin, "a hand-typed address is not pinned to an old key")
    }

    // MARK: usage meter

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    private static let usageBody = Data(#"""
    {"available":true,"stale":false,"reason":null,"plan":"max",
     "windows":[{"key":"weekly","label":"Weekly","percent":41.5,"severity":"normal","resets_at":"2026-10-02T09:00:00+00:00"},
                {"key":"session","label":"5-hour","percent":130,"severity":"warning","resets_at":1790800000}],
     "breakdown":null,"extra_usage":{"enabled":false,"spend_limit_reached":false},"other":[],
     "fetched_at":1790790000,"next_refresh_at":1790790300}
    """#.utf8)

    func testTheUsageMeterReadsItsWindowsSessionFirst() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Self.usageBody
        let usage = try await client(performer).usage()

        XCTAssertEqual(performer.paths, ["/v1/usage"])
        let meter = try XCTUnwrap(usage)
        XCTAssertTrue(meter.available)
        XCTAssertEqual(meter.plan, "max")
        XCTAssertEqual(meter.windows.map(\.key), ["session", "weekly"])
        XCTAssertEqual(meter.windows[0].fraction, 1, "over the limit draws a full bar, not an overflow")
        XCTAssertEqual(meter.windows[1].fraction, 0.415, accuracy: 0.0001)
        XCTAssertNotNil(meter.windows[0].resetsAt)
        XCTAssertNotNil(meter.windows[1].resetsAt, "ISO text and epoch seconds both read")
    }

    func testAnUnmountedUsageRouteIsNoMeterNotAnError() async throws {
        let performer = StubPerformer()
        performer.status = 404
        performer.defaultBody = Data(#"{"detail":"Not Found"}"#.utf8)
        let usage = try await client(performer).usage()
        XCTAssertNil(usage)
    }

    func testAnUnavailableMeterSaysSoRatherThanShowingZero() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(#"{"available":false,"stale":false,"reason":"no_credentials","windows":[]}"#.utf8)
        let meter = try await client(performer).usage()
        XCTAssertEqual(meter?.available, false)
        XCTAssertEqual(meter?.windows, [])
    }

    // MARK: attention queue

    func testApprovalsAndHandoffsFormOneQueueOldestFirst_sharedWithTheMacStrip() throws {
        let approvals = try JSONDecoder().decode(ApprovalsPage.self, from: Data(#"""
        {"approvals":[{"id":"a1","ts":300,"agent":"atlas","desk_known":true,"tool":"Bash","subject":"rm -rf build","cwd":"/w","status":"pending",
          "options":[{"reply":"once","available":true,"summary":"Just this once"}]}]}
        """#.utf8))
        let handoffs = try JSONDecoder().decode(HandoffsPage.self, from: Data(#"""
        {"handoffs":[{"id":"h1","ts":100,"agent":"acme","desk_known":true,"kind":"login","needs":"Sign in to the bank","state":"waiting",
          "where":"screen","evidence":"","status":"pending",
          "options":[{"reply":"done","available":true,"summary":"Done"},{"reply":"skipped","available":true,"summary":"Skip"}]}]}
        """#.utf8))

        let queue = AttentionItem.queue(approvals: approvals.approvals, handoffs: handoffs.handoffs, agents: [:])

        XCTAssertEqual(queue.map(\.askID), ["h1", "a1"])
        XCTAssertEqual(queue.map(\.deskName), ["acme", "atlas"])
        if case .permission(_, let subject) = queue[1].need { XCTAssertEqual(subject, "rm -rf build") }
        else { XCTFail("the approval is a permission question") }
    }
}
