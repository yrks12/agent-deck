import XCTest
@testable import DeckKit
@testable import DeckUI

/// **Watch mode**: the agent's computer takes the main window and the chat is
/// a side panel. Opened from the screen button's sheet or ⌘⇧W, closed the same
/// way; never opened by itself, never on a deck that cannot show a screen.
@MainActor
final class WatchModeTests: XCTestCase {

    private func deck(canServeScreen: Bool = true) async -> DeckStore {
        let client = ScriptedDeckClient()
        let atlas = Agent(name: "atlas", title: "Researcher", section: "Work",
                          state: .working, workspace: "/home/agent")
        client.rosterPayload = RosterPayload(
            agents: [atlas],
            threads: [makeThread("direct:atlas", agent: "atlas")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:atlas", messages: [],
                                    isReadOnly: false, participants: ["owner", "atlas"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(
            client: client, shell: FakeShellClient(),
            screens: canServeScreen
                ? FakeScreenClient(status: TheTakeOverIsOneClickFromWhereHeIsNeededTests.screenStatus)
                : nil,
            approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()
        return store
    }

    func testNothingIsWatchedUntilHeAsks() async {
        let store = await deck()
        XCTAssertNil(store.watching)
    }

    func testTheShortcutWatchesTheDeskInViewAndClosesIt() async {
        let store = await deck()
        store.toggleWatch()
        XCTAssertEqual(store.watching?.desk, "atlas")
        store.toggleWatch()
        XCTAssertNil(store.watching)
    }

    func testMovingTheSheetIntoWatchClosesTheSheet() async {
        let store = await deck()
        store.takeOver(focus: .screen)
        XCTAssertNotNil(store.takeover)
        store.watch(desk: "atlas")
        XCTAssertEqual(store.watching?.desk, "atlas")
        XCTAssertNil(store.takeover, "the computer is in one place at a time")
    }

    func testADeckWithNoScreenCannotBeWatched() async {
        let store = await deck(canServeScreen: false)
        store.toggleWatch()
        XCTAssertNil(store.watching)
        store.watch(desk: "atlas")
        XCTAssertNil(store.watching)
    }

    func testAnUnknownDeskIsNotWatched() async {
        let store = await deck()
        store.watch(desk: "ghost")
        XCTAssertNil(store.watching)
    }

    func testEndWatchCloses() async {
        let store = await deck()
        store.watch(desk: "atlas")
        store.endWatch()
        XCTAssertNil(store.watching)
    }
}
