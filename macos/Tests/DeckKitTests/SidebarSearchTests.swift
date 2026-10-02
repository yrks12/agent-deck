import XCTest
@testable import DeckKit
@testable import DeckUI

/// The search field at the top of the sidebar.
///
/// The failure this pins is the quiet one: a query that matches nothing
/// rendering an empty list, which is indistinguishable from a roster that
/// failed to load. It has to say so, in words, with the query in it.
@MainActor
final class SidebarSearchTests: XCTestCase {

    private func payload() -> RosterPayload {
        var hemingway = makeAgent("hemingway", title: "Designer")
        hemingway.detail = "Turns rough notes into prose."
        var seeker = makeAgent("seeker", title: "Researcher")
        seeker.detail = "Digs up sources."
        let ledger = makeAgent("ledger", title: "Email")
        return RosterPayload(
            agents: [hemingway, seeker, ledger],
            threads: [
                makeThread("direct:hemingway", agent: "hemingway", at: 30,
                           preview: ThreadPreview(text: "the second draft reads better")),
                makeThread("direct:seeker", agent: "seeker", at: 20,
                           preview: ThreadPreview(text: "three papers, one contradicts you")),
                makeThread("direct:ledger", agent: "ledger", at: 10,
                           preview: ThreadPreview(text: "nine replies drafted")),
            ],
            sectionOrder: ["Work"]
        )
    }

    private func names(_ result: SidebarSearchResult) -> [String] {
        result.snapshot.sections.flatMap(\.rows).map(\.id)
    }

    // MARK: what it matches

    func testItMatchesOnName() {
        let result = SidebarSearch.result(payload: payload(), query: "hem")

        XCTAssertEqual(names(result), ["hemingway"])
        XCTAssertEqual(result.matchCount, 1)
    }

    func testItMatchesOnTheTitleChip() {
        XCTAssertEqual(names(SidebarSearch.result(payload: payload(), query: "researcher")),
                       ["seeker"])
    }

    func testItMatchesOnThePreviewLine() {
        XCTAssertEqual(names(SidebarSearch.result(payload: payload(), query: "papers")),
                       ["seeker"])
    }

    func testItIgnoresCaseAndSurroundingSpace() {
        XCTAssertEqual(names(SidebarSearch.result(payload: payload(), query: "  LEDGER ")),
                       ["ledger"])
    }

    func testNoQueryIsNotASearchAndKeepsEveryone() {
        let result = SidebarSearch.result(payload: payload(), query: "   ")

        XCTAssertFalse(result.isSearching)
        XCTAssertEqual(names(result).count, 3)
        XCTAssertNil(result.emptyTitle)
    }

    // MARK: the failure this exists for

    func testAQueryThatMatchesNothingSaysSoRatherThanShowingAnEmptyList() {
        let result = SidebarSearch.result(payload: payload(), query: "zzz")

        XCTAssertEqual(result.matchCount, 0)
        XCTAssertEqual(result.emptyTitle, "No agents match “zzz”")
        XCTAssertEqual(
            result.emptyDescription,
            "Search looks at the name, the title and the last message. Try a shorter word."
        )
    }

    /// Rows folded below "+ N more unreads" are still on this deck, so a
    /// search that could not see them would be lying.
    func testSearchReachesRowsThatAreFoldedBelowTheFold() {
        let result = SidebarSearch.result(payload: payload(), query: "ledger")

        XCTAssertEqual(names(result), ["ledger"])
        XCTAssertTrue(
            result.snapshot.sections.allSatisfy { !$0.hasOverflow },
            "a filtered list has nothing hidden underneath it"
        )
    }

    func testAnEmptySectionIsDroppedRatherThanDrawnAsAHeadingWithNothingUnderIt() {
        var personal = makeAgent("larder", section: "Personal", title: "Email")
        personal.preview = "renewals"
        var payload = payload()
        payload.agents.append(personal)
        payload.sectionOrder = ["Work", "Personal"]

        let result = SidebarSearch.result(payload: payload, query: "hem")

        XCTAssertEqual(result.snapshot.sections.map(\.name), ["Work"])
    }

    // MARK: through the store

    func testTheStoreFiltersTheSidebarAndCanBeCleared() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = payload()
        client.pages = [MessagePage(threadID: "direct:hemingway", messages: [],
                                    isReadOnly: false, participants: ["owner", "hemingway"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()

        store.search("seek")
        guard case .loaded(let filtered) = store.roster else {
            return XCTFail("expected a loaded, filtered sidebar")
        }
        XCTAssertEqual(filtered.sections.flatMap(\.rows).map(\.id), ["seeker"])

        store.search("")
        guard case .loaded(let restored) = store.roster else {
            return XCTFail("clearing the field restores the roster")
        }
        XCTAssertEqual(restored.sections.flatMap(\.rows).count, 3)
        XCTAssertNil(store.searchOutcome?.emptyTitle)
    }

    func testTheStoreKeepsTheEmptyStateSentenceForAQueryWithNoMatches() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = payload()
        client.pages = [MessagePage(threadID: "direct:hemingway", messages: [],
                                    isReadOnly: false, participants: ["owner", "hemingway"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()

        store.search("qqq")

        XCTAssertEqual(store.searchOutcome?.emptyTitle, "No agents match “qqq”")
        if case .loaded(let snapshot) = store.roster {
            XCTAssertTrue(snapshot.sections.flatMap(\.rows).isEmpty)
        }
        // The deck is fine — this must never read as "no agents on this deck".
        XCTAssertEqual(store.agents.count, 3)
    }

    /// A search is a client-side filter over what the sidebar already has.
    func testSearchingCostsNoExtraRequest() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = payload()
        client.pages = [MessagePage(threadID: "direct:hemingway", messages: [],
                                    isReadOnly: false, participants: ["owner", "hemingway"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        let before = client.calls.count

        store.search("hem")
        store.search("ledger")

        XCTAssertEqual(client.calls.count, before, "filtering is local; it is not a query")
    }
}
