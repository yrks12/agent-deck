import XCTest
@testable import DeckKit
@testable import DeckUI

/// `DeckStore` is the only file in the view layer holding state, so it is where
/// a reconnect or unread bug would actually hide. The views themselves render
/// what it decided and are not unit-tested.
@MainActor
final class DeckStoreTests: XCTestCase {

    private func roster(_ agents: [Agent], _ threads: [ThreadSummary]) -> RosterPayload {
        RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    // MARK: the four states

    func testTheRosterStartsLoadingAndEndsLoaded() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        guard case .loading = store.roster else {
            return XCTFail("a store that has not loaded yet must say so")
        }
        await store.loadRoster()

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected loaded, got \(store.roster)")
        }
        XCTAssertEqual(snapshot.sections.flatMap(\.rows).map(\.id), ["chief"])
    }

    func testAnEmptyDeckSaysEmptyRatherThanShowingABlankLoadedList() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster([], [])
        let store = DeckStore(client: client)

        await store.loadRoster()

        guard case .empty = store.roster else {
            return XCTFail("expected empty, got \(store.roster)")
        }
    }

    func testAClosedDeckSurfacesAsAFailureWithItsOwnAdvice() async {
        let client = ScriptedDeckClient()
        client.rosterError = .authNotConfigured
        let store = DeckStore(client: client)

        await store.loadRoster()

        guard case .failed(let error) = store.roster else {
            return XCTFail("expected failed, got \(store.roster)")
        }
        XCTAssertEqual(error, .authNotConfigured)
    }

    // MARK: the reconnect, through the store

    func testAcrossADropTheStoreEndsWithEveryMessageOnceInOrder() async throws {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [
            MessagePage(threadID: "direct:chief",
                        messages: [makeMessage("m1", cursor: "0001-m1"),
                                   makeMessage("m2", cursor: "0002-m2")],
                        isReadOnly: false, participants: ["owner", "chief"]),
            // Refetch after the drop; overlaps m3 and brings the missed m4.
            MessagePage(threadID: "direct:chief",
                        messages: [makeMessage("m3", cursor: "0003-m3"),
                                   makeMessage("m4", cursor: "0004-m4")],
                        isReadOnly: false, participants: ["owner", "chief"]),
        ]
        client.feeds = [
            .emitThenDrop([.message(makeMessage("m3", cursor: "0003-m3"), readOnly: false)]),
            .emitThenFinish([.message(makeMessage("m4", cursor: "0004-m4"), readOnly: false)]),
        ]
        let store = DeckStore(client: client, backoff: { _ in })

        await store.loadRoster()
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("expected a loaded thread, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.id), ["m1", "m2", "m3", "m4"])
        XCTAssertEqual(store.connection, .live, "and the indicator recovers")
    }

    func testTheRefetchAfterADropResumesFromTheCursorTheStoreHolds() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [
            MessagePage(threadID: "direct:chief", messages: [makeMessage("m1", cursor: "0001-m1")],
                        isReadOnly: false, participants: ["owner", "chief"]),
            MessagePage(threadID: "direct:chief", messages: [],
                        isReadOnly: false, participants: ["owner", "chief"]),
        ]
        client.feeds = [.emitThenDrop([]), .emitThenFinish([])]
        let store = DeckStore(client: client, backoff: { _ in })

        await store.loadRoster()
        await store.settle()

        XCTAssertEqual(client.historyCursors, [nil, "0001-m1"])
    }

    // MARK: read-only, end to end

    func testOpeningAPeerThreadLeavesTheStoreWithNoComposerToDraw() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")],
            [makeThread("peer:chief|hemingway", agent: "chief", readOnly: true,
                        participants: ["chief", "hemingway"])]
        )
        client.pages = [MessagePage(threadID: "peer:chief|hemingway", messages: [],
                                    isReadOnly: true, participants: ["chief", "hemingway"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("expected a loaded thread, got \(store.thread)")
        }
        XCTAssertEqual(
            screen.presentation.composer,
            .viewOnly(footer: "This chat is view-only", closeButtonTitle: "Close Chat")
        )
    }

    func testSendingIntoAPeerThreadNeverReachesTheNetwork() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")],
            [makeThread("peer:chief|hemingway", agent: "chief", readOnly: true,
                        participants: ["chief", "hemingway"])]
        )
        client.pages = [MessagePage(threadID: "peer:chief|hemingway", messages: [],
                                    isReadOnly: true, participants: ["chief", "hemingway"])]
        client.feeds = [.emitThenFinish([])]
        client.sendResult = makeMessage("mX", cursor: "9-mX")
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()
        await store.send("let me in")

        XCTAssertEqual(client.sendCallCount, 0)
        XCTAssertEqual(store.sendError, "This chat is view-only.")
    }

    func testSendingIntoADirectThreadPostsOnceAndClearsTheError() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        client.sendResult = makeMessage("mX", cursor: "9-mX")
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()
        await store.send("ship it")

        XCTAssertEqual(client.sendCallCount, 1)
        XCTAssertNil(store.sendError)
    }

    // MARK: unread

    func testSelectingAnAgentClearsItsUnreadOnTheDeckAndInTheSidebar() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief", unread: 4)]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()

        XCTAssertTrue(client.calls.contains(.markRead(agent: "chief")))
        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected loaded, got \(store.roster)")
        }
        XCTAssertEqual(snapshot.totalUnread, 0, "the badge clears without a second roster fetch")
        XCTAssertEqual(client.calls.filter { $0 == .roster }.count, 1)
    }

    func testALiveUnreadFrameUpdatesTheSidebarWithoutRefetchingTheRoster() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief"), makeAgent("hemingway")],
            [makeThread("direct:chief", agent: "chief"),
             makeThread("direct:hemingway", agent: "hemingway")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([.unread(agent: "hemingway", count: 7)])]
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected loaded, got \(store.roster)")
        }
        let hemingway = snapshot.sections.flatMap(\.rows).first { $0.id == "hemingway" }
        XCTAssertEqual(hemingway?.unreadCount, 7)
        XCTAssertEqual(client.calls.filter { $0 == .roster }.count, 1)
    }
}
