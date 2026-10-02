import Combine
import XCTest
@testable import DeckKit
@testable import DeckUI

/// **He must never have to leave the conversation to find out what happened.**
///
/// His words, 2026-09-06, with the app open against the Linux deck:
/// *"why do i need to reopen the chat to see new massages"* and
/// *"why i dont see anything … to understand work is under the hood?"*
///
/// `LiveStreamTests` already proves the bytes reach `DeckEvent` — it drives the
/// real `URLSessionSSETransport` through a real `URLSession`. What no test has
/// ever driven is the layer above it: **a store with a thread open, a feed that
/// stays open, and something happening while nobody navigates.**
///
/// That distinction is the whole point of this file. `ScriptedDeckClient`
/// cannot express it: its feed yields everything it was handed and then
/// finishes, so `ConversationSync.run()` returns and the thread is never *live*
/// while the assertion runs. Every existing store test therefore measures the
/// state of a conversation that has already stopped. `LiveDeckClient` below
/// holds the stream open the way the deck does, so these tests measure the
/// screen he is actually looking at.
///
/// Both assertions here are the **good signal**: the reply is on screen, and
/// the tool call is on screen — not "no error was raised".
@MainActor
final class LiveWhileWatchingTests: XCTestCase {

    // MARK: DETECTOR — a reply must land in the open thread off the stream

    /// The deck answered him at 23:13:32 and again at 23:15:26 and he saw
    /// neither until he clicked away and back.
    ///
    /// The good signal is the reply's text in the transcript. The second
    /// assertion is what makes it a *live* test rather than a refetch test: the
    /// history route was called **once**, when the thread opened, so the line on
    /// screen can only have come off `/v1/stream`.
    func testAReplyArrivesInTheOpenConversationWithNobodyNavigating() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let store = DeckStore(client: client)

        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript == ["any updates?"] }
        let fetchesBefore = client.historyFetches

        client.push(.message(
            LiveDeckClient.reply("m2", "Checking Globex opens and Acme ads.", at: 20),
            readOnly: false
        ))
        await waitUntil("the reply is drawn") { store.transcript.count == 2 }

        XCTAssertEqual(
            store.transcript, ["any updates?", "Checking Globex opens and Acme ads."],
            "the desk answered on the open stream and the conversation he was "
            + "looking at did not move — that is the thread he has to reopen")
        XCTAssertEqual(
            client.historyFetches, fetchesBefore,
            "the reply only appeared because the transcript refetched. It has to "
            + "arrive off the stream; a refetch is what stops happening the "
            + "moment he stops clicking")
    }

    // MARK: DETECTOR — a tool call raised while he watches must appear

    /// `/v1/stream` carries **no approval frame**. §8 lists every type it
    /// sends — `hello`, `message`, `agent_state`, `unread`, `agent_renamed`,
    /// `heartbeat` — and an ask is not one of them. So the only way a tool call
    /// can reach the screen is a poll, and this app had none: `ThreadView`
    /// fetched `/v1/approvals` from `.task(id: store.selectedAgentName)`, which
    /// runs once per *selection*. A question raised one second after he opened
    /// the chat could not appear until he clicked another desk and came back.
    ///
    /// That is both halves of his complaint in one defect. The work under the
    /// hood **is** the tool call, and reopening the chat is what it took.
    func testAToolCallRaisedWhileHeIsWatchingAppearsWithoutReopeningTheChat() async throws {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let store = DeckStore(client: client, approvalPollInterval: 0.02)

        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript == ["any updates?"] }
        XCTAssertTrue(store.approvals.isEmpty, "nothing is waiting yet")

        // The desk hits something its rules do not cover, and stops.
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")

        await waitUntil("the card appears") { !store.approvals.isEmpty }
        XCTAssertEqual(
            store.approvals.map(\.title), ["gh pr create --title 'ship the routes'"],
            "chief stopped on a tool call and the conversation said nothing. The "
            + "ask was on /v1/approvals the whole time and this window only asked "
            + "for it when he changed desks")
        XCTAssertEqual(
            store.deskStatus?.headline, "Waiting on you",
            "the strip above the composer still read Idle while a desk was "
            + "stopped waiting for him")
    }

    /// The other direction, and the one a poll is *for*: a question answered in
    /// the agent's own terminal, or swept after four hours (§12.1), leaves the
    /// list — and the card has to go with it. A card still offering to grant
    /// something nobody is asking for any more is worse than no card.
    func testAQuestionAnsweredSomewhereElseStopsBeingDrawn() async throws {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")
        let store = DeckStore(client: client, approvalPollInterval: 0.02)

        await store.loadRoster()
        await waitUntil("the card appears") { !store.approvals.isEmpty }

        client.approvalsPage = ApprovalsPage(approvals: [])

        await waitUntil("the card goes") { store.approvals.isEmpty }
        XCTAssertEqual(
            store.deskStatus?.headline, "Idle",
            "the desk went back to work and the pane still claimed it was "
            + "waiting on him")
    }

    /// **The 99% CPU rule, applied to the poll.**
    ///
    /// A repeating request that republishes an identical answer tells SwiftUI
    /// to rebuild the whole thread pane on every tick, and this pane sizes a
    /// `LazyVStack` — every rebuild re-measures every row. This app has already
    /// cost the owner his Mac once for exactly that shape of mistake, which is
    /// what `LayoutSettlesTests` exists for. A poll that finds nothing new must
    /// be invisible above the store.
    func testAPollThatFindsNothingNewPublishesNothingAtAll() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let store = DeckStore(client: client, approvalPollInterval: 0.02)

        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript == ["any updates?"] }

        var rebuilds = 0
        let watching = store.objectWillChange.sink { _ in rebuilds += 1 }
        let pollsBefore = client.approvalFetches
        await waitUntil("several polls go by") { client.approvalFetches >= pollsBefore + 4 }
        watching.cancel()

        XCTAssertGreaterThanOrEqual(
            client.approvalFetches, pollsBefore + 4, "the poll never ran at all")
        XCTAssertEqual(
            rebuilds, 0,
            "\(rebuilds) rebuilds of the thread pane for "
            + "\(client.approvalFetches - pollsBefore) polls that found nothing new. "
            + "Every one of those re-measures every row of the transcript.")
    }

    /// **Closing the conversation must NOT stop it**, and this test used to say
    /// the opposite.
    ///
    /// The concern it was written for is real — a request per interval, for
    /// ever, on a window he leaves open all day — but the bound it chose was
    /// the wrong one. What this poll feeds is the **attention tray**, which is
    /// deck-wide: the ask raised on a desk he is not reading, including when he
    /// is reading none. Tying it to the selection meant that the moment he
    /// closed a chat, resolved items stopped leaving the tray and new asks
    /// stopped arriving — the frozen-tray defect.
    ///
    /// The bound now belongs to the window, which is where it was always
    /// meaningful: `TheTrayKeepsWatchingTests` pins both halves, this one keeps
    /// the close case honest from here.
    func testThePollKeepsWatchingTheDeckAfterTheConversationIsClosed() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let store = DeckStore(client: client, approvalPollInterval: 0.02)

        await store.loadRoster()
        await waitUntil("the poll starts") { client.approvalFetches > 0 }
        store.closeThread()

        let atClose = client.approvalFetches
        await waitUntil("the deck is still being watched", within: 2) {
            client.approvalFetches >= atClose + 3
        }
        XCTAssertGreaterThanOrEqual(
            client.approvalFetches, atClose + 3,
            "he closed the chat and this window stopped asking the deck what is "
            + "waiting on him. Nothing else feeds the tray, so a question raised "
            + "from that moment on reaches him nowhere at all.")
    }

    // MARK: waiting without sleeping on a guess

    private func waitUntil(
        _ what: String,
        within timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for: \(what)", file: file, line: line)
    }
}

// MARK: - a deck whose stream stays open

/// A `DeckClient` whose `/v1/stream` **stays open**, so a test can make
/// something happen while the thread is being watched rather than only before.
///
/// `ScriptedDeckClient` finishes its feed immediately, which ends
/// `ConversationSync.run()` and leaves every assertion looking at a
/// conversation that is no longer live. That is why the defect above survived a
/// suite of 357 green tests.
final class LiveDeckClient: DeckClient, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<DeckEvent, Error>.Continuation?
    private var counts: [String: Int] = [:]

    var rosterPayload = RosterPayload(agents: [], threads: [], sectionOrder: [])
    var page = MessagePage(threadID: "", messages: [], isReadOnly: false, participants: [])
    private var _approvalsPage = ApprovalsPage(approvals: [])
    private var _handoffsPage = HandoffsPage(handoffs: [], unreadable: 0)
    private var _resolvedHandoffs: [(String, String)] = []
    /// What `POST /v1/approvals/{id}` answers with (§12: the ask, the rule it
    /// wrote, and whether the desk was actually told to carry on).
    var decision = ApprovalDecision(answered: "once", rule: nil, resumed: true)
    var decideError: DeckError?

    var approvalsPage: ApprovalsPage {
        get { lock.withLock { _approvalsPage } }
        set { lock.withLock { _approvalsPage = newValue } }
    }

    /// Scripted rather than inherited. `DeckClient` defaults `handoffs()` to an
    /// empty page so an older transport degrades instead of crashing; a double
    /// on that default answers "nothing is stuck" whatever the test set up, and
    /// this one is used for every live-poll assertion in the suite.
    var handoffsPage: HandoffsPage {
        get { lock.withLock { _handoffsPage } }
        set { lock.withLock { _handoffsPage = newValue } }
    }
    /// (handoff id, outcome) — `done` and `skipped` are different orders.
    var resolvedHandoffs: [(String, String)] { lock.withLock { _resolvedHandoffs } }

    var historyFetches: Int { lock.withLock { counts["messages", default: 0] } }
    var approvalFetches: Int { lock.withLock { counts["approvals", default: 0] } }
    var handoffFetches: Int { lock.withLock { counts["handoffs", default: 0] } }

    private func tally(_ key: String) { lock.withLock { counts[key, default: 0] += 1 } }

    /// The deck saying something on the open stream, right now.
    func push(_ event: DeckEvent) {
        lock.withLock { continuation }?.yield(event)
    }

    /// The socket dying under the watcher.
    func dropStream() {
        lock.withLock { continuation }?.finish(throwing: DeckError.transport("dropped"))
    }

    func events() -> AsyncThrowingStream<DeckEvent, Error> {
        AsyncThrowingStream { continuation in
            self.lock.withLock { self.continuation = continuation }
            // The deck's first frame after `hello`; also proof the socket is up.
            continuation.yield(.heartbeat)
        }
    }

    /// Runs after the page is taken and before it is answered: the deck
    /// saying something while the history fetch is still in flight.
    var duringFetch: (() -> Void)?

    func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
        tally("messages")
        defer { duringFetch?(); duringFetch = nil }
        guard let since else { return page }
        var caught = page
        caught.messages = page.messages.filter { $0.cursor > since }
        return caught
    }

    func approvals() async throws -> ApprovalsPage {
        tally("approvals")
        return approvalsPage
    }

    func handoffs() async throws -> HandoffsPage {
        tally("handoffs")
        return handoffsPage
    }

    @discardableResult
    func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
        lock.withLock {
            _resolvedHandoffs.append((id, outcome.rawValue))
            _handoffsPage.handoffs.removeAll { $0.id == id }
        }
        return HandoffResolution(outcome: "resolved", resumed: true)
    }

    func roster() async throws -> RosterPayload { rosterPayload }
    func threads() async throws -> [ThreadSummary] { rosterPayload.threads }
    func agent(named name: String) async throws -> Agent {
        guard let found = rosterPayload.agents.first(where: { $0.name == name }) else {
            throw DeckError.unknownAgent
        }
        return found
    }
    func updateAgent(_ agent: Agent) async throws -> Agent { agent }
    /// What `POST .../messages` answers, in order; empty is a refusal.
    var sendResults: [Message] = []
    func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
        lock.lock()
        defer { lock.unlock() }
        guard !sendResults.isEmpty else { throw DeckError.unknownThread }
        return sendResults.removeFirst()
    }
    func markRead(agent: String, upTo: String?) async throws {}
    func createAgent(_ draft: AgentDraft) async throws -> Agent { throw DeckError.unknownAgent }
    func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
        throw DeckError.unknownAgent
    }
    func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
        if let decideError { throw decideError }
        return decision
    }
    func routines() async throws -> [Routine] { [] }
    func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
        throw DeckError.http(500, reason: "no routines here")
    }
    func setRoutine(id: String, enabled: Bool) async throws -> Routine {
        throw DeckError.http(500, reason: "no routines here")
    }
    func deleteRoutine(id: String) async throws {}

    // MARK: fixtures

    static func reply(_ id: String, _ text: String, at offset: TimeInterval) -> Message {
        Message(
            id: id,
            cursor: String(format: "%08d", Int(1_788_222_700 + offset)),
            threadID: "direct:chief",
            author: "chief",
            role: .agent,
            sentAt: Date(timeIntervalSince1970: 1_788_222_700 + offset),
            text: text
        )
    }

    /// One desk, one open conversation, one thing already said in it.
    static func withChief(saying opening: String) -> LiveDeckClient {
        let client = LiveDeckClient()
        var chief = makeAgent("chief")
        chief.state = .idle
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        var opener = reply("m1", opening, at: 0)
        opener.author = DeckOwner.name
        opener.role = .owner
        client.page = MessagePage(
            threadID: "direct:chief",
            messages: [opener],
            isReadOnly: false,
            participants: ["owner", "chief"]
        )
        return client
    }

    /// §12's payload, verbatim, for one desk.
    static func oneApproval(from agent: String) throws -> ApprovalsPage {
        try approvals([("z47x8", agent, 1_788_222_710)])
    }

    /// Several asks, each at a stated time, so a test can pin where they land
    /// in a conversation.
    static func approvals(_ asks: [(String, String, TimeInterval)]) throws -> ApprovalsPage {
        let entries = asks.map { id, agent, ts in
            """
            {"id":"\(id)","ts":\(ts),"agent":"\(agent)","tool":"Bash",
              "subject":"gh pr create --title 'ship the routes'",
              "cwd":"/Users/sam/Projects/acme","cwd_short":"~/Projects/acme",
              "status":"pending","options":[
                {"reply":"once","available":true,"rule":null,"summary":"just this time"},
                {"reply":"always","available":true,
                 "summary":"allow `gh pr*` in ~/Projects/acme, never ask again",
                 "rule":\(alwaysAllowJSON)},
                {"reply":"never","available":true,
                 "summary":"refuse `gh pr*` in ~/Projects/acme from now on",
                 "rule":\(denyJSON)}]}
            """
        }
        return try DeckCoding.decoder.decode(
            ApprovalsPage.self,
            from: Data("{\"approvals\":[\(entries.joined(separator: ","))]}".utf8)
        )
    }

    static let alwaysAllowJSON = """
    {"id":"ask-z47x8","kind":"always_allow","tool":"Bash","pattern":"gh pr*",
     "cwd":"/Users/sam/Projects/acme","note":"from chief on WhatsApp"}
    """

    static let denyJSON = """
    {"id":"ask-z47x8","kind":"deny","tool":"Bash","pattern":"gh pr*",
     "cwd":"/Users/sam/Projects/acme","note":"from chief on WhatsApp"}
    """

    static func alwaysAllowRule() throws -> ApprovalRule {
        try DeckCoding.decoder.decode(ApprovalRule.self, from: Data(alwaysAllowJSON.utf8))
    }

    static func denyRule() throws -> ApprovalRule {
        try DeckCoding.decoder.decode(ApprovalRule.self, from: Data(denyJSON.utf8))
    }
}

extension NSLock {
    fileprivate func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}

@MainActor
extension DeckStore {
    /// What is actually drawn in the conversation, as text.
    var transcript: [String] {
        guard case .loaded(let screen) = thread else { return [] }
        return screen.rows.map(\.message.text)
    }
}
