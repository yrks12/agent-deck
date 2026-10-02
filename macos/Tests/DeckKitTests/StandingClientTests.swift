import XCTest
@testable import DeckKit

/// **Every §23 route, as the HTTP client builds it** — list, create, PATCH,
/// approve, revoke (DELETE) and the audit — and every refusal keeping its
/// `reason`, never reduced to a status code.
final class StandingClientTests: XCTestCase {
    private let policy = Data(#"{"ok":true,"policy":{"id":"sa_1","desk":"atlas","kind":"send_email","status":"active","limits":{"count_per_day":80}}}"#.utf8)

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    private func body(_ r: URLRequest) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(r.httpBody)) as? [String: Any])
    }

    func testTheListIsOneAuthenticatedGetWithItsFilters() async throws {
        let p = StubPerformer()
        p.bodies["/v1/standing-approvals"] = Data(#"{"policies":[{"id":"sa_1","status":"proposed"},{"nope":1}],"generated_at":1}"#.utf8)
        let rows = try await client(p).standingApprovals(status: "proposed", desk: "atlas")
        XCTAssertEqual(rows.map(\.id), ["sa_1"], "an unreadable row is skipped, not the whole list")
        XCTAssertEqual(p.requests[0].httpMethod, "GET")
        XCTAssertEqual(p.authHeaders, ["Bearer sekret"])
        XCTAssertEqual(p.requests[0].url?.query, "status=proposed&desk=atlas")
        _ = try await client(p).standingApprovals(status: nil, desk: nil)
        XCTAssertNil(p.requests[1].url?.query)
    }

    func testCreatePostsTheKindTheLimitsAndAnExplicitNoExpiry() async throws {
        let p = StubPerformer(); p.defaultBody = policy
        let made = try await client(p).createStanding(StandingDraft(
            desk: "atlas", kind: .sendEmail, limits: StandingLimits(countPerDay: 80, recipients: ["@acme.com"])))
        XCTAssertEqual(made.id, "sa_1")
        XCTAssertEqual(p.requests[0].httpMethod, "POST")
        XCTAssertEqual(p.paths, ["/v1/standing-approvals"])
        let b = try body(p.requests[0])
        XCTAssertEqual(b["kind"] as? String, "send_email")
        XCTAssertEqual(b["desk"] as? String, "atlas")
        XCTAssertTrue(b["expires_at"] is NSNull)
        let limits = try XCTUnwrap(b["limits"] as? [String: Any])
        XCTAssertEqual(limits["count_per_day"] as? Int, 80)
        XCTAssertTrue(limits["usd_per_day"] is NSNull)
        XCTAssertEqual(limits["recipients"] as? [String], ["@acme.com"])
    }

    func testAnEditIsAPatchThatNeverSendsTheKind() async throws {
        let p = StubPerformer(); p.defaultBody = policy
        let when = Date(timeIntervalSince1970: 1_800_000_000)
        _ = try await client(p).updateStanding(id: "sa_1", StandingDraft(
            desk: "*", kind: .runCommand, pattern: "gh pr*", limits: StandingLimits(countPerDay: 5), expiresAt: when))
        XCTAssertEqual(p.requests[0].httpMethod, "PATCH")
        XCTAssertEqual(p.paths, ["/v1/standing-approvals/sa_1"])
        let b = try body(p.requests[0])
        XCTAssertNil(b["kind"], "the deck does not let an edit change the kind")
        XCTAssertEqual(b["pattern"] as? String, "gh pr*")
        XCTAssertEqual(b["expires_at"] as? Double, 1_800_000_000)
    }

    func testApproveAndRevokeHitTheirRoutes() async throws {
        let p = StubPerformer(); p.defaultBody = policy
        _ = try await client(p).approveStanding(id: "sa_1")
        _ = try await client(p).revokeStanding(id: "sa/2")
        XCTAssertEqual(p.requests.map(\.httpMethod), ["POST", "DELETE"])
        XCTAssertEqual(p.requests[0].url?.path, "/v1/standing-approvals/sa_1/approve")
        XCTAssertEqual(p.requests[1].url?.absoluteString, "https://deck.local/v1/standing-approvals/sa%2F2",
                       "an id is data, never a path")
    }

    func testTheAuditAsksWithItsLimitAndFilters() async throws {
        let p = StubPerformer()
        p.bodies["/v1/standing-approvals/audit"] = Data(#"{"audit":[{"ts":1,"desk":"atlas","policy_id":"sa_1","kind":"send_email","action":"x"}]}"#.utf8)
        let lines = try await client(p).standingAudit(desk: "atlas", policyID: "sa_1", limit: 200)
        XCTAssertEqual(lines.map(\.policyID), ["sa_1"])
        XCTAssertEqual(p.requests[0].url?.query, "limit=200&desk=atlas&policy_id=sa_1")
    }

    func testEveryRefusalKeepsItsReason() async {
        for refusal in StandingRefusal.allCases {
            let p = StubPerformer(); p.status = 409
            p.defaultBody = Data(#"{"ok":false,"reason":"\#(refusal.rawValue)","detail":"why"}"#.utf8)
            do {
                _ = try await client(p).revokeStanding(id: "sa_1")
                XCTFail("expected \(refusal) to throw")
            } catch let error as DeckError {
                XCTAssertEqual(error, .standingRefused(refusal, detail: "why"))
                XCTAssertFalse(error.userFacingText.isEmpty)
                XCTAssertFalse(error.isRetryable)
            } catch { XCTFail("\(error)") }
        }
    }

    func testTheSameSlugOnAnotherRouteIsNotAStandingRefusal() {
        let body = Data(#"{"ok":false,"reason":"revoked"}"#.utf8)
        XCTAssertEqual(DeckError(status: 409, body: body), .http(409, reason: "revoked"))
        XCTAssertEqual(DeckError.standing(status: 409, body: body), .standingRefused(.revoked, detail: ""))
    }

    func testARefusalThatIsNotAStandingOneFallsThroughUnchanged() {
        XCTAssertEqual(DeckError.standing(status: 401, body: Data(#"{"reason":"unauthorized"}"#.utf8)), .unauthorized)
        XCTAssertEqual(DeckError.standing(status: 404, body: Data(#"{"reason":"unknown_ask"}"#.utf8)), .unknownAsk)
        XCTAssertEqual(DeckError.standing(status: 400, body: Data(#"{"reason":"brand_new"}"#.utf8)),
                       .http(400, reason: "brand_new"))
    }

    func testNeverCoverableSaysWhyInTheDecksWords() {
        let text = DeckError.standingRefused(.neverCoverable, detail: "deleting data").userFacingText
        XCTAssertTrue(text.contains("deleting data"), text)
    }
}
