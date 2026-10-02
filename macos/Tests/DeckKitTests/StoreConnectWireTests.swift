import XCTest
@testable import DeckKit

/// **OAuth Connect and plugins, on the wire** (`docs/connectors.md`, "OAuth:
/// Connect" and "Plugins").
final class StoreConnectWireTests: XCTestCase {

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let tokens = InMemoryTokenStore()
        try? tokens.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens,
                              performer: performer)
    }

    private func body(_ request: URLRequest?) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(
            with: try XCTUnwrap(request?.httpBody)) as? [String: Any])
    }

    static let connectJSON = """
    {"ok":true,"authorize_url":"https://mcp.notion.com/authorize?client_id=c&state=S3CR3T",
     "state":"S3CR3T","redirect_uri":"http://127.0.0.1:47689/callback",
     "expires_at":"2026-09-30T12:10:00Z"}
    """

    func testConnectPostsIdAndDesksAndDecodesTheConsentURL() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/connect"] = Data(Self.connectJSON.utf8)

        let start = try await client(performer).storeConnect(id: "mcp:com.notion/mcp",
                                                             desks: .desks(["atlas"]))

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sekret")
        let sent = try body(request)
        XCTAssertEqual(sent["id"] as? String, "mcp:com.notion/mcp")
        XCTAssertEqual(sent["desks"] as? [String], ["atlas"])
        XCTAssertEqual(start.authorizeURL.host, "mcp.notion.com")
        XCTAssertEqual(start.state, "S3CR3T")
        XCTAssertEqual(start.redirectURI, "http://127.0.0.1:47689/callback")
        XCTAssertNotNil(start.expiresDate)
    }

    /// The state is the half of a sign-in that matches a code to it. It is
    /// needed for one status poll and nothing else; it is never described.
    func testAConnectStartNeverDescribesItsState() throws {
        let start = try DeckCoding.decoder.decode(StoreConnectStart.self, from: Data(Self.connectJSON.utf8))
        var dumped = ""
        dump(start, to: &dumped)
        for text in ["\(start)", String(reflecting: start), dumped] {
            XCTAssertFalse(text.contains("S3CR3T"), text)
        }
    }

    func testCompletePostsTheWholeCallbackURLAndDecodesAnInstall() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/connect/complete"] = Data("""
        {"ok":true,"item":\(StoreWireTests.connectorJSON),"installed":[],
         "reload":[{"desk":"atlas","action":"after_turn","detail":"reloads after its current turn"}]}
        """.utf8)
        let callback = "http://127.0.0.1:47689/callback?code=c&state=s"

        let result = try await client(performer).storeConnectComplete(callbackURL: callback)

        XCTAssertEqual(performer.paths, ["/v1/store/connect/complete"])
        XCTAssertEqual(try body(performer.requests.first) as? [String: String], ["callback_url": callback])
        XCTAssertEqual(result.reload.first?.desk, "atlas")
    }

    func testStatusIsAGetWithTheState() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/connect/status"] = Data(#"{"state":"done","id":"mcp:x","detail":""}"#.utf8)

        let status = try await client(performer).storeConnectStatus(state: "s")

        XCTAssertEqual(performer.requests.first?.httpMethod, "GET")
        XCTAssertTrue(try XCTUnwrap(performer.requests.first?.url?.query).contains("state=s"))
        XCTAssertEqual(status.phase, .done)
    }

    func testTheConnectRefusalsKeepTheDecksOwnSentence() {
        let cases: [(String, StoreRefusal)] = [
            ("bad_state", .badState), ("oauth_denied", .oauthDenied),
            ("oauth_unsupported", .oauthUnsupported), ("oauth_register_failed", .oauthRegisterFailed),
            ("oauth_token_failed", .oauthTokenFailed), ("install_failed", .installFailed),
            ("conflict", .conflict),
        ]
        for (reason, refusal) in cases {
            let detail = "That sign-in link is used up or expired; press Connect again."
            let error = DeckError(status: 409, body: Data(
                #"{"ok":false,"reason":"\#(reason)","detail":"\#(detail)"}"#.utf8))
            XCTAssertEqual(error, .storeRefused(refusal, detail: detail), reason)
            XCTAssertTrue(error.userFacingText.contains(detail), "\(reason): \(error.userFacingText)")
        }
    }

    // MARK: plugins

    func testAPluginItemDecodes() throws {
        let item = try DeckCoding.decoder.decode(StoreItem.self, from: Data("""
        {"id":"plugin:claude-plugins-official:figma","kind":"plugin","name":"figma","title":"Figma",
         "description":"Design files in context.","publisher":"Figma","source":"claude-plugins-official",
         "repo":null,"stars":null,"updated_at":null,"trust":"verified",
         "trust_note":"Made by Figma, listed in Anthropic's plugin directory. Plugins can bundle hooks and MCP servers.",
         "version":"1.0.0","auth":"none","secrets":[],"installable":true,"why_not":null,"installed_on":[]}
        """.utf8))

        XCTAssertEqual(item.kind, "plugin")
        XCTAssertEqual(StoreBrowser.badge(forTrust: item.trust), "Verified")
    }

    func testTheFixtureServesPluginsAndAnOAuthConnectorThatConnects() async throws {
        let fixture = FixtureDeckClient()
        let plugins = try await fixture.storeCatalog(kind: "plugin", query: nil, trust: .trusted,
                                                     limit: 100, offset: 0)
        XCTAssertTrue(plugins.items.contains { $0.trust == "official" })
        XCTAssertTrue(plugins.items.contains { $0.trust == "verified" })

        let connectors = try await fixture.storeCatalog(kind: "connector", query: nil, trust: .trusted,
                                                        limit: 100, offset: 0)
        let oauth = try XCTUnwrap(connectors.items.first { $0.auth == "oauth" })
        XCTAssertTrue(oauth.installable, "OAuth connectors install through Connect now")
        let start = try await fixture.storeConnect(id: oauth.id, desks: .all)
        XCTAssertEqual(start.redirectURI, "http://127.0.0.1:47689/callback")
    }
}
