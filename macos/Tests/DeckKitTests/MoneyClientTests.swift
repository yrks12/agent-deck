import XCTest
@testable import DeckKit

/// The clients that answer `GET /v1/money`: the HTTP adapter (route, refresh
/// query, 404 as a state) and the fixture the UI tests draw from.
final class MoneyClientTests: XCTestCase {
    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    func testMoneyIsOneAuthenticatedRequestAndRefreshAddsTheQuery() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/money"] = Data(#"{"state":"ready"}"#.utf8)
        let http = client(performer)
        let first = try await http.money(refresh: false)
        _ = try await http.money(refresh: true)
        XCTAssertEqual(first?.state, .ready)
        XCTAssertEqual(performer.paths, ["/v1/money", "/v1/money"])
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret", "Bearer sekret"])
        XCTAssertNil(performer.requests[0].url?.query)
        XCTAssertEqual(performer.requests[1].url?.query, "refresh=1")
    }

    func testADeckWithoutTheRouteAnswersNilNotAnError() async throws {
        let performer = StubPerformer()
        performer.status = 404
        let report = try await client(performer).money(refresh: false)
        XCTAssertNil(report)
    }

    func testAServerErrorThrows() async {
        let performer = StubPerformer()
        performer.status = 500
        do { _ = try await client(performer).money(refresh: false); XCTFail("expected a throw") } catch {}
    }

    func testTheFixtureClientServesAReadyReport() async throws {
        let r = try await FixtureDeckClient().money(refresh: false)
        XCTAssertEqual(r?.state, .ready)
        XCTAssertFalse(MoneyPresentation.companies(try XCTUnwrap(r)).isEmpty)
    }
}
