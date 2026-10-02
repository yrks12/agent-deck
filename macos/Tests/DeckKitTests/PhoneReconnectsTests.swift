import XCTest
@testable import DeckKit

/// **"It sat on 'Finding your deck' while the deck was healthy."**
///
/// The owner's screenshot (00:13): the Agents tab on its first load, the pill
/// reading "Finding your deck", the deck answering healthz 200 and his
/// WireGuard handshake fresh. On the phone a first request can stall (the
/// tunnel waking, a socket from before a deck restart) and nothing bounded it:
/// the roster waited out URLSession's 60s with no retry after, the stream
/// waited its 35s silence budget before trying again. These pin the rules that
/// keep it from hanging: a short first-byte budget on the stream (the deck
/// says hello the moment it connects), a bounded first load that retries
/// itself, and a backoff that is quick at first and capped low, because
/// deploys restart the deck often.
final class PhoneReconnectsTests: XCTestCase {

    // MARK: - backoff

    func testTheBackoffStartsFastAndIsCappedSoADeckRestartIsPickedUpQuickly() {
        var backoff = ReconnectBackoff()
        let waits = (0..<8).map { _ in backoff.next() }
        XCTAssertEqual(waits, [0.5, 1, 2, 4, 8, 10, 10, 10])
        XCTAssertEqual(backoff.attempt, 8)
        backoff.reset()
        XCTAssertEqual(backoff.attempt, 0)
        XCTAssertEqual(backoff.next(), 0.5, "after a success the next failure retries fast again")
    }

    // MARK: - the stream

    func testAStreamThatNeverSaysHelloIsGivenUpOnWithinTheFirstByteBudget() async {
        let silent = AsyncThrowingStream<String, Error> { _ in }   // connects, never sends
        let started = Date()
        do {
            for try await _ in HTTPDeckClient.withSilenceWatchdog(silent, budget: 30, firstByte: 0.3) {}
            XCTFail("a silent stream ended without an error")
        } catch {
            XCTAssertTrue(error is StreamWentQuiet, "\(error)")
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 2,
                          "waited the whole silence budget for a hello the deck sends at once")
    }

    func testOnceTheDeckHasSpokenTheLongerSilenceBudgetApplies() async throws {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        continuation.yield("data: {\"type\":\"hello\"}\n")
        let watched = HTTPDeckClient.withSilenceWatchdog(stream, budget: 30, firstByte: 0.2)
        var iterator = watched.makeAsyncIterator()
        _ = try await iterator.next()
        // Quiet for longer than the first-byte budget, well inside the budget.
        try await Task.sleep(nanoseconds: 600_000_000)
        continuation.yield("data: {\"type\":\"heartbeat\"}\n")
        let next = try await iterator.next()
        XCTAssertNotNil(next, "a heartbeat 0.6s after hello was cut off by the first-byte budget")
        continuation.finish()
    }

    func testTheLiveClientUsesAShortFirstByteBudget() {
        XCTAssertLessThanOrEqual(HTTPDeckClient.firstByteBudget, 10)
        XCTAssertLessThan(HTTPDeckClient.firstByteBudget, HTTPDeckClient.silenceBudget)
    }

    // MARK: - the first load

    private final class Attempts: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func bump() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n }
        var count: Int { lock.lock(); defer { lock.unlock() }; return n }
    }

    private final class Log: @unchecked Sendable {
        private let lock = NSLock()
        private var items: [String] = []
        func add(_ s: String) { lock.lock(); items.append(s); lock.unlock() }
        var all: [String] { lock.lock(); defer { lock.unlock() }; return items }
    }

    func testAFirstLoadThatHangsIsCutOffAndTriedAgain() async {
        let attempts = Attempts()
        let failures = Log()
        let started = Date()
        let loaded = await FirstLoad.untilLoaded(timeout: 0.3, backoff: ReconnectBackoff(),
                                                 sleep: { _ in }, onFailure: { failures.add("\($0)") }) {
            if attempts.bump() == 1 {
                try await Task.sleep(nanoseconds: 60_000_000_000)   // the stalled socket
            }
        }
        XCTAssertTrue(loaded)
        XCTAssertEqual(attempts.count, 2)
        XCTAssertEqual(failures.all.count, 1)
        XCTAssertLessThan(Date().timeIntervalSince(started), 3, "the first load waited out a stalled request")
    }

    func testAFirstLoadThatFailsRetriesOnTheBackoffUntilItWorks() async {
        let attempts = Attempts()
        let slept = Log()
        let loaded = await FirstLoad.untilLoaded(timeout: 5, backoff: ReconnectBackoff(),
                                                 sleep: { slept.add("\($0)") }, onFailure: { _ in }) {
            if attempts.bump() < 4 { throw DeckError.transport("connection refused") }
        }
        XCTAssertTrue(loaded)
        XCTAssertEqual(attempts.count, 4)
        XCTAssertEqual(slept.all, ["0.5", "1.0", "2.0"])
    }

    func testAFirstLoadStopsWhenItsTaskIsCancelled() async {
        let task = Task {
            await FirstLoad.untilLoaded(timeout: 5, backoff: ReconnectBackoff(),
                                        sleep: { try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) },
                                        onFailure: { _ in }) {
                throw DeckError.transport("down")
            }
        }
        try? await Task.sleep(nanoseconds: 200_000_000)
        task.cancel()
        let loaded = await task.value
        XCTAssertFalse(loaded)
    }
}
