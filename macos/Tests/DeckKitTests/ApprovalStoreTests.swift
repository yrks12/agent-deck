import XCTest
@testable import DeckKit
@testable import DeckUI

/// The card is drawn *inside a conversation*, so the store has to hand the
/// thread pane only the approvals that belong under the agent on screen — and
/// has to keep the rule text on every option all the way to the view.
@MainActor
final class ApprovalStoreTests: XCTestCase {

    private func page(_ agents: [String]) throws -> ApprovalsPage {
        let entries = agents.enumerated().map { index, agent in
            """
            {"id":"apr_\(index)","ts":175600000\(index).0,"agent":"\(agent)","tool":"Bash",
             "subject":"gh pr list","cwd":"/Users/sam/Projects/acme",
             "cwd_short":"~/Projects/acme","status":"pending","options":[
               {"reply":"once","available":true,"rule":null,"summary":"just this time"},
               {"reply":"always","available":true,
                "summary":"allow `gh pr*` in ~/Projects/acme, never ask again",
                "rule":{"id":"ask-1","kind":"always_allow","tool":"Bash","pattern":"gh pr*",
                        "cwd":"/Users/sam/Projects/acme","note":"from chief"}},
               {"reply":"never","available":true,
                "summary":"refuse `gh pr*` in ~/Projects/acme from now on",
                "rule":{"id":"ask-1","kind":"deny","tool":"Bash","pattern":"gh pr*",
                        "cwd":"/Users/sam/Projects/acme","note":"from chief"}}]}
            """
        }
        return try DeckCoding.decoder.decode(
            ApprovalsPage.self,
            from: Data("{\"approvals\":[\(entries.joined(separator: ","))]}".utf8)
        )
    }

    private func store(_ client: ScriptedDeckClient) -> DeckStore {
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief"), makeAgent("hemingway")],
            threads: [makeThread("direct:chief", agent: "chief"),
                      makeThread("direct:hemingway", agent: "hemingway")],
            sectionOrder: ["Work"]
        )
        client.pages = [
            MessagePage(threadID: "direct:chief", messages: [],
                        isReadOnly: false, participants: ["owner", "chief"]),
            MessagePage(threadID: "direct:hemingway", messages: [],
                        isReadOnly: false, participants: ["owner", "hemingway"]),
        ]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        return DeckStore(client: client)
    }

    func testOnlyTheOpenAgentsApprovalsAppearInItsThread() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page(["chief", "hemingway"])
        let deck = store(client)

        await deck.loadRoster()
        await deck.loadApprovals()

        XCTAssertEqual(deck.selectedAgentName, "chief")
        XCTAssertEqual(deck.approvals.map(\.approvalID), ["apr_0"])
        XCTAssertEqual(
            deck.approvals.first?.options.map(\.ruleText),
            ["just this time",
             "allow `gh pr*` in ~/Projects/acme, never ask again",
             "refuse `gh pr*` in ~/Projects/acme from now on"],
            "the deck's sentence survives all the way to the view layer"
        )
    }

    /// **This used to assert the card was deleted, and that was the defect.**
    ///
    /// He tapped Approve, the card vanished, and the conversation went back to
    /// exactly what it had been — nothing on screen saying which standing
    /// permission had just been written on his Mac, or whether the desk had
    /// carried on. So the answered card stays, in the conversation, saying what
    /// it did; only the *buttons* go, and only that one card's.
    func testAnsweringFromTheThreadSettlesThatCardAndNothingElse() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page(["chief", "chief"])
        let deck = store(client)
        await deck.loadRoster()
        await deck.loadApprovals()
        XCTAssertEqual(deck.approvals.count, 2)

        let card = try XCTUnwrap(deck.approvals.first)
        let always = try XCTUnwrap(card.options.first { $0.reply == .always })
        await deck.answer(card: card, with: always)

        XCTAssertEqual(deck.approvals.map(\.approvalID), ["apr_0", "apr_1"],
                       "the record of what he just granted has to stay on screen")
        XCTAssertEqual(deck.approvals.map(\.statusPill), ["Always allowed", "Waiting on you"],
                       "one is settled and one is still asking, and they must not read alike")
        XCTAssertEqual(deck.approvals.map(\.isAnswerable), [false, true],
                       "a settled card that still offered to grant would double-grant it")
        XCTAssertEqual(client.decidedApprovals.map(\.1), ["always"])
    }

    func testARefusedAnswerLeavesTheCardUpAndSaysWhy() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page(["chief"])
        client.decideError = .queueFailed
        let deck = store(client)
        await deck.loadRoster()
        await deck.loadApprovals()

        let card = try XCTUnwrap(deck.approvals.first)
        await deck.answer(card: card, with: card.options[0])

        XCTAssertEqual(deck.approvals.count, 1, "nothing was granted, so nothing disappears")
        XCTAssertEqual(deck.approvalProblem, DeckError.queueFailed.userFacingText)
    }

    func testAnUnreadableApprovalIsAnnouncedRatherThanVanishing() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try DeckCoding.decoder.decode(
            ApprovalsPage.self,
            // An option with no summary: unanswerable here, by design.
            from: Data(#"""
            {"approvals":[{"id":"a","ts":1.0,"agent":"chief","tool":"Bash","subject":"rm -rf x",
              "cwd":"/tmp","cwd_short":"/tmp","status":"pending",
              "options":[{"reply":"once","available":true,"rule":null}]}]}
            """#.utf8)
        )
        let deck = store(client)

        await deck.loadRoster()
        await deck.loadApprovals()

        XCTAssertTrue(deck.approvals.isEmpty)
        XCTAssertEqual(
            deck.approvalNotice,
            "1 approval request could not be read and is not shown here. Answer it in the agent's own terminal."
        )
    }
}
