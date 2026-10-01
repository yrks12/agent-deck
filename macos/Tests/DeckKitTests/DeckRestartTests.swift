import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The deck can be restarted underneath this app, and it is — on purpose,
/// several times a day.**
///
/// Measured on his machine while this branch was being written: `agentdeck`
/// was restarted to deploy a fix, about four seconds of downtime, with the app
/// open and his finger on an approval. The app emptied its entire sidebar,
/// drew a blank conversation, and then quit. Nothing on screen said the deck
/// had gone; nothing brought it back. The reference product draws
/// *"Connecting"* in the same situation and recovers on its own.
///
/// The shape of the fault is in `ConversationSync.run()`: the catch-up fetch at
/// the top of the loop is a bare `try`, outside the `do`. So a dropped
/// **stream** is retried — that path works — but a drop that also refuses the
/// **fetch**, which is exactly what a restarting daemon does, throws straight
/// out of the sync. `DeckStore` caught that and replaced a conversation he was
/// reading with a failure screen, and a roster refresh landing in the same
/// window replaced his desks with one too.
///
/// So this is asserted as a good signal and never as an absence: after a real
/// transport failure **and a recovery**, the roster is still his roster, the
/// open conversation is still the conversation, they are current again, and
/// the app said *"Reconnecting"* the whole way through.
@MainActor
final class DeckRestartTests: XCTestCase {

    // MARK: the deck he actually runs, with a power switch on it

    /// A deck that can be taken down and put back the way `systemctl restart
    /// agentdeck` does it: the open stream dies, **every** route refuses for as
    /// long as it is down, and then everything answers again from the same
    /// state. The refusal is `DeckError.transport`, which is what a refused
    /// socket decodes to in `HTTPDeckClient`.
    final class RestartableDeck: DeckClient, @unchecked Sendable {
        private let lock = NSLock()
        private var down = false
        private var live: AsyncThrowingStream<DeckEvent, Error>.Continuation?
        private var transcript: [Message]
        private let payload: RosterPayload
        private var _opens = 0

        /// The socket, refused. Named for what he sees in the log.
        static let refused = DeckError.transport("Could not connect to the server.")

        init(agents: [Agent], messages: [Message]) {
            payload = AgentsResponse.roster(from: agents)
            transcript = messages
        }

        /// How many times the app has opened `/v1/stream`. One on launch, one
        /// more for every recovery — a deck that never came back would leave
        /// this at 1.
        var streamOpens: Int { lock.withLock { _opens } }
        var isDown: Bool { lock.withLock { down } }

        func goDown() {
            lock.lock()
            down = true
            let open = live
            live = nil
            lock.unlock()
            open?.finish(throwing: Self.refused)
        }

        func comeBack() { lock.withLock { down = false } }

        /// Something the deck learned while the app could not hear it. It has
        /// to be on screen once the app is back, or "recovered" means the app
        /// stopped complaining rather than that it caught up.
        func saidWhileNobodyWasListening(_ message: Message) {
            lock.withLock { transcript.append(message) }
        }

        private func refuseIfDown() throws {
            if lock.withLock({ down }) { throw Self.refused }
        }

        func roster() async throws -> RosterPayload {
            try refuseIfDown()
            return payload
        }

        func threads() async throws -> [ThreadSummary] {
            try refuseIfDown()
            return payload.threads
        }

        func agent(named name: String) async throws -> Agent {
            try refuseIfDown()
            guard let found = payload.agents.first(where: { $0.name == name }) else {
                throw DeckError.unknownAgent
            }
            return found
        }

        func updateAgent(_ agent: Agent) async throws -> Agent {
            try refuseIfDown()
            return agent
        }

        func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
            try refuseIfDown()
            return MessagePage(
                threadID: threadID,
                messages: lock.withLock { transcript },
                isReadOnly: false,
                participants: ["owner", ThreadID.desk(ofDirect: threadID) ?? ""])
        }

        func send(threadID: String, text: String) async throws -> Message {
            try refuseIfDown()
            throw DeckError.unknownThread
        }

        func markRead(agent: String, upTo: String?) async throws { try refuseIfDown() }

        func createAgent(_ draft: AgentDraft) async throws -> Agent {
            try refuseIfDown()
            throw DeckError.unknownAgent
        }

        func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
            try refuseIfDown()
            throw DeckError.unknownAgent
        }

        func approvals() async throws -> ApprovalsPage {
            try refuseIfDown()
            return ApprovalsPage(approvals: [], unreadable: 0)
        }

        func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
            try refuseIfDown()
            throw DeckError.unknownAsk
        }

        /// Wired rather than inherited, and it is the **refusal** that matters
        /// here: `DeckClient`'s default answers an empty page without asking
        /// anything, so a double on it would report "nothing is stuck" while
        /// the deck was down — which is the one moment this file is about.
        func handoffs() async throws -> HandoffsPage {
            try refuseIfDown()
            return HandoffsPage(handoffs: [], unreadable: 0)
        }

        @discardableResult
        func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
            try refuseIfDown()
            throw DeckError.unknownAsk
        }

        func routines() async throws -> [Routine] {
            try refuseIfDown()
            return []
        }

        func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
            try refuseIfDown()
            throw DeckError.unknownRoutine
        }

        func setRoutine(id: String, enabled: Bool) async throws -> Routine {
            try refuseIfDown()
            throw DeckError.unknownRoutine
        }

        func deleteRoutine(id: String) async throws { try refuseIfDown() }

        func events() -> AsyncThrowingStream<DeckEvent, Error> {
            AsyncThrowingStream { continuation in
                lock.lock()
                _opens += 1
                let isDown = down
                if !isDown { live = continuation }
                lock.unlock()
                if isDown {
                    continuation.finish(throwing: Self.refused)
                } else {
                    continuation.yield(.heartbeat)
                }
            }
        }
    }

    // MARK: the case

    private func openDeck() async -> (RestartableDeck, DeckStore) {
        let deck = RestartableDeck(
            agents: [makeAgent("atlas"), makeAgent("northwind-growth")],
            messages: [makeMessage("m1", text: "on it", thread: "direct:atlas", author: "atlas")])
        let store = DeckStore(
            client: deck,
            // The real one caps at 30 seconds; a test must not wait for it, and
            // the retry cadence is not what is under test here.
            backoff: { _ in try await Task.sleep(nanoseconds: 2_000_000) },
            approvalPollInterval: 600)
        await store.loadRoster()
        await waitFor("the app to be live on the deck") { store.connection == .live }
        return (deck, store)
    }

    /// **The whole defect, and the whole fix, in one run.** Four seconds of
    /// downtime, mid-conversation.
    func testARestartOfTheDeckKeepsTheRosterAndTheConversationAndSaysReconnecting() async throws {
        let (deck, store) = await openDeck()

        // What is on screen when the deck goes. This is the thing that must
        // survive: it is the only copy the app has.
        let desksBefore = desks(in: store)
        XCTAssertEqual(desksBefore, ["atlas", "northwind-growth"], "the case needs a loaded sidebar")
        XCTAssertEqual(rows(in: store), ["on it"], "and an open conversation")

        // — the deploy —
        deck.goDown()
        await waitFor("the app to notice the deck has gone") {
            if case .reconnecting = store.connection { return true }
            return false
        }
        // A background roster refresh landing inside the outage, which is what
        // really happens: the window is open all day and something asks.
        await store.loadRoster()

        XCTAssertEqual(
            store.connection.label, "Reconnecting",
            "the deck is down and the app is saying \"\(store.connection.label)\". "
            + "Silence here is how a stale transcript reads as a current one.")
        XCTAssertEqual(
            desks(in: store), desksBefore,
            "his entire sidebar emptied because one refresh could not reach the "
            + "deck. The desks did not go anywhere — the socket did.")
        XCTAssertEqual(
            rows(in: store), ["on it"],
            "the conversation he was reading was replaced by a failure screen. "
            + "Those messages already arrived; a deck restart does not un-arrive them.")
        XCTAssertEqual(
            store.thread.loadedScreen?.connection.label, "Reconnecting",
            "and the conversation itself has to say it, not just the toolbar — "
            + "he is looking at the transcript, not at the chrome.")

        // — the daemon comes back, and it has news —
        deck.saidWhileNobodyWasListening(
            makeMessage("m2", text: "back up", thread: "direct:atlas", author: "atlas"))
        deck.comeBack()

        await waitFor("the app to come back on its own") { store.connection == .live }
        XCTAssertEqual(
            rows(in: store), ["on it", "back up"],
            "the app reconnected and did not catch up, so the conversation is "
            + "frozen at the moment the deck restarted while claiming to be live")
        XCTAssertEqual(desks(in: store), desksBefore, "and the sidebar is still his roster")
        XCTAssertGreaterThan(
            deck.streamOpens, 1,
            "nothing ever reopened the stream, so this only looks recovered")
    }

    /// **The sweep, so the fix is not "swallow every error".** A deck that was
    /// never reachable has nothing to keep on screen, and must still say what
    /// is wrong and offer the one thing that could change it.
    func testADeckThatWasNeverReachableStillSaysSoAndOffersTheRetry() async throws {
        let deck = RestartableDeck(agents: [makeAgent("atlas")], messages: [])
        deck.goDown()
        let store = DeckStore(
            client: deck,
            backoff: { _ in try await Task.sleep(nanoseconds: 2_000_000) },
            approvalPollInterval: 600)

        await store.loadRoster()

        guard case .failed(let error) = store.roster else {
            return XCTFail("a deck that has never answered is not a deck to keep quiet about: "
                           + "\(store.roster)")
        }
        let says = FailurePresentation.make(error)
        XCTAssertEqual(says.heading, "Can't reach the deck")
        XCTAssertEqual(says.action, .retry(title: "Try again"),
                       "there is nothing on screen and no way to ask again")
    }

    /// A refusal is not a drop. The deck answered — it said no — so keeping the
    /// old screen up and calling it "Reconnecting" would hide a token the user
    /// has to go and replace.
    func testARejectedTokenIsNotDrawnAsAConnectionThatIsComingBack() async throws {
        let client = ScriptedDeckClient()
        client.rosterError = .unauthorized
        let store = DeckStore(client: client, approvalPollInterval: 600)

        await store.loadRoster()

        guard case .failed(.unauthorized) = store.roster else {
            return XCTFail("a refused token has to reach the screen: \(store.roster)")
        }
        XCTAssertNotEqual(store.connection.label, "Reconnecting",
                          "nothing is coming back on its own; he has to type a new token")
    }

    // MARK: probes

    private func desks(in store: DeckStore) -> [String] {
        guard case .loaded(let snapshot) = store.roster else { return [] }
        return snapshot.sections.flatMap(\.rows).map(\.agent.name).sorted()
    }

    private func rows(in store: DeckStore) -> [String] {
        store.thread.loadedScreen?.rows.map(\.message.text) ?? []
    }

    private func waitFor(
        _ what: String,
        within timeout: TimeInterval = 5,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for \(what)", file: file, line: line)
    }
}

extension LoadState where Value == ThreadScreen {
    /// The conversation, if one is drawn. `nil` for every other state, so a
    /// test cannot read a screen off a pane that is showing a spinner.
    var loadedScreen: ThreadScreen? {
        if case .loaded(let screen) = self { return screen }
        return nil
    }
}
