import XCTest
@testable import DeckKit
@testable import DeckUI

/// Measured by driving the running app: type a sentence, press Send, and the
/// composer empties — correctly, the deck took it — but the line appears
/// **nowhere**. It is not in the box any more and it is not in the transcript.
///
/// The machinery to prevent this already existed and was wired to one flow
/// only: `pendingEcho` is set when a new agent is hired, and `apply()` merges
/// it into the transcript until the deck's own copy arrives. The composer he
/// uses every day never set it, so his line simply vanished until the stream
/// happened to echo it back.
@MainActor
final class SentLineStaysOnScreenTests: XCTestCase {

    private func roster(_ agents: [Agent], _ threads: [ThreadSummary]) -> RosterPayload {
        RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    private func loadedStore(sendFails: Bool) async -> (DeckStore, ScriptedDeckClient) {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        if sendFails {
            client.sendError = .transport("connection refused")
        } else {
            client.sendResult = makeMessage("mX", cursor: "9-mX")
        }
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()
        return (store, client)
    }

    private func messagesOnScreen(_ store: DeckStore) -> [String] {
        guard case .loaded(let screen) = store.thread else { return [] }
        return screen.messages.map(\.text)
    }

    /// THE test. Send, and the sentence is still in front of him.
    func testHisLineIsOnScreenTheMomentItIsSent() async {
        let (store, _) = await loadedStore(sendFails: false)

        store.composerDraft = "wave 7 is merged"
        await store.submitComposer()

        XCTAssertTrue(
            messagesOnScreen(store).contains("wave 7 is merged"),
            "the composer emptied and the line went nowhere — on screen: \(messagesOnScreen(store))"
        )
    }

    /// It is his line, attributed to him, not dropped in as if the agent said
    /// it.
    func testTheLineOnScreenIsAttributedToHim() async {
        let (store, _) = await loadedStore(sendFails: false)

        store.composerDraft = "wave 7 is merged"
        await store.submitComposer()

        guard case .loaded(let screen) = store.thread,
              let echoed = screen.messages.first(where: { $0.text == "wave 7 is merged" })
        else { return XCTFail("nothing on screen to check") }
        XCTAssertTrue(echoed.isFromUser)
    }

    /// The other half of the class, and the one that would make this fix worse
    /// than the bug: a send the deck refused must NOT leave a line sitting
    /// there looking delivered. It stays in the composer instead.
    func testARefusedSendPutsNothingInTheTranscript() async {
        let (store, _) = await loadedStore(sendFails: true)

        store.composerDraft = "wave 7 is merged"
        await store.submitComposer()

        XCTAssertFalse(
            messagesOnScreen(store).contains("wave 7 is merged"),
            "a refused send drew a line that was never delivered"
        )
        XCTAssertEqual(store.composerDraft, "wave 7 is merged")
    }

    /// And it goes at the BOTTOM of a conversation that already has history.
    /// `ThreadEcho.merge` puts the echo first, which is right for the thread a
    /// hire's own sentence created — everything in there is an answer to it —
    /// and wrong for every other conversation, where his newest line would be
    /// drawn above messages from last week.
    func testHisLineGoesUnderTheConversationNotOverIt() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(
            threadID: "direct:chief",
            messages: [makeMessage("m1", cursor: "1-m1", text: "older", thread: "direct:chief"),
                       makeMessage("m2", cursor: "2-m2", text: "newer", thread: "direct:chief")],
            isReadOnly: false, participants: ["owner", "chief"]
        )]
        client.feeds = [.emitThenFinish([])]
        client.sendResult = makeMessage("mX", cursor: "9-mX")
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()

        store.composerDraft = "wave 7 is merged"
        await store.submitComposer()

        XCTAssertEqual(
            messagesOnScreen(store).last, "wave 7 is merged",
            "his line was not at the bottom — on screen: \(messagesOnScreen(store))"
        )
    }
}
