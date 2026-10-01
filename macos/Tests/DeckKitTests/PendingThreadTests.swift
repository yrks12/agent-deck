import XCTest
@testable import DeckKit
@testable import DeckUI

/// "+" is a conversation, not a form.
///
/// Pressing it must put a thread on screen with a composer under it and create
/// nothing. The first thing typed into that composer is what hires the desk,
/// and the thread he typed it into is the thread the reply arrives in — no
/// sheet, no second window, no re-typing.
///
/// The failure that would actually hurt is the refusal: a create that comes
/// back `409` while his sentence is the only copy of what he wanted. These pin
/// that the sentence survives it.
@MainActor
final class PendingThreadTests: XCTestCase {

    private actor Box<Value: Sendable> {
        private(set) var value: Value?
        func set(_ value: Value?) { self.value = value }
    }

    private func provisionalDesk() -> Agent {
        Agent(name: "agent-7f3", title: "Working it out…", detail: "",
              section: "Work", threadID: "direct:agent-7f3")
    }

    /// A client that hires successfully and answers the new thread with the
    /// desk's first line.
    private func hiringClient(replying reply: String = "On it — watching your PRs.") -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        client.interviewResult = provisionalDesk()
        client.pages = [MessagePage(
            threadID: "direct:agent-7f3",
            messages: [makeMessage("m1", text: reply, thread: "direct:agent-7f3",
                                   author: "agent-7f3", ts: 1_800_000_000)],
            isReadOnly: false,
            participants: ["owner", "agent-7f3"]
        )]
        client.feeds = [.emitThenFinish([])]
        return client
    }


    // MARK: pressing "+"

    func testPlusOpensAConversationAndHiresNobodyYet() {
        let client = ScriptedDeckClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()

        guard let pending = store.pending else {
            return XCTFail("+ must land him in a thread, not a sheet")
        }
        XCTAssertEqual(
            pending.openingLine,
            "What do you want this one for? Say it however you like — I'll work out what to call myself."
        )
        XCTAssertTrue(pending.composerIsEnabled, "there is a composer to talk into straight away")
        XCTAssertTrue(client.calls.isEmpty, "nothing is created until he says something, got \(client.calls)")
    }

    func testTheSidebarCarriesAProvisionalRowWhileTheThreadIsUnsaved() {
        let store = DeckStore(client: ScriptedDeckClient())

        store.beginNewAgent()
        store.typePending("watch my open PRs")

        XCTAssertEqual(store.pending?.row.title, PendingThread.rowTitle)
        XCTAssertEqual(store.pending?.row.detail, "watch my open PRs",
                       "the provisional row shows what he has said so far")
        XCTAssertEqual(store.pending?.row.isProvisional, true)
    }

    func testTypingIsRememberedAndStillSendsNothing() {
        let client = ScriptedDeckClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")

        XCTAssertEqual(store.pending?.typedText, "watch my open PRs")
        XCTAssertTrue(client.calls.isEmpty, "typing is not a request, got \(client.calls)")
    }

    // MARK: the first message is the hire

    func testTheFirstMessageIsWhatCreatesTheAgent() async {
        let client = hiringClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        let hired = await store.submitPending()
        await store.settle()

        XCTAssertTrue(hired)
        XCTAssertEqual(client.interviewDrafts.map(\.roleHint), ["watch my open PRs"],
                       "what he typed is the role hint")
        XCTAssertEqual(store.selectedThreadID, "direct:agent-7f3")
        XCTAssertNil(store.pending, "the provisional thread became the real one")
    }

    func testHisOwnMessageIsStillOnScreenInTheThreadThatOpens() async {
        let client = hiringClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the reply arrives where he typed, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.text),
                       ["watch my open PRs", "On it — watching your PRs."],
                       "his line first, then the answer — same place, no transition")
        XCTAssertEqual(screen.messages.first?.role, .owner)
    }

    func testTheDecksOwnCopyOfHisLineIsNotDrawnTwice() async {
        let client = ScriptedDeckClient()
        client.interviewResult = provisionalDesk()
        client.pages = [MessagePage(
            threadID: "direct:agent-7f3",
            messages: [
                makeMessage("m0", text: "watch my open PRs", thread: "direct:agent-7f3",
                            author: DeckOwner.name, role: .owner, ts: 1_700_000_000),
                makeMessage("m1", text: "On it.", thread: "direct:agent-7f3",
                            author: "agent-7f3", ts: 1_800_000_000)
            ],
            isReadOnly: false,
            participants: ["owner", "agent-7f3"]
        )]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("expected the new thread, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.filter { $0.role == .owner }.map(\.text),
                       ["watch my open PRs"],
                       "the deck recorded his line too; he said it once")
    }

    func testTheRosterGainsOneDeskAndNoProvisionalRowSurvives() async {
        let client = hiringClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        await store.settle()

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected a loaded roster, got \(store.roster)")
        }
        XCTAssertEqual(snapshot.sections.flatMap(\.rows).map(\.id), ["agent-7f3"])
        XCTAssertNil(store.pending, "the provisional row must not linger beside the real one")
    }

    // MARK: the refusal — what he typed must survive it

    func testAFailedCreateKeepsWhatHeTypedAndSaysWhyInTheThread() async {
        let client = ScriptedDeckClient()
        client.interviewError = .createRefused(.tooManyLive, detail: "eight sessions are already live")
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        let hired = await store.submitPending()

        XCTAssertFalse(hired)
        guard let pending = store.pending else {
            return XCTFail("the thread stays open on a refusal — it is where the error is shown")
        }
        XCTAssertEqual(pending.typedText, "watch my open PRs",
                       "losing what he typed is the one thing this must never do")
        XCTAssertEqual(
            pending.problem,
            DeckError.createRefused(.tooManyLive, detail: "eight sessions are already live").userFacingText
        )
        XCTAssertTrue(pending.composerIsEnabled, "he can try again without retyping")
    }

    func testHeCanRetryTheSameSentenceAfterARefusal() async {
        let client = ScriptedDeckClient()
        client.interviewError = .createRefused(.tooManyLive, detail: "eight sessions are already live")
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()

        client.interviewError = nil
        client.interviewResult = provisionalDesk()
        client.pages = [MessagePage(threadID: "direct:agent-7f3", messages: [],
                                    isReadOnly: false, participants: ["owner", "agent-7f3"])]
        client.feeds = [.emitThenFinish([])]
        let hired = await store.submitPending()

        XCTAssertTrue(hired)
        XCTAssertEqual(client.interviewDrafts.map(\.roleHint), ["watch my open PRs"],
                       "the retry sent the sentence he never had to type twice")
    }

    func testTheComposerIsDisabledWhileTheDeckIsBeingAsked() async {
        let client = hiringClient()
        let store = DeckStore(client: client)
        let seen = Box<PendingThread>()
        client.onInterview = { [weak store] in
            let snapshot = await MainActor.run { store?.pending }
            await seen.set(snapshot)
        }

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        await store.settle()

        let inFlight = await seen.value
        XCTAssertEqual(inFlight?.isCreating, true)
        XCTAssertEqual(inFlight?.composerIsEnabled, false, "the composer disables while it is working")
        XCTAssertEqual(inFlight?.statusLine, PendingThread.workingLine,
                       "and the thread says it is working")
    }

    func testAnEmptyMessageHiresNobodyAndSaysSo() async {
        let client = ScriptedDeckClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("   ")
        let hired = await store.submitPending()

        XCTAssertFalse(hired)
        XCTAssertTrue(client.calls.isEmpty, "nothing was asked of the deck, got \(client.calls)")
        XCTAssertEqual(store.pending?.problem,
                       "Say what you want this one for — that is the whole question.")
    }

    // MARK: walking away

    func testOpeningAnotherThreadDiscardsThePendingOneWithoutCreatingAnything() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief")],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        store.select(agent: "chief", threadID: "direct:chief")

        XCTAssertNil(store.pending, "he walked away from it; it was never a desk")
        XCTAssertFalse(client.calls.contains(.startInterview),
                       "nothing was hired on the way out, got \(client.calls)")
    }

    func testCancellingDiscardsItWithoutCreatingAnything() {
        let client = ScriptedDeckClient()
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        store.cancelPending()

        XCTAssertNil(store.pending)
        XCTAssertTrue(client.calls.isEmpty, "Escape creates nothing, got \(client.calls)")
    }

    // MARK: the second door

    func testSettingItUpManuallyIsStillReachableAndKeepsWhatHeTyped() {
        let store = DeckStore(client: ScriptedDeckClient())

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        store.showManualSetup()

        XCTAssertTrue(store.isShowingManualSetup, "the second door is still there")
        XCTAssertEqual(store.pending?.typedText, "watch my open PRs",
                       "peeking at the form is not abandoning the sentence")
    }

    func testHiringThroughTheSecondDoorClearsTheProvisionalRow() async {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: "direct:scribe", messages: [],
                                    isReadOnly: false, participants: ["owner", "scribe"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.showManualSetup()
        let created = await store.createAgent(
            AgentDraft(name: "scribe", title: "Notes", directory: "~/Projects/acme")
        )
        await store.settle()

        XCTAssertTrue(created)
        XCTAssertNil(store.pending, "a desk exists now; the provisional row is not a second one")
    }
}
