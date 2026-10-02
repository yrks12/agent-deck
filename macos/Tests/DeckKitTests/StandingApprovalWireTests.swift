import XCTest
@testable import DeckKit

/// **§23 standing approvals, read off the wire.** The policy row, its limits
/// (where `null` is "no limit" and must go back out as `null`), today's usage
/// and the audit line — each read leniently, so a newer deck's extra key or a
/// missing one is a state, not a dropped row.
final class StandingApprovalWireTests: XCTestCase {
    private let row = #"""
    {"id":"sa_3f9a1c2e","desk":"atlas","kind":"send_email","tool":"*","pattern":"*",
     "limits":{"count_per_day":80,"usd_per_day":null,"recipients":["@acme.com"],"account":"me@acme.com"},
     "expires_at":null,"created_by":"owner","created_at":1790000000.0,"approved_at":1790000000.0,
     "revoked_at":null,"status":"active","note":"","live":true,"expired":false,
     "usage":{"day":"2026-10-02","count":43,"usd":0.0}}
    """#

    func testThePolicyRowFromTheContractReadsInFull() throws {
        let p = try DeckCoding.decoder.decode(StandingPolicy.self, from: Data(row.utf8))
        XCTAssertEqual(p.id, "sa_3f9a1c2e")
        XCTAssertEqual(p.desk, "atlas")
        XCTAssertEqual(p.kind, .sendEmail)
        XCTAssertEqual(p.limits.countPerDay, 80)
        XCTAssertNil(p.limits.usdPerDay)
        XCTAssertEqual(p.limits.recipients, ["@acme.com"])
        XCTAssertEqual(p.limits.account, "me@acme.com")
        XCTAssertNil(p.expiresAt)
        XCTAssertEqual(p.createdBy, "owner")
        XCTAssertEqual(p.createdAt, Date(timeIntervalSince1970: 1_790_000_000))
        XCTAssertNil(p.revokedAt)
        XCTAssertEqual(p.status, .active)
        XCTAssertTrue(p.live)
        XCTAssertFalse(p.expired)
        XCTAssertEqual(p.usage, StandingUsage(day: "2026-10-02", count: 43, usd: 0))
    }

    func testAProposedRowFromADeskAndAnUnknownKindStillRead() throws {
        let json = #"{"id":"sa_1","desk":"*","kind":"teleport","status":"proposed","created_by":"atlas"}"#
        let p = try DeckCoding.decoder.decode(StandingPolicy.self, from: Data(json.utf8))
        XCTAssertEqual(p.status, .proposed)
        XCTAssertEqual(p.kind, .other("teleport"))
        XCTAssertEqual(p.kind.wire, "teleport")
        XCTAssertEqual(p.createdBy, "atlas")
        XCTAssertEqual(p.usage.count, 0)
        XCTAssertFalse(p.live)
    }

    func testEveryKindRoundTripsItsSlug() {
        for kind in StandingKind.all { XCTAssertEqual(StandingKind(wire: kind.wire), kind) }
        XCTAssertEqual(StandingKind.all.map(\.wire),
                       ["send_email", "spend_money", "post_comment", "run_command", "call_api"])
    }

    func testANoLimitIsSentAsAnExplicitNullSoAPatchRemovesIt() throws {
        let data = try DeckCoding.encoder.encode(StandingLimits(countPerDay: nil, usdPerDay: 5))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertTrue(object["count_per_day"] is NSNull, "a missing key keeps the old limit on a PATCH")
        XCTAssertEqual(object["usd_per_day"] as? Double, 5)
        XCTAssertEqual(object["recipients"] as? [String], [])
        XCTAssertEqual(object["account"] as? String, "")
    }

    func testTheAuditLineReads() throws {
        let json = #"""
        {"ts":1790000100.5,"desk":"atlas","tool":"mcp__gmail__send","action":"send to b***@acme.com",
         "kind":"send_email","policy_id":"sa_3f9a1c2e","count_after":44,"usd_after":0.0,"cost_usd":null}
        """#
        let line = try DeckCoding.decoder.decode(StandingAuditLine.self, from: Data(json.utf8))
        XCTAssertEqual(line.at, Date(timeIntervalSince1970: 1_790_000_100.5))
        XCTAssertEqual(line.policyID, "sa_3f9a1c2e")
        XCTAssertEqual(line.kind, .sendEmail)
        XCTAssertEqual(line.countAfter, 44)
        XCTAssertNil(line.costUSD)
        XCTAssertEqual(line.action, "send to b***@acme.com")
    }

    func testTheEditFormStartsFromTheRow() throws {
        let p = try DeckCoding.decoder.decode(StandingPolicy.self, from: Data(row.utf8))
        let draft = StandingDraft(p)
        XCTAssertEqual(draft.desk, "atlas")
        XCTAssertEqual(draft.kind, .sendEmail)
        XCTAssertEqual(draft.limits, p.limits)
    }
}
