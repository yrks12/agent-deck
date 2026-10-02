import XCTest
@testable import DeckKit
@testable import DeckUI

/// Measured against the real deck: the centre pane sat on "Opening thread" for
/// as long as the window was open, while the sidebar had failed and the
/// inspector said "No agent selected". Nothing was in flight and nothing could
/// ever arrive. A spinner that cannot resolve is the worst empty state there
/// is — it tells the reader to wait for something that does not exist.
///
/// These pin the pane to *actual* state: nothing selected is the empty state,
/// a real fetch is the spinner, and a roster that would not load is reported
/// by the pane in the same terms the sidebar uses.
@MainActor
final class ThreadPaneStateTests: XCTestCase {

    private func roster(_ agents: [Agent], _ threads: [ThreadSummary]) -> RosterPayload {
        RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    // MARK: no spinner without a load

    func testAFreshWindowShowsTheEmptyStateRatherThanASpinner() {
        let store = DeckStore(client: ScriptedDeckClient())

        guard case .empty = store.thread else {
            return XCTFail("nothing is open and nothing is loading, so the pane must say so, got \(store.thread)")
        }
        XCTAssertNil(store.selectedThreadID, "there is no thread to be opening")
    }

    func testADeckWithNoDesksLeavesThePaneEmptyRatherThanSpinning() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster([], [])
        let store = DeckStore(client: client)

        await store.loadRoster()

        guard case .empty = store.thread else {
            return XCTFail("an empty roster selects nothing, so nothing is opening, got \(store.thread)")
        }
    }

    // MARK: the paired positive — a real load does spin

    func testOpeningARealThreadShowsTheSpinnerWhileItIsInFlight() {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        store.open(threadID: "direct:chief")

        guard case .loading = store.thread else {
            return XCTFail("a fetch really is in flight, so the spinner is the truth, got \(store.thread)")
        }
        XCTAssertEqual(store.selectedThreadID, "direct:chief")
    }

    func testTheSpinnerResolvesIntoTheConversation() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief",
                                    messages: [makeMessage("m1", thread: "direct:chief")],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("expected the conversation, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.id), ["m1"])
    }

    // MARK: the two panes never contradict each other

    func testAFailedRosterIsReportedByThePaneAndTheSidebarAsTheSameCondition() async {
        let client = ScriptedDeckClient()
        client.rosterError = .missingToken
        let store = DeckStore(client: client)

        await store.loadRoster()

        guard case .failed(let sidebar) = store.roster else {
            return XCTFail("expected the sidebar to fail, got \(store.roster)")
        }
        guard case .failed(let pane) = store.thread else {
            return XCTFail("the pane cannot spin while the sidebar has given up, got \(store.thread)")
        }
        XCTAssertEqual(sidebar, pane, "two panes, one condition — they must name the same one")
        XCTAssertEqual(pane, .missingToken)
    }

    func testATransportFailureIsAlsoCarriedToThePane() async {
        let client = ScriptedDeckClient()
        client.rosterError = .transport("connection refused")
        let store = DeckStore(client: client)

        await store.loadRoster()

        guard case .failed(let sidebar) = store.roster,
              case .failed(let pane) = store.thread else {
            return XCTFail("expected both to fail, got \(store.roster) / \(store.thread)")
        }
        XCTAssertEqual(sidebar, pane)
        XCTAssertEqual(pane, .transport("connection refused"))
    }

    /// The exception that keeps the rule honest: a conversation already on
    /// screen is not thrown away because a background roster refresh failed.
    func testAnOpenConversationSurvivesAFailedRosterRefresh() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief",
                                    messages: [makeMessage("m1", thread: "direct:chief")],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()

        client.rosterError = .transport("connection refused")
        await store.loadRoster()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the messages are still on screen; do not replace them, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.id), ["m1"])
    }
}
