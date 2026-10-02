import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"I can't load older messages so I can't see the video."**
///
/// The owner, 2026-10-01, on a 681-line thread whose video bubble sat ~250
/// lines up. Opening a thread fetched its newest page and nothing ever asked
/// the deck for anything older: the server answered `has_more_before: true`
/// and a `next_before` cursor, the app decoded both, and no code read them.
/// Everything above the newest 50 lines was unreachable on Mac and iPhone.
///
/// The double below pages exactly as `server/api.py` `page()` does — newest
/// `limit` with no cursor, strictly-older with `before=`, strictly-newer with
/// `since=` — over a thread three and a half pages long.
@MainActor
final class OlderMessagesTests: XCTestCase {

    // MARK: DETECTOR — the sync reaches the first line of a long thread

    func testScrollingToTheTopPagesBackToTheFirstMessage() async throws {
        let client = PagingDeckClient(lines: 175)
        let sync = ConversationSync(client: client, threadID: "direct:chief")
        let run = Task { try await sync.run() }
        defer { run.cancel() }

        try await waitFor("the newest page") { await sync.messages().count == 50 }
        let opened = await sync.hasEarlier
        XCTAssertTrue(opened, "the deck said there is more above; the sync must say so too")

        var pages = 0
        while await sync.hasEarlier, pages < 10 {
            try await sync.loadEarlier()
            pages += 1
        }

        let held = await sync.messages()
        XCTAssertEqual(held.first?.id, "m0",
                       "the first thing ever said in the thread could not be reached")
        XCTAssertEqual(held.count, 175, "every line, once each")
        XCTAssertEqual(held.map(\.id), client.all.map(\.id), "in the deck's order")
        XCTAssertEqual(pages, 3, "three pages back from the newest 50: 50, 50, 25")
        XCTAssertEqual(client.beforeFetches, 3)
    }

    // MARK: DETECTOR — the Mac screen offers it and draws what comes back

    func testTheStoreShowsEarlierMessagesOnRequest() async throws {
        let client = PagingDeckClient(lines: 120)
        let store = DeckStore(client: client)
        await store.loadRoster()
        try await waitFor("the thread opens") { self.screen(store)?.rows.count == 50 }
        XCTAssertEqual(screen(store)?.hasEarlier, true,
                       "a thread longer than one page must offer its earlier lines")

        store.loadEarlier()
        try await waitFor("the second page lands") { self.screen(store)?.rows.count == 100 }
        store.loadEarlier()
        try await waitFor("the first line lands") { self.screen(store)?.rows.count == 120 }

        XCTAssertEqual(screen(store)?.rows.first?.message.id, "m0")
        XCTAssertEqual(screen(store)?.hasEarlier, false, "at the first line there is nothing above")
        try await waitFor("the control settles") { !store.isLoadingEarlier }
    }

    // MARK: sweep — a quote of a line older than anything drawn

    func testTappingAQuoteOfAnOldLinePagesBackToItAndScrolls() async throws {
        let client = PagingDeckClient(lines: 160)
        let store = DeckStore(client: client)
        await store.loadRoster()
        try await waitFor("the thread opens") { self.screen(store)?.rows.count == 50 }

        store.jump(to: MessageQuote(id: "m7", author: "chief", excerpt: "line 7"))

        try await waitFor("the quoted line is reached and jumped to") {
            store.quoteJump?.messageID == "m7"
        }
        XCTAssertTrue(screen(store)?.messages.contains { $0.id == "m7" } ?? false,
                      "the jump must land on a row that is drawn")
    }

    // MARK: the wire

    func testTheHTTPClientAsksForTheOlderPageByCursor() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data("""
        {"thread_id":"direct:chief","messages":[],"has_more_before":false,
         "has_more_after":true,"next_since":"","next_before":""}
        """.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("t")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens, performer: performer)
        _ = try await client.messages(threadID: "direct:chief", before: "00000000000000000042", limit: 50)
        let url = try XCTUnwrap(performer.requests.last?.url?.absoluteString)
        XCTAssertTrue(url.contains("before=00000000000000000042"), url)
        XCTAssertTrue(url.contains("limit=50"), url)
        XCTAssertFalse(url.contains("since="), url)
    }

    // MARK: helpers

    private func screen(_ store: DeckStore) -> ThreadScreen? {
        guard case .loaded(let screen) = store.thread else { return nil }
        return screen
    }

    private func waitFor(
        _ what: String, within timeout: TimeInterval = 3,
        file: StaticString = #filePath, line: UInt = #line,
        _ condition: () async -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await condition() { return }
            try await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for: \(what)", file: file, line: line)
        throw CancellationError()
    }
}

/// A deck holding one long conversation with `chief`, paged the way the real
/// one pages it, with a stream that stays open.
final class PagingDeckClient: DeckClient, @unchecked Sendable {
    let all: [Message]
    private let lock = NSLock()
    private var _beforeFetches = 0
    var beforeFetches: Int { lock.withLock { _beforeFetches } }

    init(lines: Int) {
        all = (0..<lines).map { n in
            Message(
                id: "m\(n)",
                cursor: String(format: "%020d", n + 1),
                threadID: "direct:chief",
                author: n.isMultiple(of: 2) ? "chief" : DeckOwner.name,
                role: n.isMultiple(of: 2) ? .agent : .owner,
                sentAt: Date(timeIntervalSince1970: 1_790_000_000 + Double(n) * 60),
                text: "line \(n)"
            )
        }
    }

    private func page(_ window: ArraySlice<Message>, before: Bool, after: Bool) -> MessagePage {
        MessagePage(
            threadID: "direct:chief", messages: Array(window), isReadOnly: false,
            participants: [DeckOwner.name, "chief"],
            hasMoreBefore: before, hasMoreAfter: after,
            nextSince: window.last?.cursor, nextBefore: window.first?.cursor
        )
    }

    func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
        if let since {
            let start = all.firstIndex { $0.cursor > since } ?? all.count
            let end = min(all.count, start + limit)
            return page(all[start..<end], before: start > 0, after: end < all.count)
        }
        let start = max(0, all.count - limit)
        return page(all[start...], before: start > 0, after: false)
    }

    func messages(threadID: String, before: String, limit: Int) async throws -> MessagePage {
        lock.withLock { _beforeFetches += 1 }
        let end = all.firstIndex { $0.cursor >= before } ?? all.count
        let start = max(0, end - limit)
        return page(all[start..<end], before: start > 0, after: end < all.count)
    }

    func events() -> AsyncThrowingStream<DeckEvent, Error> {
        AsyncThrowingStream { continuation in continuation.yield(.heartbeat) }
    }

    func roster() async throws -> RosterPayload {
        var chief = makeAgent("chief")
        chief.state = .idle
        return RosterPayload(agents: [chief], threads: [makeThread("direct:chief", agent: "chief")],
                             sectionOrder: ["Work"])
    }
    func threads() async throws -> [ThreadSummary] { [] }
    func agent(named name: String) async throws -> Agent { throw DeckError.unknownAgent }
    func updateAgent(_ agent: Agent) async throws -> Agent { agent }
    func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
        throw DeckError.unknownThread
    }
    func markRead(agent: String, upTo: String?) async throws {}
    func createAgent(_ draft: AgentDraft) async throws -> Agent { throw DeckError.unknownAgent }
    func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
        throw DeckError.unknownAgent
    }
    func approvals() async throws -> ApprovalsPage { .init(approvals: [], unreadable: 0) }
    func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
        throw DeckError.unknownAsk
    }
    func routines() async throws -> [Routine] { [] }
    func createRoutine(_ draft: RoutineDraft) async throws -> Routine { throw DeckError.unknownRoutine }
    func setRoutine(id: String, enabled: Bool) async throws -> Routine { throw DeckError.unknownRoutine }
    func deleteRoutine(id: String) async throws {}
    func handoffs() async throws -> HandoffsPage { HandoffsPage(handoffs: [], unreadable: 0) }
    func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
        HandoffResolution(outcome: outcome.rawValue, resumed: true)
    }
}
