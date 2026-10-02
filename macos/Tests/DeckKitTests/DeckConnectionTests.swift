import XCTest
@testable import DeckKit

/// Redeeming a pairing code (K3 `/v1/pair`) and what the app keeps afterwards
/// (K9). Everything runs against a private defaults suite and an in-memory
/// token store: nothing here can touch the owner's real connection.
final class DeckConnectionTests: XCTestCase {

    private var suiteName = ""
    private var defaults: UserDefaults!
    private var tokens: InMemoryTokenStore!

    override func setUp() {
        suiteName = "dev.agentdeck.app.test\(UUID().uuidString.prefix(8))"
        defaults = UserDefaults(suiteName: suiteName)
        tokens = InMemoryTokenStore()
    }

    override func tearDown() { defaults.removePersistentDomain(forName: suiteName) }

    private let okBody = #"{"token":"adt_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG","device_id":"d_1a2b3c4d","deck":{"name":"Dan's deck","version":"0.9.0"}}"#

    private func code(tls: String = "ca", url: String = "https://203.0.113.7", exp: Int? = nil, pin: String? = nil) throws -> PairingCode {
        var obj: [String: Any] = ["v": 1, "url": url, "code": "q7Zb0cV2l8RkT1xYw3HnAg",
                                  "exp": exp ?? Int(Date().timeIntervalSince1970) + 900,
                                  "tls": tls, "name": "Dan's deck"]
        if let pin { obj["pin"] = pin }
        let data = try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
        let b64 = data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        return try PairingCode.parse("ADK1." + b64)
    }

    private func connection(_ spy: SpyPerformer) -> DeckConnection {
        DeckConnection(defaults: defaults, tokens: tokens, performerFor: { _ in (spy, nil) })
    }

    // MARK: success

    func testRedeemPostsTheCodeAndDeviceWithoutAnyBearer() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        _ = try await connection(spy).pair(code: try code(), device: "Dan's MacBook")
        let req = try XCTUnwrap(spy.requests.first)
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.url?.absoluteString, "https://203.0.113.7/v1/pair")
        XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(req.httpBody)) as? [String: String]
        XCTAssertEqual(body, ["code": "q7Zb0cV2l8RkT1xYw3HnAg", "device": "Dan's MacBook"])
    }

    func testTheDeviceNameIsCappedAtSixtyCharacters() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        _ = try await connection(spy).pair(code: try code(), device: String(repeating: "x", count: 90))
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(spy.requests.first?.httpBody)) as? [String: String]
        XCTAssertEqual(body?["device"]?.count, 60)
    }

    func testSuccessKeepsUrlNameAndTokenAndOnlyThose() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        let paired = try await connection(spy).pair(code: try code(), device: "Mac")
        XCTAssertEqual(paired.deviceID, "d_1a2b3c4d")
        XCTAssertEqual(paired.version, "0.9.0")
        XCTAssertEqual(defaults.string(forKey: "deckBaseURL"), "https://203.0.113.7")
        XCTAssertEqual(defaults.string(forKey: "deckTLSPin"), "")
        XCTAssertEqual(defaults.string(forKey: "deckName"), "Dan's deck")
        XCTAssertEqual(try tokens.token(), "adt_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")
        // The token lives in the Keychain, the one-time code nowhere.
        let dump = defaults.dictionaryRepresentation().map { "\($0)=\($1)" }.joined()
        XCTAssertFalse(dump.contains("adt_"))
        XCTAssertFalse(dump.contains("q7Zb0cV2l8RkT1xYw3HnAg"))
    }

    func testPinModeStoresThePin() async throws {
        let pin = LocalTLSServer.pin
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        _ = try await connection(spy).pair(code: try code(tls: "pin", pin: pin), device: "Mac")
        XCTAssertEqual(defaults.string(forKey: "deckTLSPin"), pin)
    }

    func testSuccessAnnouncesNewCredentials() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        let heard = expectation(forNotification: .deckCredentialsChanged, object: nil)
        _ = try await connection(spy).pair(code: try code(), device: "Mac")
        await fulfillment(of: [heard], timeout: 2)
    }

    // MARK: refusals (each one is its own sentence, and nothing is saved)

    private func refusal(_ status: Int, _ reason: String, headers: [String: String] = [:]) async -> (PairingFailure?, DeckConnection) {
        let spy = SpyPerformer { _ in (status, Data(#"{"error":"\#(reason)","detail":"server sentence"}"#.utf8), headers) }
        let c = connection(spy)
        do {
            _ = try await c.pair(code: try code(), device: "Mac")
            return (nil, c)
        } catch { return (error as? PairingFailure, c) }
    }

    func testEachServerRefusalHasItsOwnOutcome() async {
        var (f, _) = await refusal(400, "pair_malformed"); XCTAssertEqual(f, .malformed)
        (f, _) = await refusal(401, "pair_unknown"); XCTAssertEqual(f, .unknownCode)
        (f, _) = await refusal(410, "pair_expired"); XCTAssertEqual(f, .expired)
        (f, _) = await refusal(409, "pair_used"); XCTAssertEqual(f, .alreadyUsed)
        (f, _) = await refusal(429, "rate_limited", headers: ["Retry-After": "120"]); XCTAssertEqual(f, .rateLimited(retryAfter: 120))
        (f, _) = await refusal(429, "rate_limited"); XCTAssertEqual(f, .rateLimited(retryAfter: nil))
        (f, _) = await refusal(500, "boom"); XCTAssertEqual(f, .refused(status: 500, reason: "boom"))
    }

    func testEveryFailureReadsAsAPlainSentence() async {
        for (s, r) in [(400, "pair_malformed"), (401, "pair_unknown"), (410, "pair_expired"), (409, "pair_used"), (429, "rate_limited")] {
            let (f, _) = await refusal(s, r)
            let text = f?.userFacingText ?? ""
            XCTAssertFalse(text.isEmpty, r)
            XCTAssertFalse(text.contains(r), "\(r) is a slug, not a sentence")
        }
        XCTAssertTrue(PairingFailure.alreadyUsed.userFacingText.contains("already"))
        XCTAssertTrue(PairingFailure.expired.userFacingText.lowercased().contains("expired"))
    }

    func testAFailedRedeemSavesNothing() async {
        let (_, c) = await refusal(409, "pair_used")
        XCTAssertNil(defaults.string(forKey: "deckBaseURL"))
        XCTAssertNil(defaults.string(forKey: "deckName"))
        XCTAssertNil(try? tokens.token())
        XCTAssertNil(c.saved)
    }

    func testAnExpiredCodeNeverLeavesTheMac() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        do {
            _ = try await connection(spy).pair(code: try code(exp: 1000), device: "Mac")
            XCTFail("expired codes are dead; do not spend a rate-limited attempt on one")
        } catch { XCTAssertEqual(error as? PairingFailure, .expired) }
        XCTAssertTrue(spy.requests.isEmpty)
    }

    func testAnUnreadableSuccessBodyIsNotAConnection() async throws {
        let spy = SpyPerformer { _ in (200, Data(#"{"hello":1}"#.utf8), [:]) }
        do {
            _ = try await connection(spy).pair(code: try code(), device: "Mac")
            XCTFail("no token, no pairing")
        } catch { XCTAssertEqual(error as? PairingFailure, .badResponse) }
        XCTAssertNil(try tokens.token())
    }

    // MARK: forget

    func testForgetRemovesTokenUrlPinAndName() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        let c = connection(spy)
        _ = try await c.pair(code: try code(tls: "pin", pin: LocalTLSServer.pin), device: "Mac")
        XCTAssertNotNil(c.saved)
        try c.forget()
        for key in ["deckBaseURL", "deckTLSPin", "deckName"] { XCTAssertNil(defaults.object(forKey: key), key) }
        XCTAssertNil(try tokens.token())
        XCTAssertNil(c.saved)
    }

    func testSavedReflectsWhatWasStored() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.okBody.utf8), [:]) }
        let c = connection(spy)
        XCTAssertNil(c.saved)
        _ = try await c.pair(code: try code(tls: "pin", pin: LocalTLSServer.pin), device: "Mac")
        XCTAssertEqual(c.saved, SavedConnection(url: URL(string: "https://203.0.113.7")!, pin: LocalTLSServer.pin, name: "Dan's deck"))
    }

    // MARK: over a real pinned TLS connection

    func testPairsThroughARealPinnedTLSSession() async throws {
        let server = try LocalTLSServer()
        try await server.start()
        defer { server.stop() }
        server.body = okBody
        let c = DeckConnection(defaults: defaults, tokens: tokens)
        let pairing = try code(tls: "pin", url: "https://127.0.0.1:\(server.port)", pin: LocalTLSServer.pin)
        let paired = try await c.pair(code: pairing, device: "Mac")
        XCTAssertEqual(paired.name, "Dan's deck")
        XCTAssertTrue(try XCTUnwrap(server.requests.first).hasPrefix("POST /v1/pair"))
        XCTAssertEqual(try tokens.token()?.hasPrefix("adt_"), true)
    }

    func testAWrongPinIsSaidOutLoudAndSavesNothing() async throws {
        let server = try LocalTLSServer()
        try await server.start()
        defer { server.stop() }
        server.body = okBody
        let c = DeckConnection(defaults: defaults, tokens: tokens)
        let wrong = "sha256/" + Data(repeating: 9, count: 32).base64EncodedString()
        do {
            _ = try await c.pair(code: try code(tls: "pin", url: "https://127.0.0.1:\(server.port)", pin: wrong), device: "Mac")
            XCTFail("a server with a different key must not get the code")
        } catch { XCTAssertEqual(error as? PairingFailure, .pinMismatch) }
        XCTAssertTrue(server.requests.isEmpty)
        XCTAssertNil(try tokens.token())
        XCTAssertTrue(PairingFailure.pinMismatch.userFacingText.contains("not the server"))
    }

    func testStoredPinDrivesTheLiveTransports() async throws {
        let server = try LocalTLSServer()
        try await server.start()
        defer { server.stop() }
        defaults.set(server.url.absoluteString, forKey: "deckBaseURL")
        defaults.set(LocalTLSServer.pin, forKey: "deckTLSPin")
        let c = DeckConnection(defaults: defaults, tokens: tokens)
        let (_, http) = try await c.requestPerformer().perform(URLRequest(url: server.url.appendingPathComponent("v1/version")))
        XCTAssertEqual(http.statusCode, 200)
        _ = c.sseTransport()
    }
}
