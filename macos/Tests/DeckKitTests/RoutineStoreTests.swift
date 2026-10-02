import XCTest
@testable import DeckKit
@testable import DeckUI

/// The routines panel lives in the inspector, which is per-agent, so the store
/// has to hand it that agent's routines and nobody else's.
@MainActor
final class RoutineStoreTests: XCTestCase {

    private func routine(_ id: String, agent: String, enabled: Bool = true) throws -> Routine {
        try DeckCoding.decoder.decode(Routine.self, from: Data("""
        {"id":"\(id)","agent":"\(agent)","prompt":"Morning sweep",
         "trigger":{"spec":"0 8 * * 1-5","tz":"Europe/London"},
         "next_run_at":1756100000.0,"enabled":\(enabled)}
        """.utf8))
    }

    private func store(_ client: ScriptedDeckClient) -> DeckStore {
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief"), makeAgent("hemingway")],
            threads: [makeThread("direct:chief", agent: "chief", at: 50),
                      makeThread("direct:hemingway", agent: "hemingway", at: 10)],
            sectionOrder: ["Work"]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        return DeckStore(client: client)
    }

    func testThePanelShowsOnlyTheOpenAgentsRoutines() async throws {
        let client = ScriptedDeckClient()
        client.routinesToReturn = [try routine("r1", agent: "chief"),
                                   try routine("r2", agent: "hemingway")]
        let deck = store(client)

        await deck.loadRoster()
        await deck.loadRoutines()

        XCTAssertEqual(deck.routines.map(\.id), ["r1"])
        XCTAssertEqual(deck.routines.first?.scheduleText, "Weekdays at 8:00 AM")
    }

    func testAddingARoutineSendsASpecAndATimezoneAndNoNextRunTime() async throws {
        let client = ScriptedDeckClient()
        client.routineToReturn = try routine("r9", agent: "chief")
        let deck = store(client)
        await deck.loadRoster()

        let added = await deck.addRoutine(
            RoutineDraft(agentName: "chief", prompt: "Morning sweep", spec: "0 8 * * 1-5")
        )

        XCTAssertTrue(added)
        let draft = try XCTUnwrap(client.routineDrafts.first)
        XCTAssertEqual(draft.spec, "0 8 * * 1-5")
        XCTAssertFalse(
            String(decoding: try draft.encodedBody(), as: UTF8.self).contains("next_run_at")
        )
        XCTAssertEqual(deck.routines.map(\.id), ["r9"])
    }

    func testDisablingARoutineKeepsWhateverTheDeckAnswered() async throws {
        let client = ScriptedDeckClient()
        client.routinesToReturn = [try routine("r1", agent: "chief")]
        client.routineToReturn = try routine("r1", agent: "chief", enabled: false)
        let deck = store(client)
        await deck.loadRoster()
        await deck.loadRoutines()

        await deck.setRoutine(id: "r1", enabled: false)

        XCTAssertEqual(deck.routines.first?.isEnabled, false)
        XCTAssertEqual(deck.routines.first?.nextRunText, "Paused")
    }

    func testDeletingARoutineRemovesItsRow() async throws {
        let client = ScriptedDeckClient()
        client.routinesToReturn = [try routine("r1", agent: "chief")]
        let deck = store(client)
        await deck.loadRoster()
        await deck.loadRoutines()

        await deck.deleteRoutine(id: "r1")

        XCTAssertTrue(deck.routines.isEmpty)
        XCTAssertEqual(
            deck.routinesEmptyMessage,
            "No routines yet. A routine sends this agent the same prompt on a schedule."
        )
    }

    func testAFailedRoutineLoadIsQuotedAndNotDrawnAsAnEmptyPanel() async {
        let client = ScriptedDeckClient()
        client.routinesError = .authNotConfigured
        let deck = store(client)
        await deck.loadRoster()

        await deck.loadRoutines()

        XCTAssertEqual(deck.routinesProblem, DeckError.authNotConfigured.userFacingText)
        XCTAssertNil(deck.routinesEmptyMessage)
    }
}
