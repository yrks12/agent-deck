import XCTest
@testable import DeckKit
@testable import DeckUI

/// Hitting "+" should not be a form. You say what you want the desk *for*, and
/// the desk names itself — its name, its title and its description come back
/// from the agent, not from six text fields the owner had to invent answers for.
///
/// The failure that will actually bite is the rename: it lands while the owner
/// is mid-conversation with the thing. The conversation he is reading must not
/// vanish, reload, or turn into a second sidebar row.
@MainActor
final class InterviewTests: XCTestCase {

    private func roster(_ agents: [Agent], _ threads: [ThreadSummary]) -> RosterPayload {
        RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    // MARK: what "+" asks for

    /// The deck allocates every new desk its own workspace, so there is nothing
    /// left to pick: one sentence is the whole interview.
    func testTheOnlyThingAskedForIsWhatItIsFor() {
        XCTAssertNil(InterviewDraft(roleHint: "watch my open PRs").problem)
    }

    func testAnEmptyPurposeIsRefusedBeforeAnythingIsSent() {
        let problem = InterviewDraft(roleHint: "   ").problem

        XCTAssertEqual(problem, "Say what you want this one for — that is the whole question.")
    }

    func testAnEmptyMessageIsTheOnlyThingThatCanBlockASend() {
        XCTAssertNil(InterviewDraft(roleHint: "watch my open PRs").problem)
        XCTAssertNotNil(InterviewDraft(roleHint: " ").problem)
    }

    /// The detector for "no folder, ever": the wire body is `role_hint` and
    /// nothing else. A `cwd` back in this dictionary is a folder question back
    /// on his screen.
    func testNoFolderIsSentBecauseTheDeckAllocatesTheWorkspace() {
        let body = InterviewDraft(roleHint: "  watch my open PRs  ").wireBody

        XCTAssertEqual(body, ["role_hint": "watch my open PRs"])
    }

    func testEngineAndBossStillRideAlongAndAFolderStillDoesNot() {
        let body = InterviewDraft(
            roleHint: "watch my open PRs", engine: "claude", reportsTo: "chief"
        ).wireBody

        XCTAssertEqual(body, ["role_hint": "watch my open PRs",
                              "engine": "claude",
                              "reports_to": "chief"])
    }

    // MARK: the desk names itself

    func testTheOwnerTypesNoNameAndTheDeckSuppliesOne() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        let started = await store.submitPending()

        XCTAssertTrue(started)
        XCTAssertEqual(client.interviewDrafts.map(\.roleHint), ["watch my open PRs"])
        XCTAssertEqual(store.agents.keys.sorted(), ["agent-7f3"],
                       "the name came from the deck, not from a field")
    }

    func testTheConversationOpensStraightAwaySoTheRestHappensByTalking() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        client.pages = [MessagePage(threadID: "direct:agent-7f3",
                                    messages: [makeMessage("m1",
                                                           text: "What do you want me for?", thread: "direct:agent-7f3")],
                                    isReadOnly: false, participants: ["owner", "agent-7f3"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        await store.settle()

        XCTAssertEqual(store.selectedThreadID, "direct:agent-7f3")
        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the point of the flow is that a conversation opens, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.text),
                       ["watch my open PRs", "What do you want me for?"],
                       "his own line, then the desk's — he never re-typed it")
    }

    func testARefusedInterviewKeepsWhatWasTypedAndQuotesTheDeck() async {
        let client = ScriptedDeckClient()
        client.interviewError = .createRefused(.noSuchDirectory, detail: "no such directory")
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        let started = await store.submitPending()

        XCTAssertFalse(started, "a refusal keeps the conversation open")
        XCTAssertEqual(store.pending?.problem,
                       DeckError.createRefused(.noSuchDirectory, detail: "no such directory").userFacingText)
    }

    // MARK: pretrust — the dialog the interview cannot always pre-answer

    /// He is already looking at the new thread the interview just opened, so
    /// that is where a failed pre-trust is said — not left for him to find
    /// later in the settings panel.
    func testAPretrustFailureShowsInTheThreadThatJustOpened() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        client.interviewPretrust = PretrustOutcome(ok: false, reason: "lock_busy")
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()

        XCTAssertEqual(store.pretrustWarning, BlockedReason.lockBusy.sentence)
    }

    func testAnOkPretrustShowsNoWarningAtAll() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        client.interviewPretrust = PretrustOutcome(ok: true, reason: "trusted")
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()

        XCTAssertNil(store.pretrustWarning)
    }

    /// A deck that has not shipped `pretrust` at all must not read as a
    /// failure — additive means silent, not an invented warning.
    func testNoPretrustFieldAtAllShowsNoWarning() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        let store = DeckStore(client: client)

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()

        XCTAssertNil(store.pretrustWarning)
    }

    /// Walking away from the thread must not leave a stale warning glued to
    /// whatever he opens next.
    func testLeavingTheThreadClearsThePretrustWarning() async {
        let client = ScriptedDeckClient()
        client.interviewResult = Agent(
            name: "agent-7f3", title: "Working it out…", detail: "",
            section: "Work", threadID: "direct:agent-7f3"
        )
        client.interviewPretrust = PretrustOutcome(ok: false, reason: "lock_busy")
        client.rosterPayload = roster(
            [Agent(name: "chief", title: "Negotiator", section: "Work",
                   threadID: "direct:chief")],
            [makeThread("direct:chief", agent: "chief")]
        )
        let store = DeckStore(client: client)
        await store.loadRoster()

        store.beginNewAgent()
        store.typePending("watch my open PRs")
        _ = await store.submitPending()
        XCTAssertNotNil(store.pretrustWarning)

        store.select(agent: "chief", threadID: "direct:chief")

        XCTAssertNil(store.pretrustWarning, "the warning belonged to the thread he just left")
    }

    // MARK: the rename, mid-conversation

    /// Builds a store that is already talking to a provisional desk, with one
    /// message on screen.
    private func talkingToProvisionalDesk() async -> (DeckStore, ScriptedDeckClient) {
        let client = ScriptedDeckClient()
        let provisional = Agent(name: "agent-7f3", title: "Working it out…", detail: "",
                                section: "Work", threadID: "direct:agent-7f3")
        client.rosterPayload = roster(
            [provisional],
            [makeThread("direct:agent-7f3", agent: "agent-7f3")]
        )
        client.pages = [MessagePage(threadID: "direct:agent-7f3",
                                    messages: [makeMessage("m1",
                                                           text: "watching your PRs", thread: "direct:agent-7f3")],
                                    isReadOnly: false, participants: ["owner", "agent-7f3"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()
        return (store, client)
    }

    func testTheOpenConversationSurvivesTheAgentRenamingItself() async {
        let (store, _) = await talkingToProvisionalDesk()

        await store.applyRename(
            to: Agent(name: "seeker", title: "PR watcher",
                      detail: "Watches the open PRs and says what changed.",
                      section: "Work", threadID: "direct:seeker"),
            replacing: "agent-7f3"
        )

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the conversation he is reading must not vanish, got \(store.thread)")
        }
        XCTAssertEqual(screen.messages.map(\.id), ["m1"],
                       "same messages, not a reload — a reload is what loses his place")
    }

    func testTheNewNameIsWhatIsDisplayedAfterTheRename() async {
        let (store, _) = await talkingToProvisionalDesk()

        await store.applyRename(
            to: Agent(name: "seeker", title: "PR watcher",
                      detail: "Watches the open PRs and says what changed.",
                      section: "Work", threadID: "direct:seeker"),
            replacing: "agent-7f3"
        )

        XCTAssertEqual(store.selectedAgentName, "seeker", "the selection follows the desk")
        XCTAssertEqual(store.agents["seeker"]?.title, "PR watcher")
        XCTAssertNil(store.agents["agent-7f3"], "the placeholder is gone, not kept alongside")
        XCTAssertEqual(store.selectedThreadID, "direct:seeker",
                       "the thread id is keyed on the name, so it moves with it")
    }

    func testTheSidebarUpdatesTheSameRowRatherThanGrowingASecondOne() async {
        let (store, _) = await talkingToProvisionalDesk()

        await store.applyRename(
            to: Agent(name: "seeker", title: "PR watcher", detail: "",
                      section: "Work", threadID: "direct:seeker"),
            replacing: "agent-7f3"
        )

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected a loaded roster, got \(store.roster)")
        }
        let rows = snapshot.sections.flatMap(\.rows)
        XCTAssertEqual(rows.count, 1, "one desk renamed itself; it is not two desks")
        XCTAssertEqual(rows.map(\.id), ["seeker"])
        XCTAssertEqual(rows.first?.agent.displayName, "Seeker")
    }

    /// A rename for a desk the owner is *not* reading must not steal his pane.
    func testARenameElsewhereDoesNotMoveTheSelection() async {
        let (store, _) = await talkingToProvisionalDesk()

        await store.applyRename(
            to: Agent(name: "scribe", title: "Notes", detail: "",
                      section: "Work", threadID: "direct:scribe"),
            replacing: "someone-else"
        )

        XCTAssertEqual(store.selectedAgentName, "agent-7f3")
        XCTAssertEqual(store.selectedThreadID, "direct:agent-7f3")
    }
}
