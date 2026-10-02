import XCTest
@testable import DeckKit
@testable import DeckUI

/// Three ways this app could take something away from the person using it and
/// leave him with no way back. All three were reachable in the first minute of
/// driving the real app.
@MainActor
final class FailureRecoveryTests: XCTestCase {

    private func roster(_ agents: [Agent], _ threads: [ThreadSummary]) -> RosterPayload {
        RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    // MARK: a failed roster stays a failed roster

    /// The sidebar could not load, so he types a name to look for the desk he
    /// wanted. `search` rebuilt the list from a payload that was never
    /// fetched, so the failure screen -- and the only retry button on it --
    /// was replaced by a sentence about *his deck* being empty.
    func testTypingInSearchDoesNotTurnAFailedRosterIntoAnEmptyDeck() async {
        let client = ScriptedDeckClient()
        client.rosterError = .transport("connection refused")
        let store = DeckStore(client: client)

        await store.loadRoster()
        guard case .failed = store.roster else {
            return XCTFail("precondition: the load must have failed")
        }

        store.search("chief")

        guard case .failed(let error) = store.roster else {
            return XCTFail("a search cannot invent a roster the deck never sent — got \(store.roster)")
        }
        XCTAssertEqual(error, .transport("connection refused"))
    }

    /// And clearing the field must not "resolve" it into an empty deck either
    /// -- that was the worse half: the failure screen never came back.
    func testClearingTheSearchLeavesTheFailureOnScreen() async {
        let client = ScriptedDeckClient()
        client.rosterError = .transport("connection refused")
        let store = DeckStore(client: client)

        await store.loadRoster()
        store.search("chief")
        store.search("")

        guard case .failed = store.roster else {
            return XCTFail("expected the failure to still be on screen, got \(store.roster)")
        }
    }

    /// The sweep: a loaded roster must still filter normally, or the guard
    /// above would have been "fixed" by breaking search altogether.
    func testSearchStillFiltersALoadedRoster() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief"), makeAgent("hemingway")],
            [makeThread("direct:chief", agent: "chief"),
             makeThread("direct:hemingway", agent: "hemingway")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()
        store.search("hemingway")

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected loaded, got \(store.roster)")
        }
        XCTAssertEqual(snapshot.sections.flatMap(\.rows).map(\.id), ["hemingway"])
    }

    // MARK: a failed send keeps his sentence

    /// An empty composer is the universal signal for "that went". Clearing it
    /// before the deck has answered means a refusal reads as a success and the
    /// sentence is gone. The hire flow already gets this right --
    /// `PendingThread.typedText` exists for exactly this reason -- so the
    /// composer he uses every day is held to the same rule.
    func testAFailedSendKeepsWhatHeTyped() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        client.sendError = .transport("connection refused")
        let store = DeckStore(client: client)

        await store.loadRoster()
        await store.settle()
        store.composerDraft = "the thing I typed once and will not type twice"
        await store.submitComposer()

        XCTAssertEqual(
            store.composerDraft, "the thing I typed once and will not type twice",
            "a refused send must leave the sentence where he can send it again"
        )
        XCTAssertNotNil(store.sendError)
    }

    /// The other half of the class: a send that worked must empty the box, or
    /// he sends everything twice.
    func testASentMessageClearsTheComposer() async {
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
        store.composerDraft = "ship it"
        await store.submitComposer()

        XCTAssertEqual(store.composerDraft, "")
        XCTAssertNil(store.sendError)
        XCTAssertEqual(client.sendCallCount, 1)
    }

    /// A view-only thread refuses before the socket, and that refusal is not a
    /// send either — so it keeps the text too.
    func testAViewOnlyRefusalAlsoKeepsWhatHeTyped() async {
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
        store.composerDraft = "let me in"
        await store.submitComposer()

        XCTAssertEqual(store.composerDraft, "let me in")
        XCTAssertEqual(client.sendCallCount, 0)
    }

    // MARK: saving a token makes the window behind it recover

    /// The first thing anyone does with this app: open Settings, paste the
    /// deck's token, save. Nothing re-asked the deck, so the window behind
    /// still said "Not connected yet" and the only reading available was that
    /// the token had been rejected.
    func testSavingATokenMakesTheRosterTryAgainWithoutARelaunch() async {
        let client = ScriptedDeckClient()
        client.rosterError = .missingToken
        let store = DeckStore(client: client)

        await store.loadRoster()
        guard case .failed(.missingToken) = store.roster else {
            return XCTFail("precondition: an empty Keychain must read as missingToken")
        }

        // The token goes in, and the deck now answers.
        client.rosterError = nil
        client.rosterPayload = roster(
            [makeAgent("chief")], [makeThread("direct:chief", agent: "chief")]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]

        NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
        // The store reloads on the main actor; give that task a turn to run.
        for _ in 0..<50 {
            if case .loaded = store.roster { break }
            await Task.yield()
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("saving a token must reload the roster, got \(store.roster)")
        }
        XCTAssertEqual(snapshot.sections.flatMap(\.rows).map(\.id), ["chief"])
    }
}
