import XCTest
@testable import DeckKit
@testable import DeckUI

/// **A desk that names itself must not take the conversation with it.**
///
/// Naming itself is the *normal* end of the interview: "+" hires a placeholder,
/// the placeholder asks what the job is, and when it knows it renames itself.
/// So this is not an edge case — it is what happens on **every** agent the
/// owner creates.
///
/// Measured on his live deck. He pasted his whole context into a new desk and
/// got nothing back; his pane sat on `Idle — Its session is up and nothing is
/// running.` while he typed "hello?" at it. Nothing was wrong on the deck: the
/// agent read everything, finished the interview, named itself and replied.
/// The frame it sent was
///
/// ```json
/// {"type": "agent_renamed", "ts": 1788689947.7,
///  "old_name": "new-hire-77ec17",
///  "agent": {"name": "portfolio-lead", "label": "Portfolio", "state": "OFFLINE",
///            "unread": 1, "last_activity_by": "agent",
///            "preview": "Portfolio, then — real estate video software, …"}}
/// ```
///
/// and the app had no case for it, so it was decoded to `nil` and thrown away.
/// The app was not wedged (0.1-3.1% CPU, 40 requests to the box in a minute)
/// and the stream was healthy; it was simply never told. Everything downstream
/// is keyed on the name — the roster row, the thread id, the selection, the
/// `agent_state` frames — so from that moment the app was watching a desk that
/// no longer existed.
///
/// ## The three failures this file pins, and they are one defect
///
/// 1. **The frame is dropped.** `DeckEvent` had no `agentRenamed`, so
///    `applyRename` had no caller.
/// 2. **The open thread freezes even once it is wired**, because the live
///    pipeline is keyed on the thread id it was opened with and the rename
///    changes it. Both `ConversationSync.belongsHere` and `DeckStore.apply`
///    would start rejecting the very frames the conversation is made of.
/// 3. **The state line goes stale**, because `RosterModel.applyAgentState`
///    is a `guard let position … else { return }` on the name. This file
///    *measures* that rather than assuming it — see
///    `testAStateFrameForANameTheRosterHasNeverHeardOfIsSilentlyDropped`,
///    which is the reason the wrong "Idle" is the same defect and not a
///    second one.
///
/// **Every assertion here is the presence of the good signal** — the
/// conversation is still on screen, under the new name, still taking live
/// frames — never the absence of an error. A thread that failed to load is
/// also a thread with no wrong name in it.
@MainActor
final class RenameTests: XCTestCase {

    private let placeholder = "new-hire-77ec17"
    private let chosen = "portfolio-lead"

    // MARK: the frame itself

    /// The deck's own bytes, verbatim. A hand-built `DeckEvent` would pass
    /// while the app still threw the real frame away.
    func testTheDecksRenameFrameDecodes() throws {
        let wire = """
        {"type": "agent_renamed", "ts": 1788689947.7,
         "old_name": "new-hire-77ec17",
         "agent": {"name": "portfolio-lead", "label": "Portfolio", "state": "OFFLINE",
                   "unread": 1, "last_activity_by": "agent",
                   "preview": "Portfolio, then — real estate video software, shop.initech.example"}}
        """
        let event = DeckEvent(sse: SSEEvent(name: nil, data: wire, id: nil))

        guard case .agentRenamed(let oldName, let agent) = event else {
            return XCTFail("the deck's rename frame decoded to \(String(describing: event)). "
                           + "Every agent he creates sends one of these at the end of its "
                           + "interview, and dropping it detaches the conversation he is in.")
        }
        XCTAssertEqual(oldName, placeholder)
        XCTAssertEqual(agent.name, chosen)
        XCTAssertEqual(agent.title, "Portfolio", "the label is the chip beside the name")
        XCTAssertEqual(
            agent.threadID, "direct:portfolio-lead",
            "the frame carries no thread_id, so it has to be derived from the new "
            + "name — otherwise the pane is re-pointed at an empty string")
    }

    /// An unknown frame type must still be ignored rather than crash the
    /// stream; adding a case must not have made the decoder strict.
    func testAnUnknownFrameIsStillIgnored() {
        XCTAssertNil(DeckEvent(sse: SSEEvent(
            name: nil, data: #"{"type":"something_new"}"#, id: nil)))
    }

    // MARK: THE test — he is reading the thread when it happens

    func testTheOpenConversationSurvivesTheDeskNamingItself() async throws {
        let client = scriptedDeck(feed: [
            .agentRenamed(oldName: placeholder, agent: renamedAgent()),
            .message(makeMessage("m9", cursor: "0009", text: "Portfolio, then.",
                                 thread: "direct:\(chosen)", author: chosen), readOnly: false),
        ])
        let store = try await openedOnThePlaceholder(client)

        // GOOD SIGNAL FIRST: the conversation is still there. Everything below
        // would also be true of a pane that failed to load.
        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the conversation is \(store.thread) after the desk named "
                           + "itself. This is what he saw: he pasted his context, the "
                           + "agent answered, and his pane detached.")
        }
        XCTAssertGreaterThanOrEqual(
            screen.rows.count, 3,
            "the transcript he was reading is gone — a rename must not reload the "
            + "thread, it must carry it")

        // The header is the new desk, not the placeholder id.
        XCTAssertEqual(screen.presentation.headerTitle, "Portfolio-lead")
        XCTAssertEqual(screen.presentation.headerSubtitle, "Portfolio")
        XCTAssertEqual(screen.presentation.threadID, "direct:\(chosen)")

        // And the selection moved with it, so the composer still posts to a
        // thread the deck will accept.
        XCTAssertEqual(store.selectedAgentName, chosen)
        XCTAssertEqual(store.selectedThreadID, "direct:\(chosen)")

        // THE line he never got: a message that arrived under the NEW thread id
        // while the pane was still open under the old one.
        XCTAssertTrue(
            screen.rows.contains { $0.message.id == "m9" },
            "the desk replied under its new name and the reply never reached the "
            + "screen: \(screen.rows.map(\.message.id)). That is the 'I typed "
            + "hello? and nothing happened' he reported.")
    }

    /// **The frame that arrives AFTER the rename has landed.**
    ///
    /// The one above passes even with the old thread-id guard in place, because
    /// the reply and the rename reach the main actor in the same burst and the
    /// reply gets there first. The freeze he would have hit next is the frame
    /// that arrives once the rename is applied and `selectedThreadID` has
    /// already moved — every update from the pipeline he is watching then fails
    /// a guard written against the id it was opened with, and the conversation
    /// stays on screen and stops moving.
    ///
    /// Sequenced with a dropped stream so the two are provably not in the same
    /// burst, which also pins the other half: the catch-up after the drop must
    /// ask the deck for the **new** thread.
    func testAReplyThatArrivesAfterTheRenameHasLandedStillReachesTheScreen() async throws {
        let client = scriptedDeck(feed: [])
        // Dropped, not finished: a clean end returns from the sync loop, and
        // this needs the reconnection that follows a real stream dying.
        client.feeds = [
            .emitThenDrop([.agentRenamed(oldName: placeholder, agent: renamedAgent())]),
            .emitThenFinish([
                .message(makeMessage("m9", cursor: "0009", text: "Portfolio, then.",
                                     thread: "direct:\(chosen)", author: chosen), readOnly: false),
            ]),
        ]
        // The reconnect re-reads the thread; without a second page the catch-up
        // throws and the drop looks like a failure instead of a reconnection.
        client.pages.append(MessagePage(
            threadID: "direct:\(chosen)", messages: [], isReadOnly: false,
            participants: [DeckOwner.name, chosen]))

        // The reconnect delay is where the main actor gets to run. Without it
        // both streams drain in one batch, the rename is applied last, and the
        // ordering that freezes the pane never happens in the harness.
        let store = DeckStore(
            client: client,
            backoff: { _ in for _ in 0..<200 { await Task.yield() } },
            approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: placeholder, threadID: "direct:\(placeholder)")
        await store.settle()

        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the conversation is \(store.thread)")
        }
        XCTAssertTrue(
            screen.rows.contains { $0.message.id == "m9" },
            "the desk spoke after it had renamed itself and the line never reached "
            + "the screen: \(screen.rows.map(\.message.id)). The pane is still on "
            + "screen and has stopped moving — which is exactly what he described.")

        // And the deck was asked for the new thread, not the placeholder's.
        XCTAssertEqual(
            client.calls.last(where: { if case .messages = $0 { return true } else { return false } }),
            .messages(threadID: "direct:\(chosen)", since: "0003"),
            "the catch-up after the drop went to \(client.calls). The old id still "
            + "answers today, but the desk is called something else now and this "
            + "client should be asking for it by that name.")
    }

    /// The composer must still work afterwards, or he can read the reply and
    /// not answer it.
    func testHeCanStillSendAfterTheDeskRenamesItself() async throws {
        let client = scriptedDeck(feed: [
            .agentRenamed(oldName: placeholder, agent: renamedAgent()),
        ])
        let store = try await openedOnThePlaceholder(client)
        client.sendResult = makeMessage("m10", cursor: "0010", text: "hello?",
                                        thread: "direct:\(chosen)", author: DeckOwner.name,
                                        role: .owner)

        store.composerDraft = "hello?"
        await store.submitComposer()

        XCTAssertTrue(
            client.calls.contains(.send(threadID: "direct:\(chosen)")),
            "his line went to \(client.calls) — the composer is still posting to "
            + "the placeholder's thread, or to nothing at all")
    }

    // MARK: the sidebar

    func testTheSidebarShowsOneDeskUnderTheNewNameAndNoStrangerBesideIt() async throws {
        let client = scriptedDeck(feed: [
            .agentRenamed(oldName: placeholder, agent: renamedAgent()),
        ])
        let store = try await openedOnThePlaceholder(client)

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("the sidebar is \(store.roster) — no roster to check")
        }
        let rows = snapshot.sections.flatMap(\.rows)
        XCTAssertEqual(
            rows.map(\.id), [chosen],
            "one desk renamed itself; the sidebar shows \(rows.map(\.id)). The "
            + "placeholder must not linger and the new name must not arrive as a "
            + "second row.")

        // He is reading this very conversation, so it cannot arrive with an
        // unread badge on it — the frame carries `unread: 1` because the desk
        // spoke, and he is looking at what it said.
        XCTAssertEqual(
            rows.first?.unreadCount, 0,
            "the desk he is currently reading is badged unread. The rename frame "
            + "carries the count from before he was watching.")
    }

    /// The same frame while he is reading **someone else** must move the row
    /// and leave his selection alone.
    func testARenameOfAnotherDeskDoesNotStealTheOpenConversation() async throws {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief", title: "Chief of staff")
        chief.state = .working
        client.rosterPayload = RosterPayload(
            agents: [chief, makeAgent(placeholder, title: "New hire")],
            threads: [makeThread("direct:chief", agent: "chief"),
                      makeThread("direct:\(placeholder)", agent: placeholder)],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:chief",
                                    messages: [makeMessage("c1", cursor: "0001", thread: "direct:chief")],
                                    isReadOnly: false, participants: [DeckOwner.name, "chief"])]
        client.feeds = [.emitThenFinish([.agentRenamed(oldName: placeholder, agent: renamedAgent())])]

        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()

        XCTAssertEqual(store.selectedAgentName, "chief",
                       "another desk renamed itself and the app changed what he was reading")
        XCTAssertEqual(store.selectedThreadID, "direct:chief")
        guard case .loaded(let screen) = store.thread else {
            return XCTFail("chief's conversation is \(store.thread)")
        }
        XCTAssertEqual(screen.presentation.headerTitle, "Chief")

        guard case .loaded(let snapshot) = store.roster else { return XCTFail("no roster") }
        let names = snapshot.sections.flatMap(\.rows).map(\.id).sorted()
        XCTAssertEqual(names, ["chief", chosen],
                       "the renamed desk did not move in the sidebar: \(names)")
    }

    /// A rename for a desk this client has never seen must be inert — not a
    /// phantom row, not a crash. The deck fans one stream to every window.
    func testARenameOfADeskThisWindowHasNeverSeenChangesNothing() async throws {
        let client = scriptedDeck(feed: [
            .agentRenamed(oldName: "somebody-elses-desk", agent: makeAgent("stranger")),
        ])
        let store = try await openedOnThePlaceholder(client)

        guard case .loaded(let snapshot) = store.roster else { return XCTFail("no roster") }
        XCTAssertEqual(
            snapshot.sections.flatMap(\.rows).map(\.id), [placeholder],
            "a rename for a desk that is not on this roster invented a row")
        XCTAssertEqual(store.selectedAgentName, placeholder,
                       "a stranger's rename moved his selection")
    }

    // MARK: the state line — measured, not assumed

    /// **Why the wrong "Idle" is the same defect.**
    ///
    /// The deck said `WORKING` and his pane said `Idle`. The mechanism is one
    /// line: `RosterModel.applyAgentState` is a `guard let position … else
    /// { return }` on the name, so once the roster is still keyed on
    /// `new-hire-77ec17`, every state frame for `portfolio-lead` is dropped on
    /// the floor. This measures that directly, with no rename involved.
    func testAStateFrameForANameTheRosterHasNeverHeardOfIsSilentlyDropped() async {
        let model = RosterModel(client: ScriptedDeckClient())
        await model.replace(with: RosterPayload(
            agents: [makeAgent(placeholder)],
            threads: [makeThread("direct:\(placeholder)", agent: placeholder)],
            sectionOrder: ["Work"]))

        await model.applyAgentState(name: chosen, state: .working)
        let agents = await model.agentsByName()

        XCTAssertNil(agents[chosen], "the state frame invented a desk")
        XCTAssertEqual(
            agents[placeholder]?.state, .idle,
            "this is the measurement: a WORKING frame under the new name leaves "
            + "the old row exactly as it was. So the wrong 'Idle' is not a second "
            + "bug — it is what a dropped rename looks like one frame later.")
    }

    /// And with the rename applied, the same frame lands.
    func testTheStateLineFollowsTheDeskThroughTheRename() async throws {
        let client = scriptedDeck(feed: [
            .agentRenamed(oldName: placeholder, agent: renamedAgent()),
            .agentState(name: chosen, state: .working, blocked: .unspecified),
        ])
        let store = try await openedOnThePlaceholder(client)

        guard let status = store.deskStatus else {
            return XCTFail("the conversation says nothing at all about what the desk "
                           + "is doing, which is the strip he asked for")
        }
        XCTAssertEqual(
            store.agents[chosen]?.state, .working,
            "the deck said WORKING under the new name and the roster did not take it")
        XCTAssertTrue(
            status.headline.localizedCaseInsensitiveContains("working"),
            "the strip says \"\(status.headline)\" while the deck says WORKING. He "
            + "watched his agent read his whole context and the pane told him it "
            + "was idle.")
    }

    // MARK: harness

    private func renamedAgent() -> Agent {
        var agent = makeAgent(chosen, title: "Portfolio")
        agent.state = .offline
        agent.unread = 1
        agent.preview = "Portfolio, then — real estate video software"
        return agent
    }

    private func scriptedDeck(feed: [DeckEvent]) -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent(placeholder, title: "New hire")],
            threads: [makeThread("direct:\(placeholder)", agent: placeholder, unread: 1)],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(
            threadID: "direct:\(placeholder)",
            messages: (1...3).map {
                makeMessage("m\($0)", cursor: String(format: "%04d", $0),
                            text: "line \($0) of the context he pasted",
                            thread: "direct:\(placeholder)", author: placeholder)
            },
            isReadOnly: false,
            participants: [DeckOwner.name, placeholder])]
        client.feeds = [.emitThenFinish(feed)]
        return client
    }

    private func openedOnThePlaceholder(_ client: ScriptedDeckClient) async throws -> DeckStore {
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: placeholder, threadID: "direct:\(placeholder)")
        await store.settle()
        return store
    }
}
