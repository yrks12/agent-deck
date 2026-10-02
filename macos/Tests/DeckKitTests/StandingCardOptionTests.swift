import XCTest
@testable import DeckKit

private final class StubCard: StandingCardClient, @unchecked Sendable {
    var result: Result<StandingFromCardResult, DeckError> = .success(StandingFromCardResult(policies: []))
    private(set) var routes: [String] = []
    func standFromCard(route: String, _ body: StandingFromCard) async throws -> StandingFromCardResult {
        routes.append(route)
        return try result.get()
    }
}

/// **"Always, up to a limit…" on an approval card** (§23, from a card): the
/// row's `standing_option` is read, carried to both cards, posted to the
/// card's own route, and what it made is what the card then says.
final class StandingCardOptionTests: XCTestCase {
    private func approval(_ option: String?) throws -> Approval {
        let extra = option.map { #","standing_option":\#($0)"# } ?? ""
        let json = #"{"id":"ask_1","agent":"atlas","desk_known":true,"tool":"Bash","subject":"gh pr create","cwd":"/w","ts":5,"options":[{"reply":"once","available":true,"summary":"just this time","rule":null}]\#(extra)}"#
        return try DeckCoding.decoder.decode(Approval.self, from: Data(json.utf8))
    }

    private let offered = #"{"available":true,"kind":"run_command","desk":"atlas","route":"/v1/approvals/ask_1/standing","summary":"Let atlas do this up to a daily limit"}"#

    func testTheOptionIsReadOffTheApprovalRow() throws {
        let option = try XCTUnwrap(try approval(offered).standingOption)
        XCTAssertTrue(option.isAvailable)
        XCTAssertEqual(option.kind, .runCommand)
        XCTAssertEqual(option.route, "/v1/approvals/ask_1/standing")
        XCTAssertNil(try approval(nil).standingOption, "an older deck sends none")
        let routeless = #"{"available":true,"kind":"send_email","desk":"atlas","summary":"x"}"#
        XCTAssertEqual(try approval(routeless).standingOption?.isAvailable, false, "no route, nothing to post to")
    }

    func testBothCardsOfferItOnlyWhileTheQuestionIsLive() throws {
        let a = try approval(offered)
        XCTAssertTrue(ApprovalCard.make(approval: a, agents: [:]).offersStanding)
        XCTAssertFalse(ApprovalCard.make(approval: a, agents: [:],
                                         decision: ApprovalDecision(answered: "once", rule: nil, resumed: true)).offersStanding)
        XCTAssertTrue(AttentionItem.make(approval: a, agents: [:]).offersStanding)
        let refused = #"{"available":false,"kind":"run_command","desk":"atlas","route":"/v1/approvals/ask_1/standing","summary":"not available for this action"}"#
        XCTAssertFalse(ApprovalCard.make(approval: try approval(refused), agents: [:]).offersStanding)
        XCTAssertFalse(AttentionItem.make(approval: try approval(refused), agents: [:]).offersStanding)
    }

    func testTheHTTPClientPostsTheLimitsToTheCardsRoute() async throws {
        let p = StubPerformer()
        p.defaultBody = Data(#"{"ok":true,"policies":[{"id":"sa_9","status":"active","kind":"run_command","limits":{"count_per_day":20}}],"answered":{"ok":true,"ask":{"answered":"once"},"rule":null,"resumed":true}}"#.utf8)
        let store = InMemoryTokenStore(); try store.setToken("sekret")
        let http = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: p)
        let result = try await http.standFromCard(route: "/v1/approvals/ask_1/standing",
                                                  StandingFromCard(limits: StandingLimits(countPerDay: 20)))
        XCTAssertEqual(result.policies.map(\.id), ["sa_9"])
        XCTAssertEqual(result.answered?.answered, "once")
        XCTAssertEqual(p.requests[0].httpMethod, "POST")
        XCTAssertEqual(p.paths, ["/v1/approvals/ask_1/standing"])
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(p.requests[0].httpBody)) as? [String: Any])
        XCTAssertEqual((body["limits"] as? [String: Any])?["count_per_day"] as? Int, 20)
        XCTAssertNil(body["expires_at"], "no expiry is left out, not sent")
        do {
            _ = try await http.standFromCard(route: "/v1/agents/x", StandingFromCard(limits: StandingLimits(countPerDay: 1)))
            XCTFail("a route that is not a standing route must not be posted to")
        } catch {}
        XCTAssertEqual(p.requests.count, 1)
    }

    func testAStandingAnswerSettlesTheCardAndSaysTheLimit() async throws {
        let a = try approval(offered)
        let stub = StubCard()
        stub.result = .success(StandingFromCardResult(
            policies: [StandingPolicy(id: "sa_9", desk: "atlas", kind: .runCommand, limits: StandingLimits(countPerDay: 20))],
            answered: ApprovalDecision(answered: "once", rule: nil, resumed: true)))
        let model = ApprovalsModel(client: ScriptedDeckClient())
        let ok = await model.stand(approval: a, with: StandingFromCard(limits: StandingLimits(countPerDay: 20)), via: stub)
        XCTAssertTrue(ok)
        XCTAssertEqual(stub.routes, ["/v1/approvals/ask_1/standing"])
        let entries = await model.entries(forAgent: "atlas")
        let entry = try XCTUnwrap(entries.first)
        let card = ApprovalCard.make(approval: entry.approval, agents: [:], decision: entry.decision,
                                     settledElsewhere: entry.settledElsewhere, standing: entry.standing)
        XCTAssertEqual(card.status, .alwaysAllowed)
        XCTAssertFalse(card.isAnswerable)
        XCTAssertTrue(card.outcome?.contains("Up to 20 commands a day") == true, card.outcome ?? "nil")
    }

    func testARefusalKeepsTheCardLiveAndSaysWhy() async throws {
        let a = try approval(offered)
        let stub = StubCard()
        stub.result = .failure(.standingRefused(.neverCoverable, detail: "deleting data"))
        let deck = ScriptedDeckClient()
        deck.approvalsPage = ApprovalsPage(approvals: [a])
        let model = ApprovalsModel(client: deck)
        await model.load()
        let ok = await model.stand(approval: a, with: StandingFromCard(limits: StandingLimits(countPerDay: 1)), via: stub)
        XCTAssertFalse(ok)
        let problem = await model.problem
        XCTAssertTrue(problem?.contains("deleting data") == true)
        let entries = await model.entries(forAgent: "atlas")
        let entry = try XCTUnwrap(entries.first)
        XCTAssertNil(entry.decision)
        XCTAssertTrue(entry.standing.isEmpty)
    }

    func testTheFixtureAnswersFromACard() async throws {
        let r = try await FixtureDeckClient().standFromCard(route: "/v1/approvals/a/standing",
                                                            StandingFromCard(limits: StandingLimits(countPerDay: 3)))
        XCTAssertEqual(r.policies.first?.limits.countPerDay, 3)
    }
}
