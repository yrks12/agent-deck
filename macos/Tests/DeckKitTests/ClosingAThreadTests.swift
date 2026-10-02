import XCTest
@testable import DeckKit
@testable import DeckUI

/// Measured by driving the running app: open a desk, switch to its read-only
/// peer transcript, press the close button on the view-only footer, then click
/// that same desk in the sidebar to get back. Nothing happens. The pane keeps
/// saying "No conversation open" and there is no other route to the
/// conversation.
///
/// The cause is that closing cleared the *thread* and left the *agent*
/// selected. The sidebar's selection binding reads `selectedAgentName`, so the
/// row was still highlighted, clicking it did not change the List's selection,
/// and the setter that reopens a thread never ran.
@MainActor
final class ClosingAThreadTests: XCTestCase {

    private func store() async -> DeckStore {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief")],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        client.pages = [
            MessagePage(threadID: "direct:chief", messages: [],
                        isReadOnly: false, participants: ["owner", "chief"]),
            MessagePage(threadID: "direct:chief", messages: [],
                        isReadOnly: false, participants: ["owner", "chief"]),
        ]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()
        return store
    }

    /// THE test. Closing must let go of the desk as well as the thread, or the
    /// sidebar row he clicks is already selected and does nothing.
    func testClosingAThreadLetsGoOfTheDeskSoItCanBeOpenedAgain() async {
        let store = await store()
        XCTAssertEqual(store.selectedAgentName, "chief", "precondition: a desk is open")

        store.closeThread()

        XCTAssertNil(
            store.selectedAgentName,
            "the row stayed selected, so clicking it could not reopen the conversation"
        )
    }

    /// The behaviour that matters, rather than the flag: after closing,
    /// choosing the desk again puts a conversation back on screen.
    func testAfterClosingTheDeskCanBeOpenedAgain() async {
        let store = await store()
        store.closeThread()

        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()

        guard case .loaded = store.thread else {
            return XCTFail("reopening the desk did not put a conversation back — got \(store.thread)")
        }
    }

    /// And closing still does what it says: the conversation goes away.
    func testClosingStillClosesTheConversation() async {
        let store = await store()
        store.closeThread()

        XCTAssertNil(store.selectedThreadID)
        guard case .empty = store.thread else {
            return XCTFail("expected an empty pane after closing, got \(store.thread)")
        }
    }
}
