import XCTest
@testable import DeckKit

/// **Connectors & Skills, on the wire.** The shapes are `docs/connectors.md`'s,
/// verbatim: an Item, a catalog page, the installed list, and what install,
/// update and uninstall answer. What is worth pinning is that each decodes,
/// that every route carries the `/v1` bearer, and that a refusal keeps its
/// `reason` instead of collapsing into a status code.
final class StoreWireTests: XCTestCase {

    private let base = URL(string: "https://deck.local")!

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let tokens = InMemoryTokenStore()
        try? tokens.setToken("sekret")
        return HTTPDeckClient(baseURL: base, tokens: tokens, performer: performer)
    }

    static let connectorJSON = """
    {"id":"mcp:com.microsoft/microsoft-learn-mcp","kind":"connector",
     "name":"microsoft-learn-mcp","title":"Microsoft Learn",
     "description":"Search Microsoft's official documentation.",
     "publisher":"Microsoft","source":"mcp-registry",
     "repo":"https://github.com/MicrosoftDocs/mcp","stars":12345,
     "updated_at":"2026-09-20T10:00:00Z","trust":"official",
     "trust_note":"Published by Microsoft (com.microsoft), source github.com/MicrosoftDocs/mcp",
     "version":"1.2.0","auth":"api_key",
     "secrets":[{"name":"API_KEY","label":"API key","description":"From the portal","required":true}],
     "installable":true,"why_not":null,"installed_on":["atlas"]}
    """

    static let skillJSON = """
    {"id":"skill:anthropics/skills:pdf","kind":"skill","name":"pdf","title":"PDF",
     "description":"Read and write PDF files.","publisher":"Anthropic",
     "source":"anthropic-skills","repo":"https://github.com/anthropics/skills",
     "stars":null,"updated_at":null,"trust":"official","trust_note":"Anthropic's own skills",
     "version":"9f2a15f","auth":"none","secrets":[],"installable":true,"why_not":null,
     "installed_on":[],"readme":"# PDF\\nUse this skill to..."}
    """

    // MARK: decoding

    func testAnItemDecodesEveryFieldTheContractNames() throws {
        let item = try DeckCoding.decoder.decode(StoreItem.self, from: Data(Self.connectorJSON.utf8))

        XCTAssertEqual(item.id, "mcp:com.microsoft/microsoft-learn-mcp")
        XCTAssertEqual(item.kind, "connector")
        XCTAssertEqual(item.title, "Microsoft Learn")
        XCTAssertEqual(item.publisher, "Microsoft")
        XCTAssertEqual(item.source, "mcp-registry")
        XCTAssertEqual(item.repo, "https://github.com/MicrosoftDocs/mcp")
        XCTAssertEqual(item.stars, 12345)
        XCTAssertEqual(item.updatedAt, "2026-09-20T10:00:00Z")
        XCTAssertNotNil(item.updatedDate, "an ISO date the app cannot read shows no age at all")
        XCTAssertEqual(item.trust, "official")
        XCTAssertEqual(item.version, "1.2.0")
        XCTAssertEqual(item.auth, "api_key")
        XCTAssertEqual(item.secrets, [StoreSecretField(
            name: "API_KEY", label: "API key", description: "From the portal", required: true)])
        XCTAssertTrue(item.installable)
        XCTAssertNil(item.whyNot)
        XCTAssertEqual(item.installedOn, ["atlas"])
    }

    func testASkillWithNullsAndAReadmeDecodes() throws {
        let item = try DeckCoding.decoder.decode(StoreItem.self, from: Data(Self.skillJSON.utf8))

        XCTAssertEqual(item.kind, "skill")
        XCTAssertNil(item.stars)
        XCTAssertNil(item.updatedAt)
        XCTAssertNil(item.updatedDate)
        XCTAssertEqual(item.readme, "# PDF\nUse this skill to...")
        XCTAssertEqual(item.secrets, [])
    }

    func testTheCatalogPageDecodesItemsTotalAndStaleness() throws {
        let page = try DeckCoding.decoder.decode(StoreCatalogPage.self, from: Data("""
        {"items":[\(Self.connectorJSON),\(Self.skillJSON)],"total":140,
         "refreshed_at":"2026-09-30T06:00:00Z","stale":true}
        """.utf8))

        XCTAssertEqual(page.items.map(\.id), [
            "mcp:com.microsoft/microsoft-learn-mcp", "skill:anthropics/skills:pdf"])
        XCTAssertEqual(page.total, 140)
        XCTAssertEqual(page.refreshedAt, "2026-09-30T06:00:00Z")
        XCTAssertTrue(page.stale)
    }

    func testTheInstalledListDecodesPerDesk() throws {
        let page = try DeckCoding.decoder.decode(StoreInstalledPage.self, from: Data("""
        {"desks":[{"desk":"atlas","items":[
          {"id":"skill:anthropics/skills:pdf","kind":"skill","name":"pdf","title":"PDF",
           "desk":"atlas","version":"9f2a15f","commit":"9f2a15f5b1bf0c3e","installed_at":"2026-09-29T12:00:00Z",
           "trust":"official","update_available":true}]},
          {"desk":"hemingway","items":[]}]}
        """.utf8))

        XCTAssertEqual(page.desks.map(\.desk), ["atlas", "hemingway"])
        let row = try XCTUnwrap(page.desks.first?.items.first)
        XCTAssertEqual(row.commit, "9f2a15f5b1bf0c3e")
        XCTAssertEqual(row.installedAt, "2026-09-29T12:00:00Z")
        XCTAssertTrue(row.updateAvailable)
    }

    /// `action` is an open set on the server (`after_turn`,
    /// `restart_after_turn`, `next_wake`, `restarting`, `none`, …). A closed
    /// enum here would turn a newer deck's successful install into a decoding
    /// error on screen.
    func testAnInstallAnswerDecodesAndReloadActionIsAnOpenString() throws {
        let result = try DeckCoding.decoder.decode(StoreInstallResult.self, from: Data("""
        {"ok":true,"item":\(Self.connectorJSON),
         "installed":[{"id":"mcp:com.microsoft/microsoft-learn-mcp","kind":"connector",
           "name":"microsoft-learn-mcp","title":"Microsoft Learn","desk":"atlas","version":"1.2.0",
           "commit":null,"installed_at":"2026-09-30T09:00:00Z","trust":"official","update_available":false}],
         "reload":[{"desk":"atlas","action":"restart_after_turn","detail":"reloads after its current turn"},
                   {"desk":"hemingway","action":"something_new","detail":"picks it up on its next wake"}]}
        """.utf8))

        XCTAssertEqual(result.item.id, "mcp:com.microsoft/microsoft-learn-mcp")
        XCTAssertEqual(result.installed.map(\.desk), ["atlas"])
        XCTAssertEqual(result.reload.map(\.action), ["restart_after_turn", "something_new"])
        XCTAssertEqual(result.reload.first?.detail, "reloads after its current turn")
    }

    func testAnUninstallAnswerDecodes() throws {
        let result = try DeckCoding.decoder.decode(StoreUninstallResult.self, from: Data("""
        {"ok":true,"removed":[{"desk":"atlas","id":"skill:anthropics/skills:pdf"}],
         "reload":[{"desk":"atlas","action":"next_wake","detail":"picks it up on its next wake"}]}
        """.utf8))

        XCTAssertEqual(result.removed, [StoreRemoved(desk: "atlas", id: "skill:anthropics/skills:pdf")])
        XCTAssertEqual(result.reload.first?.action, "next_wake")
    }

    // MARK: the routes

    func testTheTrustedCatalogIsOneBearerGETWithKindAndTrustTrusted() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/catalog"] = Data(#"{"items":[],"total":0,"refreshed_at":null,"stale":false}"#.utf8)

        _ = try await client(performer).storeCatalog(
            kind: "connector", query: nil, trust: .trusted, limit: 100, offset: 0)

        XCTAssertEqual(performer.paths, ["/v1/store/catalog"])
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
        let query = try XCTUnwrap(performer.requests.first?.url?.query)
        XCTAssertTrue(query.contains("kind=connector"), query)
        XCTAssertTrue(query.contains("trust=trusted"), query)
        XCTAssertFalse(query.contains("q="), "the trusted catalog is filtered on the Mac: \(query)")
    }

    func testOtherSourcesAskForTrustAllWithTheQuery() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/catalog"] = Data(#"{"items":[],"total":0,"refreshed_at":null,"stale":false}"#.utf8)

        _ = try await client(performer).storeCatalog(
            kind: "skill", query: "postgres", trust: .all, limit: 100, offset: 0)

        let query = try XCTUnwrap(performer.requests.first?.url?.query)
        XCTAssertTrue(query.contains("trust=all"), query)
        XCTAssertTrue(query.contains("q=postgres"), query)
    }

    func testOneItemIsAskedForById() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/item"] = Data(Self.skillJSON.utf8)

        let item = try await client(performer).storeItem(id: "skill:anthropics/skills:pdf")

        XCTAssertEqual(item.id, "skill:anthropics/skills:pdf")
        XCTAssertTrue(try XCTUnwrap(performer.requests.first?.url?.query).contains("id=skill"))
    }

    func testInstallPostsIdDesksSecretsAndTheUnverifiedAcceptance() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/install"] = Data("""
        {"ok":true,"item":\(Self.connectorJSON),"installed":[],"reload":[]}
        """.utf8)

        _ = try await client(performer).storeInstall(StoreInstallRequest(
            id: "mcp:x/y", desks: .desks(["atlas", "hemingway"]),
            secrets: ["API_KEY": "sk-live-123"], acceptUnverified: true))

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sekret")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(
            with: try XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(body["id"] as? String, "mcp:x/y")
        XCTAssertEqual(body["desks"] as? [String], ["atlas", "hemingway"])
        XCTAssertEqual(body["secrets"] as? [String: String], ["API_KEY": "sk-live-123"])
        XCTAssertEqual(body["accept_unverified"] as? Bool, true)
    }

    func testAllDesksIsTheStringAllAndNoSecretsSendsNoSecretsKey() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/install"] = Data("""
        {"ok":true,"item":\(Self.skillJSON),"installed":[],"reload":[]}
        """.utf8)

        _ = try await client(performer).storeInstall(StoreInstallRequest(
            id: "skill:anthropics/skills:pdf", desks: .all, secrets: [:], acceptUnverified: false))

        let body = try XCTUnwrap(JSONSerialization.jsonObject(
            with: try XCTUnwrap(performer.requests.first?.httpBody)) as? [String: Any])
        XCTAssertEqual(body["desks"] as? String, "all")
        XCTAssertNil(body["secrets"])
        XCTAssertNil(body["accept_unverified"], "only ever sent as an explicit yes")
    }

    func testUpdateUninstallInstalledAndRefreshHitTheirRoutes() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/store/update"] = Data("""
        {"ok":true,"item":\(Self.skillJSON),"installed":[],"reload":[]}
        """.utf8)
        performer.bodies["/v1/store/uninstall"] = Data(#"{"ok":true,"removed":[],"reload":[]}"#.utf8)
        performer.bodies["/v1/store/installed"] = Data(#"{"desks":[]}"#.utf8)
        performer.bodies["/v1/store/refresh"] = Data(#"{"ok":true,"refreshing":true}"#.utf8)
        let deck = client(performer)

        _ = try await deck.storeUpdate(id: "skill:a", desks: .desks(["atlas"]))
        _ = try await deck.storeUninstall(id: "skill:a", desks: .all)
        _ = try await deck.storeInstalled(desk: "atlas")
        let refreshing = try await deck.storeRefresh()

        XCTAssertEqual(performer.paths, [
            "/v1/store/update", "/v1/store/uninstall", "/v1/store/installed", "/v1/store/refresh"])
        XCTAssertEqual(performer.requests.map(\.httpMethod), ["POST", "POST", "GET", "POST"])
        XCTAssertEqual(Set(performer.authHeaders), ["Bearer sekret"])
        XCTAssertTrue(try XCTUnwrap(performer.requests[2].url?.query).contains("desk=atlas"))
        XCTAssertTrue(refreshing)
    }

    // MARK: refusals keep their reason

    func testEveryStoreRefusalKeepsItsReasonAndDetail() {
        let cases: [(Int, String, StoreRefusal)] = [
            (404, "unknown_item", .unknownItem), (404, "unknown_desk", .unknownDesk),
            (404, "not_installed", .notInstalled), (409, "unverified", .unverified),
            (400, "missing_secret", .missingSecret), (409, "needs_oauth", .needsOAuth),
            (409, "not_installable", .notInstallable), (502, "fetch_failed", .fetchFailed),
            (400, "bad_input", .badInput),
        ]
        for (status, reason, refusal) in cases {
            let error = DeckError(status: status, body: Data(
                #"{"ok":false,"reason":"\#(reason)","detail":"API_KEY"}"#.utf8))
            XCTAssertEqual(error, .storeRefused(refusal, detail: "API_KEY"), reason)
            XCTAssertFalse(error.userFacingText.isEmpty, reason)
        }
        XCTAssertTrue(
            DeckError.storeRefused(.missingSecret, detail: "API_KEY").userFacingText.contains("API_KEY"),
            "the detail names the field; the sentence must keep it")
        XCTAssertTrue(DeckError.storeRefused(.fetchFailed, detail: "").isRetryable)
        XCTAssertFalse(DeckError.storeRefused(.unverified, detail: "").isRetryable)
    }

    func testTheFixtureServesAStoreWorthLookingAt() async throws {
        let fixture = FixtureDeckClient()
        let connectors = try await fixture.storeCatalog(
            kind: "connector", query: nil, trust: .trusted, limit: 100, offset: 0)
        let skills = try await fixture.storeCatalog(
            kind: "skill", query: nil, trust: .trusted, limit: 100, offset: 0)
        let everything = try await fixture.storeCatalog(
            kind: "connector", query: nil, trust: .all, limit: 100, offset: 0)

        XCTAssertFalse(connectors.items.isEmpty)
        XCTAssertFalse(skills.items.isEmpty)
        XCTAssertTrue(connectors.items.allSatisfy { $0.trust != "unverified" },
                      "the trusted catalog never carries an unverified item")
        XCTAssertTrue(everything.items.contains { $0.trust == "unverified" },
                      "Other sources must have something to warn about in a demo")
        let installed = try await fixture.storeInstalled(desk: nil)
        XCTAssertFalse(installed.desks.isEmpty)
    }
}
