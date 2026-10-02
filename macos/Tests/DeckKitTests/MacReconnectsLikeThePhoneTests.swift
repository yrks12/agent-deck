import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The class sweep: the Mac app gets the phone's reconnect rules.**
///
/// The phone was fixed for "Finding your deck" (bounded first load, quick
/// capped backoff). The Mac shares the transport and `ConversationSync` and had
/// the same shape: a thread's backoff that grew to 30s and never came down
/// after it reconnected (after a few deploys, every restart cost him half a
/// minute), a first roster load nothing bounded (URLSession's 60s), and a deck
/// that was down when the app opened that was never asked again until he
/// pressed "Try again".
@MainActor
final class MacReconnectsLikeThePhoneTests: XCTestCase {

    func testTheThreadBackoffIsTheQuickCappedOne() {
        XCTAssertEqual((1...7).map(ConversationSync.backoffSeconds(attempt:)), [0.5, 1, 2, 4, 8, 10, 10],
                       "a deck restarted by a deploy is found up to 30s late")
    }

    private final class Attempts: @unchecked Sendable {
        private let lock = NSLock()
        private var items: [Int] = []
        func add(_ n: Int) { lock.lock(); items.append(n); lock.unlock() }
        var all: [Int] { lock.lock(); defer { lock.unlock() }; return items }
    }

    func testAThreadThatReconnectedStartsItsBackoffAgain() async throws {
        let client = ScriptedDeckClient()
        let page = MessagePage(threadID: "direct:atlas", messages: [], isReadOnly: false,
                               participants: ["owner", "atlas"])
        client.pages = [page, page, page, page]
        // Three deploys: each time the stream is live (a heartbeat), then drops.
        client.feeds = [.emitThenDrop([.heartbeat]), .emitThenDrop([.heartbeat]),
                        .emitThenDrop([.heartbeat]), .emitThenFinish([])]
        let attempts = Attempts()
        let sync = ConversationSync(client: client, threadID: "direct:atlas",
                                    backoff: { attempts.add($0) })
        try await sync.run()
        XCTAssertEqual(attempts.all, [1, 1, 1],
                       "each restart waited longer than the last although the deck had come back in between")
    }

    func testAMacThatHasNeverReachedTheDeckKeepsTryingOnItsOwn() async {
        let client = ScriptedDeckClient()
        client.rosterError = .transport("connection refused")
        let store = DeckStore(client: client, backoff: { _ in try await Task.sleep(nanoseconds: 2_000_000) },
                              approvalPollInterval: 600)
        await store.loadRoster()
        guard case .failed = store.roster else { return XCTFail("precondition: the first load failed") }

        // The deck comes up; nobody presses anything.
        client.rosterPayload = RosterPayload(agents: [makeAgent("atlas")],
                                             threads: [makeThread("direct:atlas", agent: "atlas")],
                                             sectionOrder: ["Work"])
        client.rosterError = nil
        await waitFor("the Mac to find the deck by itself") {
            if case .loaded = store.roster { return true }
            return false
        }
    }

    func testAFirstRosterLoadThatStallsIsCutOffAndTriedAgain() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(agents: [makeAgent("atlas")],
                                             threads: [makeThread("direct:atlas", agent: "atlas")],
                                             sectionOrder: ["Work"])
        client.rosterStallsOnce = 60
        let store = DeckStore(client: client, backoff: { _ in try await Task.sleep(nanoseconds: 2_000_000) },
                              approvalPollInterval: 600, rosterTimeout: 0.2)
        let started = Date()
        Task { await store.loadRoster() }
        await waitFor("the stalled load to be cut off and the roster to arrive") {
            if case .loaded = store.roster { return true }
            return false
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 3, "the sidebar spun for the whole stalled request")
    }

    private func waitFor(_ what: String, within timeout: TimeInterval = 5,
                         file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for \(what)", file: file, line: line)
    }
}
