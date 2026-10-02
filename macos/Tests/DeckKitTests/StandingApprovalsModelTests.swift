import XCTest
@testable import DeckKit
@testable import DeckUI

/// A §23 client that records what was asked and answers from a script.
final class StubStanding: StandingApprovalsClient, @unchecked Sendable {
    var rows: [StandingPolicy] = []
    var audit: [StandingAuditLine] = []
    var failure: DeckError?
    private(set) var calls: [String] = []

    private func check() throws { if let failure { throw failure } }
    private func row(_ id: String) -> StandingPolicy {
        rows.first { $0.id == id } ?? StandingPolicy(id: id, desk: "atlas", kind: .sendEmail, limits: StandingLimits(countPerDay: 1))
    }

    func standingApprovals(status: String?, desk: String?) async throws -> [StandingPolicy] {
        calls.append("list"); try check(); return rows
    }
    func createStanding(_ draft: StandingDraft) async throws -> StandingPolicy {
        calls.append("create \(draft.desk)"); try check()
        return StandingPolicy(id: "sa_new", desk: draft.desk, kind: draft.kind, limits: draft.limits)
    }
    func updateStanding(id: String, _ draft: StandingDraft) async throws -> StandingPolicy {
        calls.append("update \(id)"); try check()
        var r = row(id); r.limits = draft.limits; return r
    }
    func approveStanding(id: String) async throws -> StandingPolicy {
        calls.append("approve \(id)"); try check()
        var r = row(id); r.status = .active; r.live = true; return r
    }
    func revokeStanding(id: String) async throws -> StandingPolicy {
        calls.append("revoke \(id)"); try check()
        var r = row(id); r.status = .revoked; r.live = false; return r
    }
    func standingAudit(desk: String?, policyID: String?, limit: Int) async throws -> [StandingAuditLine] {
        calls.append("audit \(limit)"); try check(); return audit
    }
}

/// **The Standing approvals screen's model**, shared by the Mac and the phone:
/// it lists, approves, dismisses, revokes and saves — changing a row only when
/// the deck says so, and showing a refusal in the deck's own words.
@MainActor
final class StandingApprovalsModelTests: XCTestCase {
    private func proposal(_ id: String) -> StandingPolicy {
        StandingPolicy(id: id, desk: "atlas", kind: .sendEmail, limits: StandingLimits(countPerDay: 80),
                       createdBy: "atlas", status: .proposed, live: false)
    }

    func testItLoadsAndCountsTheProposals() async {
        let stub = StubStanding()
        stub.rows = [proposal("sa_1"), StandingPolicy(id: "sa_2", desk: "*", kind: .callAPI,
                                                      limits: StandingLimits(countPerDay: 5))]
        let model = StandingApprovalsModel(client: stub)
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.proposedCount, 1)
        XCTAssertEqual(model.sections.proposed.map(\.id), ["sa_1"])
    }

    func testApproveAndDismissChangeTheRowTheDeckAnswered() async {
        let stub = StubStanding()
        stub.rows = [proposal("sa_1"), proposal("sa_2")]
        let model = StandingApprovalsModel(client: stub)
        await model.load()
        await model.approve(model.policies[0])
        await model.revoke(model.policies[1])
        XCTAssertEqual(stub.calls, ["list", "approve sa_1", "revoke sa_2"])
        XCTAssertEqual(model.policies.map(\.status), [.active, .revoked])
        XCTAssertEqual(model.proposedCount, 0)
        XCTAssertTrue(model.busy.isEmpty)
    }

    func testARefusalIsShownInTheDecksWordsAndTheListIsReread() async {
        let stub = StubStanding()
        stub.rows = [proposal("sa_1")]
        let model = StandingApprovalsModel(client: stub)
        await model.load()
        stub.failure = .standingRefused(.notProposed, detail: "")
        await model.approve(model.policies[0])
        XCTAssertEqual(model.problem, DeckError.standingRefused(.notProposed, detail: "").userFacingText)
        XCTAssertEqual(stub.calls.last, "list")
        XCTAssertEqual(model.phase, .loaded, "a failed refresh keeps what is on screen")
    }

    func testSaveCreatesOrEditsAndAFormProblemNeverReachesTheDeck() async {
        let stub = StubStanding()
        let model = StandingApprovalsModel(client: stub)
        let ok = await model.save(StandingDraft(desk: "atlas", limits: StandingLimits(countPerDay: 3)))
        XCTAssertTrue(ok)
        XCTAssertEqual(model.policies.map(\.id), ["sa_new"])
        let edited = await model.save(StandingDraft(desk: "atlas", limits: StandingLimits(countPerDay: 9)), editing: "sa_new")
        XCTAssertTrue(edited)
        XCTAssertEqual(model.policies.first?.limits.countPerDay, 9)
        let refused = await model.save(StandingDraft(desk: "atlas"))
        XCTAssertFalse(refused)
        XCTAssertNotNil(model.problem)
        XCTAssertEqual(stub.calls, ["create atlas", "update sa_new"])
    }

    func testTheDecksRefusalKeepsTheFormOpen() async {
        let stub = StubStanding()
        stub.failure = .standingRefused(.neverCoverable, detail: "deleting data")
        let model = StandingApprovalsModel(client: stub)
        let ok = await model.save(StandingDraft(desk: "atlas", kind: .runCommand, pattern: "rm *",
                                                limits: StandingLimits(countPerDay: 3)))
        XCTAssertFalse(ok)
        XCTAssertTrue(model.problem?.contains("deleting data") == true)
    }

    func testAnOlderDeckWithoutTheRoutesIsAStateNotAnError() async {
        let stub = StubStanding()
        stub.failure = .http(404, reason: "")
        let model = StandingApprovalsModel(client: stub)
        await model.load()
        XCTAssertEqual(model.phase, .unavailable)
        stub.failure = .unauthorized
        let other = StandingApprovalsModel(client: stub)
        await other.load()
        XCTAssertEqual(other.phase, .failed(DeckError.unauthorized.userFacingText))
    }

    func testTheAuditIsAskedForTwoHundredLines() async {
        let stub = StubStanding()
        stub.audit = [StandingAuditLine(at: nil, desk: "atlas", tool: "Bash", action: "gh pr list",
                                        kind: .runCommand, policyID: "sa_1")]
        let model = StandingApprovalsModel(client: stub)
        await model.loadAudit()
        XCTAssertEqual(model.audit.count, 1)
        XCTAssertEqual(stub.calls, ["audit 200"])
    }

    func testTheFixtureServesEveryGroup() async throws {
        let rows = try await FixtureDeckClient().standingApprovals(status: nil, desk: nil)
        let s = StandingPresentation.sections(rows)
        XCTAssertFalse(s.proposed.isEmpty)
        XCTAssertFalse(s.active.isEmpty)
        let log = try await FixtureDeckClient().standingAudit(desk: nil, policyID: nil, limit: 200)
        XCTAssertFalse(log.isEmpty)
    }
}
