import XCTest
@testable import DeckKit

/// **The Mac's HTTP to MB1: right route, both credentials, secret kept safe.**
///
/// The node routes need the `/v1` bearer *and* `X-Deck-Node:
/// <node_id>.<node_secret>`; a missing header is `401 unknown_node` and the
/// loop would spin re-pairing. The secret arrives once (201) and must land in
/// the Keychain — through the StateSuite-aware account, so a test build or a
/// pairing run can never overwrite the owner's real node secret.
final class MacBridgeNodeClientTests: XCTestCase {

    final class Performer: RequestPerformer, @unchecked Sendable {
        let lock = NSLock()
        var requests: [URLRequest] = []
        var replies: [(Int, String)] = []
        var fallback: (Int, String) = (200, "{}")

        func reply(_ status: Int, _ body: String) { lock.withLock { replies.append((status, body)) } }

        func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
            let (status, body) = lock.withLock { () -> (Int, String) in
                requests.append(request)
                return replies.isEmpty ? fallback : replies.removeFirst()
            }
            let r = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (Data(body.utf8), r)
        }

        var last: URLRequest? { lock.withLock { requests.last } }
        func json(_ i: Int) throws -> [String: Any] {
            let body = try XCTUnwrap(lock.withLock { requests[i].httpBody })
            return try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        }
    }

    var performer: Performer!
    var secrets: InMemoryTokenStore!
    var client: MacNodeClient!

    override func setUp() {
        performer = Performer()
        secrets = InMemoryTokenStore()
        client = MacNodeClient(deckURL: URL(string: "http://10.99.0.1:7789")!,
                               tokens: InMemoryTokenStore(token: "bearer-1"), secrets: secrets, performer: performer)
    }

    private let register = MacRegisterRequest(machineId: "M-1", name: "Mac", os: "macOS 26.0", appVersion: "1.5.0",
                                              capabilities: [.run], mode: .ask)

    func testFirstRegisterSendsNoNodeHeaderAndStoresTheSecret() async throws {
        performer.reply(201, #"{"node_id":"mac_aaaaaaaaaaaa","node_secret":"S3CRET","name":"Mac","primary":true}"#)
        let r = try await client.register(register)
        XCTAssertEqual(r.nodeId, "mac_aaaaaaaaaaaa")
        let req = try XCTUnwrap(performer.last)
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.url?.absoluteString, "http://10.99.0.1:7789/v1/nodes")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer bearer-1")
        XCTAssertNil(req.value(forHTTPHeaderField: "X-Deck-Node"))
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(try performer.json(0)["machine_id"] as? String, "M-1")
        XCTAssertEqual(try secrets.token(), "mac_aaaaaaaaaaaa.S3CRET")
    }

    func testRefreshPresentsTheHeaderAndKeepsTheSecret() async throws {
        try secrets.setToken("mac_aaaaaaaaaaaa.S3CRET")
        performer.reply(200, #"{"node_id":"mac_aaaaaaaaaaaa","name":"Mac","primary":true}"#)
        _ = try await client.register(register)
        XCTAssertEqual(performer.last?.value(forHTTPHeaderField: "X-Deck-Node"), "mac_aaaaaaaaaaaa.S3CRET")
        XCTAssertEqual(try secrets.token(), "mac_aaaaaaaaaaaa.S3CRET")
    }

    /// Re-pair: the server hands a new secret; the old one stops working.
    func testRePairReplacesTheSecret() async throws {
        try secrets.setToken("mac_aaaaaaaaaaaa.OLD")
        performer.reply(201, #"{"node_id":"mac_aaaaaaaaaaaa","node_secret":"NEW","name":"Mac","primary":false}"#)
        _ = try await client.register(register)
        XCTAssertEqual(try secrets.token(), "mac_aaaaaaaaaaaa.NEW")
    }

    func testPollEventsAndGrantHitMB1RoutesWithBothCredentials() async throws {
        try secrets.setToken("mac_aaaaaaaaaaaa.S3CRET")
        performer.reply(200, #"{"jobs":[],"cancel":["mj_000000000001"],"server_ts":5}"#)
        let polled = try await client.poll(nodeId: "mac_aaaaaaaaaaaa",
                                           MacPollRequest(wait: 25, freeSlots: 4, running: [], mode: .ask, grants: [:]))
        XCTAssertEqual(polled.cancel, ["mj_000000000001"])

        performer.reply(200, #"{"ok":true,"cancel":true}"#)
        let ev = try await client.postEvents(nodeId: "mac_aaaaaaaaaaaa", jobId: "mj_000000000001", [])
        XCTAssertTrue(ev.cancel)

        performer.reply(200, #"{"ok":true,"told":true}"#)
        let g = try await client.postGrant(nodeId: "mac_aaaaaaaaaaaa", MacGrantPost(desk: "atlas", decision: .hour, until: 9))
        XCTAssertTrue(g.told)

        let urls = performer.requests.map { $0.url?.absoluteString ?? "" }
        XCTAssertEqual(urls, [
            "http://10.99.0.1:7789/v1/nodes/mac_aaaaaaaaaaaa/poll",
            "http://10.99.0.1:7789/v1/nodes/mac_aaaaaaaaaaaa/jobs/mj_000000000001/events",
            "http://10.99.0.1:7789/v1/nodes/mac_aaaaaaaaaaaa/grants",
        ])
        for r in performer.requests {
            XCTAssertEqual(r.httpMethod, "POST")
            XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer bearer-1")
            XCTAssertEqual(r.value(forHTTPHeaderField: "X-Deck-Node"), "mac_aaaaaaaaaaaa.S3CRET")
        }
        XCTAssertEqual((try performer.json(1)["events"] as? [Any])?.count, 0)   // an empty list is a keepalive
    }

    /// The long-poll must outlive the server's 25 s wait.
    func testPollRequestTimeoutOutlivesTheLongPoll() async throws {
        try secrets.setToken("mac_aaaaaaaaaaaa.S")
        performer.reply(200, #"{"jobs":[],"cancel":[],"server_ts":1}"#)
        _ = try await client.poll(nodeId: "mac_aaaaaaaaaaaa", MacPollRequest(wait: 25, freeSlots: 4, running: [], mode: .ask, grants: [:]))
        XCTAssertGreaterThanOrEqual(performer.last?.timeoutInterval ?? 0, 40)
        XCTAssertEqual(MacNodeClient.sessionConfiguration().timeoutIntervalForRequest, 40)
    }

    func testRefusalsCarryTheServersReasonAndDetail() async throws {
        try secrets.setToken("mac_aaaaaaaaaaaa.S")
        performer.reply(401, #"{"ok":false,"reason":"unknown_node","detail":"X-Deck-Node is not this Mac's"}"#)
        do {
            _ = try await client.poll(nodeId: "mac_aaaaaaaaaaaa", MacPollRequest(wait: 0, freeSlots: 0, running: [], mode: .ask, grants: [:]))
            XCTFail("a 401 must throw")
        } catch let e as MacNodeError {
            XCTAssertEqual(e, .refused(status: 401, reason: "unknown_node", detail: "X-Deck-Node is not this Mac's"))
            XCTAssertTrue(e.isUnknownNode)
            XCTAssertFalse(e.isServerTrouble)
        }
        performer.reply(503, "<html>bad gateway</html>")
        do {
            _ = try await client.postEvents(nodeId: "mac_aaaaaaaaaaaa", jobId: "mj_1", [])
            XCTFail("a 503 must throw")
        } catch let e as MacNodeError {
            XCTAssertEqual(e, .refused(status: 503, reason: "http_503", detail: ""))
            XCTAssertTrue(e.isServerTrouble)
        }
    }

    func testNodeRoutesWithoutAPairingDoNotGoOut() async throws {
        do {
            _ = try await client.poll(nodeId: "mac_aaaaaaaaaaaa", MacPollRequest(wait: 0, freeSlots: 0, running: [], mode: .ask, grants: [:]))
            XCTFail("unpaired must throw")
        } catch let e as MacNodeError {
            XCTAssertEqual(e, .notPaired)
        }
        XCTAssertTrue(performer.requests.isEmpty)
    }

    func testNoBearerTokenIsRefusedLocally() async throws {
        let c = MacNodeClient(deckURL: URL(string: "http://10.99.0.1:7789")!, tokens: InMemoryTokenStore(),
                              secrets: secrets, performer: performer)
        do { _ = try await c.register(register); XCTFail("no token must throw") } catch let e as MacNodeError {
            XCTAssertEqual(e, .missingToken)
        }
        XCTAssertTrue(performer.requests.isEmpty)
    }

    func testBasePathIsKept() async throws {
        let c = MacNodeClient(deckURL: URL(string: "https://deck.example.com/agent-deck/")!,
                              tokens: InMemoryTokenStore(token: "t"), secrets: secrets, performer: performer)
        performer.reply(201, #"{"node_id":"mac_b","node_secret":"x","name":"Mac","primary":true}"#)
        _ = try await c.register(register)
        XCTAssertEqual(performer.last?.url?.absoluteString, "https://deck.example.com/agent-deck/v1/nodes")
    }

    // MARK: where the secret and the machine id live

    func testSecretAccountFollowsTheStateSuite() {
        let real = StateSuite(environment: [:])
        XCTAssertEqual(real.macNodeSecretAccount, "mac-node-secret")
        let test = StateSuite(environment: ["DECK_STATE_SUITE": "pairing-test"])
        XCTAssertEqual(test.macNodeSecretAccount, "mac-node-secret.pairing-test")
        XCTAssertNotEqual(test.macNodeSecretAccount, real.macNodeSecretAccount)
    }

    func testMachineIdIsMadeOnceAndKept() throws {
        let dir = NSTemporaryDirectory() + "mac-node-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let first = MacNodeIdentity(directory: dir, suite: StateSuite(environment: [:]))
        let id = first.machineId()
        XCTAssertNotNil(UUID(uuidString: id))
        XCTAssertEqual(MacNodeIdentity(directory: dir, suite: StateSuite(environment: [:])).machineId(), id)
        XCTAssertTrue(FileManager.default.fileExists(atPath: dir + "/node.json"))
        // A test suite gets its own machine, so it never re-pairs the owner's.
        let other = MacNodeIdentity(directory: dir, suite: StateSuite(environment: ["DECK_STATE_SUITE": "t"])).machineId()
        XCTAssertNotEqual(other, id)
        XCTAssertTrue(FileManager.default.fileExists(atPath: dir + "/node.t.json"))
    }

    func testThisMacDescribesItselfWithinTheServersLimits() {
        let me = MacNodeIdentity.describe(name: "Sam's\nMacBook " + String(repeating: "x", count: 100))
        XCTAssertLessThanOrEqual(me.name.count, 64)
        XCTAssertFalse(me.name.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) })
        XCTAssertTrue(me.os.hasPrefix("macOS "), me.os)
    }
}
