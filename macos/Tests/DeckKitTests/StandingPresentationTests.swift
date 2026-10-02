import XCTest
@testable import DeckKit

/// **What the Standing approvals screen says.** Usage against each limit, the
/// proposed-count badge, the order of the groups, and the form's own check —
/// decided here so the Mac and the phone say the same thing.
final class StandingPresentationTests: XCTestCase {
    private func policy(_ id: String, kind: StandingKind = .sendEmail, status: StandingStatus = .active,
                        count: Int? = 80, usd: Double? = nil, used: Int = 0, spent: Double = 0,
                        expired: Bool = false, created: Double = 0, by: String = "owner") -> StandingPolicy {
        StandingPolicy(id: id, desk: "atlas", kind: kind, limits: StandingLimits(countPerDay: count, usdPerDay: usd),
                       createdBy: by, createdAt: Date(timeIntervalSince1970: created), status: status,
                       live: status == .active && !expired, expired: expired,
                       usage: StandingUsage(day: "2026-10-02", count: used, usd: spent))
    }

    func testUsageReadsAgainstEachLimit() {
        XCTAssertEqual(StandingPresentation.usage(policy("a", used: 43)), ["43/80 emails today"])
        XCTAssertEqual(StandingPresentation.usage(policy("b", kind: .spendMoney, count: nil, usd: 5, spent: 1.2)),
                       ["$1.20/$5.00 today"])
        XCTAssertEqual(StandingPresentation.usage(policy("c", kind: .runCommand, count: 1, usd: 2, used: 1, spent: 0.5)),
                       ["1/1 command today", "$0.50/$2.00 today"])
        XCTAssertEqual(StandingPresentation.usage(policy("d", kind: .callAPI, count: 10, used: 3)), ["3/10 API calls today"])
    }

    func testTheMeterFollowsTheFullerLimit() {
        XCTAssertEqual(StandingPresentation.fill(policy("a", used: 40)), 0.5)
        XCTAssertEqual(StandingPresentation.fill(policy("b", count: 10, usd: 4, used: 1, spent: 3)), 0.75)
        XCTAssertEqual(StandingPresentation.fill(policy("c", used: 200)), 1)
        XCTAssertNil(StandingPresentation.fill(policy("d", count: nil)))
    }

    func testTheBadgeCountsProposalsOnly() {
        let rows = [policy("a", status: .proposed), policy("b"), policy("c", status: .proposed),
                    policy("d", status: .revoked)]
        XCTAssertEqual(StandingPresentation.proposedCount(rows), 2)
        XCTAssertEqual(StandingPresentation.badge(2), "2")
        XCTAssertNil(StandingPresentation.badge(0))
        XCTAssertEqual(StandingPresentation.badge(120), "99+")
    }

    func testProposalsComeFirstThenWhatIsHonouredThenWhatEnded() {
        let rows = [policy("live-old", created: 1), policy("gone", status: .revoked),
                    policy("ask", status: .proposed, by: "atlas"), policy("live-new", created: 9),
                    policy("lapsed", expired: true)]
        let s = StandingPresentation.sections(rows)
        XCTAssertEqual(s.proposed.map(\.id), ["ask"])
        XCTAssertEqual(s.active.map(\.id), ["live-new", "live-old"])
        XCTAssertEqual(Set(s.ended.map(\.id)), ["gone", "lapsed"])
    }

    func testStateLineSaysWhoProposedAndWhenItEnds() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertEqual(StandingPresentation.state(policy("a", status: .proposed, by: "atlas")), "Proposed by atlas")
        XCTAssertEqual(StandingPresentation.state(policy("b")), "No expiry")
        var ending = policy("c"); ending.expiresAt = now.addingTimeInterval(3 * 86_400 + 60)
        XCTAssertEqual(StandingPresentation.state(ending, now: now), "Expires in 3 days")
        XCTAssertEqual(StandingPresentation.state(policy("d", expired: true)), "Expired")
        XCTAssertEqual(StandingPresentation.state(policy("e", status: .revoked)), "Revoked")
    }

    func testHeadingLimitsAndScope() {
        var p = policy("a", kind: .runCommand, count: 20, usd: 5)
        p.desk = "*"; p.pattern = "gh pr*"; p.limits.recipients = []
        XCTAssertEqual(StandingPresentation.heading(p), "Every desk · Run commands")
        XCTAssertEqual(StandingPresentation.limits(p.limits, kind: p.kind), "Up to 20 commands a day · up to $5.00 a day")
        XCTAssertEqual(StandingPresentation.scope(p), "gh pr*", "drawn monospaced; a backtick would show as one")
        var mail = policy("b"); mail.limits.recipients = ["@acme.com"]; mail.limits.account = "me@acme.com"
        XCTAssertEqual(StandingPresentation.scope(mail), "to @acme.com · as me@acme.com")
        XCTAssertNil(StandingPresentation.scope(policy("c")))
    }

    func testTheFormChecksWhatTheDeckWouldRefuse() {
        XCTAssertNotNil(StandingPresentation.problem(StandingDraft(desk: "", limits: StandingLimits(countPerDay: 1))))
        XCTAssertNotNil(StandingPresentation.problem(StandingDraft(desk: "atlas")), "no limit")
        XCTAssertNotNil(StandingPresentation.problem(StandingDraft(desk: "atlas", kind: .spendMoney,
                                                                  limits: StandingLimits(countPerDay: 3))))
        XCTAssertNotNil(StandingPresentation.problem(StandingDraft(desk: "atlas", kind: .runCommand,
                                                                  limits: StandingLimits(countPerDay: 3))), "too wide")
        XCTAssertNotNil(StandingPresentation.problem(StandingDraft(desk: "atlas", limits: StandingLimits(countPerDay: 0))))
        XCTAssertNil(StandingPresentation.problem(StandingDraft(desk: "atlas", kind: .runCommand, pattern: "gh pr*",
                                                               limits: StandingLimits(countPerDay: 3))))
    }

    func testTypedLimits() {
        XCTAssertEqual(StandingPresentation.count(" 80 "), 80)
        XCTAssertNil(StandingPresentation.count(""))
        XCTAssertEqual(StandingPresentation.dollars("$5.50"), 5.5)
        XCTAssertNil(StandingPresentation.dollars("five"))
    }

    func testTheAuditLineReads() {
        let line = StandingAuditLine(at: nil, desk: "atlas", tool: "Bash", action: "gh pr create", kind: .runCommand,
                                     policyID: "sa_1", countAfter: 4, usdAfter: 0, costUSD: nil)
        XCTAssertEqual(StandingPresentation.auditTitle(line), "atlas · gh pr create")
        XCTAssertEqual(StandingPresentation.auditDetail(line), "Run commands · 4 today")
    }
}
