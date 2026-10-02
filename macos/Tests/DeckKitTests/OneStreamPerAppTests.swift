import XCTest
@testable import DeckKit

/// **One live-updates connection per app, not one per open thread.**
///
/// Measured on the box (agentdeck journal, 04:19–04:30 UTC): the iPhone opened
/// a new `/v1/stream` every time he opened a thread — `GET .../messages` then
/// `GET /v1/stream`, a dozen in ten minutes — on top of the roster's own. The
/// deck runs one stream and fans everything down it; on a memory-tight box
/// every extra socket is a held response and a queue. `SharedDeckEvents` is
/// the client half of that rule: one upstream, any number of listeners.
final class OneStreamPerAppTests: XCTestCase {

    /// An upstream the test drives, counting how many times it was opened.
    private final class Upstream: @unchecked Sendable {
        private let lock = NSLock()
        private var continuations: [AsyncThrowingStream<DeckEvent, Error>.Continuation] = []
        private var cancelled = 0

        var opened: Int { lock.lock(); defer { lock.unlock() }; return continuations.count }
        var closed: Int { lock.lock(); defer { lock.unlock() }; return cancelled }

        func open() -> AsyncThrowingStream<DeckEvent, Error> {
            AsyncThrowingStream { continuation in
                lock.lock(); continuations.append(continuation); lock.unlock()
                continuation.onTermination = { [weak self] _ in
                    guard let self else { return }
                    self.lock.lock(); self.cancelled += 1; self.lock.unlock()
                }
            }
        }

        func send(_ event: DeckEvent) { lock.lock(); let c = continuations.last; lock.unlock(); c?.yield(event) }
        func drop() {
            lock.lock(); let c = continuations.last; lock.unlock()
            c?.finish(throwing: StreamWentQuiet(after: 1))
        }
    }

    private func settle() async { try? await Task.sleep(nanoseconds: 100_000_000) }

    func testTheRosterAndTwoOpenThreadsShareOneConnection() async throws {
        let upstream = Upstream()
        let hub = SharedDeckEvents(open: { upstream.open() })
        var roster = hub.events().makeAsyncIterator()
        var thread = hub.events().makeAsyncIterator()
        var call = hub.events().makeAsyncIterator()
        await settle()
        XCTAssertEqual(upstream.opened, 1, "every listener opened its own /v1/stream")

        upstream.send(.heartbeat)
        let a = try await roster.next(), b = try await thread.next(), c = try await call.next()
        XCTAssertEqual(a, .heartbeat)
        XCTAssertEqual(b, .heartbeat)
        XCTAssertEqual(c, .heartbeat)
        let opened = await hub.connectionsOpened
        XCTAssertEqual(opened, 1)
    }

    func testADroppedStreamEndsEveryListenerAndTheyRejoinOneNewConnection() async throws {
        let upstream = Upstream()
        let hub = SharedDeckEvents(open: { upstream.open() })
        var first = hub.events().makeAsyncIterator()
        var second = hub.events().makeAsyncIterator()
        await settle()
        upstream.drop()   // the deck restarted
        do { _ = try await first.next(); XCTFail("the drop was swallowed") } catch {}
        do { _ = try await second.next(); XCTFail("the drop was swallowed") } catch {}

        // Each reconnects on its own backoff: still one connection between them.
        var again1 = hub.events().makeAsyncIterator()
        var again2 = hub.events().makeAsyncIterator()
        await settle()
        XCTAssertEqual(upstream.opened, 2, "after a drop each listener reconnected separately")
        upstream.send(.heartbeat)
        let x = try await again1.next(), y = try await again2.next()
        XCTAssertEqual(x, .heartbeat)
        XCTAssertEqual(y, .heartbeat)
    }

    func testClosingAThreadKeepsTheStreamAndTheLastListenerClosesIt() async throws {
        let upstream = Upstream()
        let hub = SharedDeckEvents(open: { upstream.open() })
        var roster = hub.events().makeAsyncIterator()
        let threadTask = Task { for try await _ in hub.events() {} }
        await settle()
        threadTask.cancel()   // he closed the thread
        await settle()
        XCTAssertEqual(upstream.closed, 0, "closing a thread closed the roster's stream too")
        upstream.send(.heartbeat)
        let still = try await roster.next()
        XCTAssertEqual(still, .heartbeat)

        let rosterTask = Task { for try await _ in hub.events() {} }
        await settle()
        rosterTask.cancel()
        _ = roster   // the iterator above is released with the test
        await settle()
    }

    func testNobodyListeningMeansNoConnection() async {
        let upstream = Upstream()
        let hub = SharedDeckEvents(open: { upstream.open() })
        let task = Task { for try await _ in hub.events() {} }
        await settle()
        task.cancel()
        await settle()
        XCTAssertEqual(upstream.closed, 1, "a stream nobody listens to was left open on the box")
    }

    func testRetryOrComingBackToTheFrontReplacesTheConnectionForEveryone() async throws {
        let upstream = Upstream()
        let hub = SharedDeckEvents(open: { upstream.open() })
        var roster = hub.events().makeAsyncIterator()
        var thread = hub.events().makeAsyncIterator()
        await settle()
        await hub.reconnect()   // a socket that slept in the background is not trusted
        do { _ = try await roster.next(); XCTFail("the roster kept listening to the old socket") } catch {}
        do { _ = try await thread.next(); XCTFail("the thread kept listening to the old socket") } catch {}
        XCTAssertEqual(upstream.closed, 1, "the old socket was left open on the box")
        let back1 = hub.events(), back2 = hub.events()
        await settle()
        XCTAssertEqual(upstream.opened, 2)
        withExtendedLifetime((back1, back2)) {}
    }

    // MARK: - the live client

    private final class CountingSSE: SSETransport, @unchecked Sendable {
        private let lock = NSLock()
        private var count = 0
        var opened: Int { lock.lock(); defer { lock.unlock() }; return count }
        func lines(for request: URLRequest) -> AsyncThrowingStream<String, Error> {
            lock.lock(); count += 1; lock.unlock()
            return AsyncThrowingStream { _ in }
        }
    }

    func testAClientThatSharesItsStreamOpensOneSocketForEveryListener() async {
        let sse = CountingSSE()
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.test")!,
                                    tokens: InMemoryTokenStore(token: "t"), sse: sse, sharesOneStream: true)
        let tasks = (0..<3).map { _ in Task { for try await _ in client.events() {} } }
        await settle()
        XCTAssertEqual(sse.opened, 1, "three listeners on one client opened \(sse.opened) sockets")
        tasks.forEach { $0.cancel() }
    }

    func testAPlainClientStillOpensItsOwn() async {
        let sse = CountingSSE()
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.test")!,
                                    tokens: InMemoryTokenStore(token: "t"), sse: sse)
        let tasks = (0..<2).map { _ in Task { for try await _ in client.events() {} } }
        await settle()
        XCTAssertEqual(sse.opened, 2)
        tasks.forEach { $0.cancel() }
    }
}
