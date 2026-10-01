import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"no fedbeck whethere somthing going."**
///
/// He sends a message and the conversation is inert. Nothing on that screen
/// says the desk took it, is thinking, has stopped to ask him something, or has
/// gone quiet. The deck knows all of it — a row carries `state` and `blocked`,
/// `/v1/approvals` says when a session is stopped waiting for a human, and the
/// stream says whether this app is attached at all. None of it reached the pane
/// he was looking at.
///
/// So this sweeps the **class**, not a spinner: every condition he has to be
/// able to tell apart — working, waiting on him, done, idle, offline, and the
/// app not being connected — has to arrive as a different sentence, on the
/// thread, without him navigating anywhere.
final class DeskStatusTests: XCTestCase {

    private func agent(_ state: AgentState, blocked: Blocked? = nil) -> Agent {
        var desk = makeAgent("chief")
        desk.state = state
        desk.blocked = blocked
        return desk
    }

    private func status(
        _ state: AgentState,
        blocked: Blocked? = nil,
        connection: ConnectionState = .live,
        approvals: [ApprovalCard] = [],
        isSending: Bool = false
    ) -> DeskStatus? {
        DeskStatus.make(
            agent: agent(state, blocked: blocked),
            connection: connection,
            approvals: approvals,
            isSending: isSending
        )
    }

    // MARK: 1. the three he named — working, stuck, done

    func testAWorkingDeskSaysSoOnTheThread() throws {
        let shown = try XCTUnwrap(status(.working))

        XCTAssertEqual(shown.tone, .working)
        XCTAssertTrue(
            shown.headline.localizedCaseInsensitiveContains("working"),
            "a desk that is working must say it in a word, got \(shown.headline)")
    }

    func testADeskThatHasFinishedIsNotDrawnTheSameAsOneStillThinking() throws {
        let working = try XCTUnwrap(status(.working))
        let done = try XCTUnwrap(status(.done))

        XCTAssertNotEqual(working.headline, done.headline)
        XCTAssertEqual(done.tone, .quiet)
        XCTAssertNotNil(done.detail, "\"done\" is only useful if it says the desk has gone quiet")
    }

    func testADeskStoppedOnADialogSaysWhatToDoAboutIt() throws {
        let stuck = Blocked(what: "waiting", reason: "dialog_unrelayed", detail: "")
        let shown = try XCTUnwrap(status(.working, blocked: stuck))

        XCTAssertEqual(shown.tone, .waitingOnYou,
                       "a frozen desk is not 'working' however its state field reads")
        XCTAssertEqual(shown.detail, BlockedReason.dialogUnrelayed.sentence,
                       "the same sentence Blocked already writes, not a second wording of it")
    }

    // MARK: 2. stopped waiting for HIM, from the two places the deck says it

    func testADeskAskingForHimSaysItIsWaitingOnHim() throws {
        let shown = try XCTUnwrap(status(.needsYou))

        XCTAssertEqual(shown.tone, .waitingOnYou)
        XCTAssertTrue(shown.headline.localizedCaseInsensitiveContains("you"),
                      "it has to name him, got \(shown.headline)")
    }

    func testAPendingApprovalIsSaidOnTheThreadAndNamesTheAsk() throws {
        let card = ApprovalCard(
            approvalID: "a1", title: "gh pr create", tool: "Bash", at: nil,
            runsOn: "Runs on Chief's computer",
            why: "", disclosureTitle: "Show the details", details: "", options: [],
            status: .waitingOnYou
        )
        // `state` is deliberately the least alarming one there is: a live
        // question outranks a quiet row, because the row is what he already
        // could not tell apart from "nothing is happening".
        let shown = try XCTUnwrap(status(.idle, approvals: [card]))

        XCTAssertEqual(shown.tone, .waitingOnYou)
        XCTAssertEqual(shown.detail, "gh pr create",
                       "the card is on screen underneath; the strip has to name the same ask")
    }

    func testTwoPendingApprovalsAreCountedRatherThanQuotedOneAtATime() throws {
        let cards = (1...2).map {
            ApprovalCard(approvalID: "a\($0)", title: "ask \($0)", tool: "Bash", at: nil,
                         runsOn: "", why: "",
                         disclosureTitle: "", details: "", options: [], status: .waitingOnYou)
        }
        let shown = try XCTUnwrap(status(.idle, approvals: cards))

        XCTAssertEqual(shown.tone, .waitingOnYou)
        XCTAssertEqual(shown.detail, "2 requests are waiting for an answer.")
    }

    // MARK: 3. the desk is not there at all

    func testAnOfflineDeskReadsAsOfflineAndNotAsIdle() throws {
        let offline = try XCTUnwrap(status(.offline))
        let idle = try XCTUnwrap(status(.idle))

        XCTAssertEqual(offline.tone, .offline)
        XCTAssertEqual(idle.tone, .quiet)
        XCTAssertNotEqual(offline.headline, idle.headline,
                          "\"no session at this desk\" and \"a session sitting idle\" are "
                          + "different things to do something about")
    }

    // MARK: 4. the app not being attached outranks everything the desk claims

    func testADroppedStreamIsSaidOnTheThreadAndOutranksAStaleWorkingState() throws {
        let shown = try XCTUnwrap(
            status(.working, connection: .reconnecting(attempt: 2))
        )

        XCTAssertEqual(shown.tone, .disconnected,
                       "the last state the deck sent is not news once the stream is gone — "
                       + "showing it as current is the lie that made him think nothing worked")
        XCTAssertNotNil(shown.detail)
    }

    func testAnIdleConnectionSaysNothingHereIsLive() throws {
        let shown = try XCTUnwrap(status(.working, connection: .idle))

        XCTAssertEqual(shown.tone, .disconnected)
    }

    func testAThreadWithNoDeskBehindItClaimsNothingWhileTheStreamIsHealthy() {
        XCTAssertNil(
            DeskStatus.make(agent: nil, connection: .live, approvals: [], isSending: false),
            "a peer transcript has no desk of its own; inventing a state for it "
            + "would be putting a sentence on screen nothing had said")
    }

    func testAThreadWithNoDeskStillSaysTheStreamIsDown() throws {
        let shown = try XCTUnwrap(
            DeskStatus.make(agent: nil, connection: .reconnecting(attempt: 1),
                            approvals: [], isSending: false)
        )

        XCTAssertEqual(shown.tone, .disconnected)
    }

    // MARK: 5. the moment he presses Return

    func testThePressOfReturnIsAcknowledgedBeforeAnythingComesBack() throws {
        let shown = try XCTUnwrap(status(.idle, isSending: true))

        XCTAssertEqual(shown.tone, .working)
        XCTAssertTrue(shown.headline.localizedCaseInsensitiveContains("send"),
                      "the gap between pressing Return and the desk waking up is "
                      + "exactly where he decided the app was broken, got \(shown.headline)")
    }

    // MARK: 6. the sweep — no two conditions may read the same

    func testEveryStateADeskCanBeInGetsItsOwnSentence() {
        var headlines: [String: String] = [:]
        for state in AgentState.allCases {
            let shown = DeskStatus.make(
                agent: agent(state), connection: .live, approvals: [], isSending: false
            )
            let headline = try? XCTUnwrap(shown?.headline)
            XCTAssertNotNil(headline, "\(state.rawValue) says nothing at all")
            guard let headline else { continue }
            if let clash = headlines[headline] {
                XCTFail("\(state.rawValue) and \(clash) both read \"\(headline)\" — "
                        + "he cannot tell them apart")
            }
            headlines[headline] = state.rawValue
        }
        XCTAssertEqual(headlines.count, AgentState.allCases.count)
    }

    /// VoiceOver gets one sentence, not a headline and an orphaned fragment —
    /// the same rule `Message.spokenLabel` and `FailureView` already follow.
    func testTheStatusIsSpokenAsOneSentence() throws {
        let shown = try XCTUnwrap(status(.offline))

        XCTAssertTrue(shown.spokenLabel.hasPrefix(shown.headline))
        XCTAssertTrue(shown.spokenLabel.contains(try XCTUnwrap(shown.detail)))
    }
}

/// The same thing again, but driven the way he drives it: off the live stream,
/// into the store the open conversation is reading.
@MainActor
final class LiveDeskStatusTests: XCTestCase {

    private func store(startingIn state: AgentState) -> (DeckStore, ScriptedDeckClient) {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = state
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        return (DeckStore(client: client), client)
    }

    /// THE detector for "no sign anything is happening": a state change the
    /// deck pushes has to move the sentence on the thread he is already
    /// looking at. No refetch, no reopening, no clicking away and back.
    func testTheOpenThreadStartsSayingWorkingOffTheLiveFrameAlone() async throws {
        let (store, client) = store(startingIn: .idle)
        client.feeds = [.emitThenFinish([
            .agentState(name: "chief", state: .working, blocked: .unspecified)
        ])]

        await store.loadRoster()
        await store.settle()

        XCTAssertEqual(
            store.deskStatus?.tone, .working,
            "the deck said the desk started working and the conversation he is "
            + "watching still reads \(String(describing: store.deskStatus?.headline))")
    }

    /// The retraction half. A desk that finishes has to stop claiming to work,
    /// or "working" becomes a light that never goes out.
    func testTheThreadStopsSayingWorkingWhenTheDeskGoesQuiet() async throws {
        let (store, client) = store(startingIn: .working)
        client.feeds = [.emitThenFinish([
            .agentState(name: "chief", state: .done, blocked: .unspecified)
        ])]

        await store.loadRoster()
        await store.settle()

        XCTAssertEqual(store.deskStatus?.tone, .quiet)
    }

    /// A freeze arrives as `blocked` on the same frame. It has to reach the
    /// conversation, not only the sidebar row — the pane he is looking at is
    /// where he is waiting for an answer that is never coming.
    func testAFreezeReachesTheConversationAndNotOnlyTheSidebar() async throws {
        let (store, client) = store(startingIn: .working)
        let stuck = Blocked(what: "waiting", reason: "dialog_unrelayed", detail: "")
        client.feeds = [.emitThenFinish([
            .agentState(name: "chief", state: .working, blocked: .value(stuck))
        ])]

        await store.loadRoster()
        await store.settle()

        XCTAssertEqual(store.deskStatus?.tone, .waitingOnYou)
        XCTAssertEqual(store.deskStatus?.detail, BlockedReason.dialogUnrelayed.sentence)
    }
}
