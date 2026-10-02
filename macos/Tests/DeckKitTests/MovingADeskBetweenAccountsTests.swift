import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"Move to account…" from the store's side (S12b).** The deck refuses in
/// three vocabularies (409 not_idle, 404 no_account, 502 move_failed) and each
/// one has to reach the screen as its own sentence, on the desk it was about.
/// A deck that cannot move at all says so, rather than doing nothing.
@MainActor
final class MovingADeskBetweenAccountsTests: XCTestCase {

    private func store(_ client: ScriptedDeckClient) -> DeckStore {
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief"), makeAgent("scout")],
            threads: [makeThread("direct:chief", agent: "chief", at: 50),
                      makeThread("direct:scout", agent: "scout", at: 10)],
            sectionOrder: ["Work"])
        client.feeds = [.emitThenFinish([])]
        return DeckStore(client: client, approvalPollInterval: 600)
    }

    func testAMoveAsksTheDeckAndSettlesWithNoProblemShown() async {
        let client = ScriptedDeckClient()
        let deck = store(client)
        await deck.loadRoster()

        await deck.moveDesk("scout", to: "work")

        XCTAssertEqual(client.moves.map(\.agent), ["scout"])
        XCTAssertEqual(client.moves.map(\.account), ["work"])
        XCTAssertNil(deck.accountMoves["scout"], "success leaves nothing on the row")
    }

    func testEachRefusalLandsOnTheDeskAsItsOwnSentence() async {
        let cases: [(AccountMoveError, String)] = [
            (.notIdle(detail: "scout is mid-turn"), "mid-turn"),
            (.noAccount(detail: "the deck has no account called work"), "no account called work"),
            (.moveFailed(detail: ""), "still on its old account"),
        ]
        for (error, fragment) in cases {
            let client = ScriptedDeckClient()
            client.moveError = error
            let deck = store(client)
            await deck.loadRoster()

            await deck.moveDesk("scout", to: "work")

            guard case .failed(let text)? = deck.accountMoves["scout"] else {
                return XCTFail("\(error): no failure shown, got \(String(describing: deck.accountMoves["scout"]))")
            }
            XCTAssertTrue(text.contains(fragment), "\(error): \(text)")
            XCTAssertNil(deck.accountMoves["chief"], "only the desk it was about")
        }
    }

    func testAClientThatCannotMoveSaysSoRatherThanDoingNothing() async {
        // The fixture deck has no accounts routes.
        let deck = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await deck.loadRoster()
        let name = deck.selectedAgentName ?? "chief"

        await deck.moveDesk(name, to: "work")

        guard case .failed(let text)? = deck.accountMoves[name] else {
            return XCTFail("a deck that cannot move must say so")
        }
        XCTAssertEqual(text, AccountMoveError.unsupported.userFacingText)
    }

    func testTheProblemIsClearedWhenHeTriesAgainAndItWorks() async {
        let client = ScriptedDeckClient()
        client.moveError = .notIdle(detail: "")
        let deck = store(client)
        await deck.loadRoster()
        await deck.moveDesk("scout", to: "work")
        XCTAssertNotNil(deck.accountMoves["scout"])

        client.moveError = nil
        await deck.moveDesk("scout", to: "work")

        XCTAssertNil(deck.accountMoves["scout"])
    }

    func testDismissingAProblemClearsIt() async {
        let client = ScriptedDeckClient()
        client.moveError = .moveFailed(detail: "")
        let deck = store(client)
        await deck.loadRoster()
        await deck.moveDesk("scout", to: "work")

        deck.dismissAccountMove("scout")

        XCTAssertNil(deck.accountMoves["scout"])
    }
}
