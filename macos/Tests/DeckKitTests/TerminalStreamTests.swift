import XCTest
@testable import DeckKit

/// **"The terminal of each computer should look and feel like a terminal."**
///
/// The one-shot `POST /terminal` cannot run vim, top or ssh: every command is
/// a fresh `bash -lc` that is cut off at twenty seconds. The stream is a real
/// PTY (`WS /v1/agents/{name}/terminal/stream`), so this pins the client half
/// of its wire and the model that drives the emulator from it — with a fake
/// socket, no deck and no window.
@MainActor
final class TerminalStreamTests: XCTestCase {

    // MARK: - the wire

    func testHelloCarriesTheWindowTheGridAndWhetherHeMayType() {
        let text = #"{"type":"hello","desk":"atlas","window":"agent","windows":["deck","agent"],"read_only":true,"cols":120,"rows":40}"#
        XCTAssertEqual(TerminalStreamEvent.parse(text: text), .hello(TerminalHello(
            desk: "atlas", window: .agent, windows: [.deck, .agent],
            readOnly: true, cols: 120, rows: 40)))
    }

    func testAHelloWithoutReadOnlyIsWritable() {
        let text = #"{"type":"hello","desk":"atlas","window":"deck","cols":80,"rows":24}"#
        guard case .hello(let hello)? = TerminalStreamEvent.parse(text: text) else {
            return XCTFail("hello did not parse")
        }
        XCTAssertFalse(hello.readOnly)
        XCTAssertEqual(hello.windows, [.deck, .agent])
    }

    func testBinaryIsRawOutputEvenWhenItSplitsACharacter() {
        // The first byte of "é" alone: the emulator, not this parser, joins it.
        let half = Data([0x68, 0xC3])
        XCTAssertEqual(TerminalStreamEvent.parse(binary: half), .output(half))
    }

    func testExitErrorAndTheUnknown() {
        XCTAssertEqual(TerminalStreamEvent.parse(text: #"{"type":"exit","code":3}"#), .exit(code: 3))
        XCTAssertEqual(TerminalStreamEvent.parse(
            text: #"{"type":"error","reason":"computer_not_running","detail":"deck-desk-atlas is stopped"}"#),
                       .error(reason: "computer_not_running", detail: "deck-desk-atlas is stopped"))
        XCTAssertEqual(TerminalStreamEvent.parse(text: #"{"type":"error"}"#),
                       .error(reason: "error", detail: ""))
        XCTAssertNil(TerminalStreamEvent.parse(text: #"{"type":"from-the-future"}"#))
        XCTAssertNil(TerminalStreamEvent.parse(text: "not json"))
    }

    func testResizeAndAckAreTheExactJSONTheDeckReads() {
        XCTAssertEqual(TerminalStreamWire.resize(cols: 120, rows: 40),
                       #"{"type":"resize","cols":120,"rows":40}"#)
        XCTAssertEqual(TerminalStreamWire.ack(bytes: 4096), #"{"type":"ack","bytes":4096}"#)
    }

    func testAResizeIsClampedToWhatTheDeckAccepts() {
        XCTAssertEqual(TerminalStreamWire.resize(cols: 0, rows: 900),
                       #"{"type":"resize","cols":1,"rows":200}"#)
        XCTAssertEqual(TerminalStreamWire.resize(cols: 9000, rows: -2),
                       #"{"type":"resize","cols":500,"rows":1}"#)
    }

    func testCloseCodesAreReasonsNotNumbers() {
        XCTAssertEqual(TerminalStreamFailure.fromClose(code: 4401), .unauthorized)
        XCTAssertEqual(TerminalStreamFailure.fromClose(code: 4403), .unauthorized)
        XCTAssertEqual(TerminalStreamFailure.fromClose(code: 4404), .unknownDesk)
        XCTAssertEqual(TerminalStreamFailure.fromClose(code: 4409), .computerNotRunning(""))
        // A drop is not a refusal: these reconnect.
        XCTAssertNil(TerminalStreamFailure.fromClose(code: 1006))
        XCTAssertNil(TerminalStreamFailure.fromClose(code: 1001))
        XCTAssertNil(TerminalStreamFailure.fromClose(code: 1011))
    }

    func testOnlyTheMissingTerminalFallsBackToTheOneShotDrawer() {
        XCTAssertTrue(TerminalStreamFailure.fromError(reason: "terminal_unavailable", detail: "")
            .fallsBackToOneShot)
        // A deck with no stream route at all refuses the upgrade itself.
        XCTAssertTrue(TerminalStreamFailure.fromHandshake(status: 403).fallsBackToOneShot)
        XCTAssertFalse(TerminalStreamFailure.fromError(reason: "computer_not_running", detail: "")
            .fallsBackToOneShot)
        XCTAssertFalse(TerminalStreamFailure.unauthorized.fallsBackToOneShot)
        XCTAssertEqual(TerminalStreamFailure.fromHandshake(status: 401), .unauthorized)
    }

    func testEveryFailureIsASentenceNotASlug() {
        let failures: [TerminalStreamFailure] = [
            .computerNotRunning("x"), .unavailable("x"), .unauthorized, .unknownDesk,
            .notOnThisDeck, .other(reason: "pty_failed", detail: ""),
        ]
        for failure in failures {
            XCTAssertFalse(failure.sentence.isEmpty)
            XCTAssertFalse(failure.sentence.contains("_"), failure.sentence)
        }
    }

    func testTheRequestIsTheSocketWithTheBearerAndTheGrid() throws {
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local:8443")!,
                                    tokens: InMemoryTokenStore(token: "t0k"))
        let request = try client.terminalStreamRequest(agent: "atlas", window: .agent,
                                                       cols: 132, rows: 43)
        let url = try XCTUnwrap(request.url)
        let parts = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(parts.scheme, "wss")
        XCTAssertEqual(parts.host, "deck.local")
        XCTAssertEqual(parts.port, 8443)
        XCTAssertEqual(parts.path, "/v1/agents/atlas/terminal/stream")
        XCTAssertEqual(Dictionary(uniqueKeysWithValues: (parts.queryItems ?? []).map { ($0.name, $0.value ?? "") }),
                       ["window": "agent", "cols": "132", "rows": "43"])
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer t0k")

        let plain = HTTPDeckClient(baseURL: URL(string: "http://10.0.0.1:7788")!,
                                   tokens: InMemoryTokenStore(token: "t0k"))
        XCTAssertEqual(try plain.terminalStreamRequest(agent: "a", window: .deck, cols: 80, rows: 24)
            .url?.scheme, "ws")
    }

    // MARK: - the model

    func testHelloMakesItLiveAndOutputReachesTheEmulator() async throws {
        let (model, client, sink) = make()
        let socket = client.nextSocket()
        model.attach(sink)
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        socket.push(.output(Data("hi".utf8)))
        try await until { sink.fed == Data("hi".utf8) }
        XCTAssertEqual(model.state, .live)
        XCTAssertEqual(sink.resets, 1, "a fresh session starts from a clean screen")
        XCTAssertEqual(client.opened.first?.window, .deck)
        model.stop()
    }

    func testInputAndResizeGoOutInOrder() async throws {
        let (model, client, sink) = make()
        let socket = client.nextSocket()
        model.attach(sink)
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        model.send(input: Data("v".utf8))
        model.resize(cols: 100, rows: 30)
        model.resize(cols: 100, rows: 30)   // the same grid again is not news
        model.send(input: Data("i".utf8))
        try await until { socket.sent.count == 3 }
        XCTAssertEqual(socket.sent, [.input(Data("v".utf8)),
                                     .text(#"{"type":"resize","cols":100,"rows":30}"#),
                                     .input(Data("i".utf8))])
        model.stop()
    }

    func testTheGridKnownBeforeConnectingIsTheOneAskedFor() async throws {
        let (model, client, _) = make()
        _ = client.nextSocket()
        model.resize(cols: 140, rows: 50)
        model.start()
        try await until { client.opened.count == 1 }
        XCTAssertEqual(client.opened.first?.cols, 140)
        XCTAssertEqual(client.opened.first?.rows, 50)
        model.stop()
    }

    func testItAcksEvery32KiBProcessedAndNotBefore() async throws {
        let (model, client, sink) = make(idleAck: nil)
        let socket = client.nextSocket()
        model.attach(sink)
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        let chunk = Data(repeating: 0x61, count: 10 * 1024)
        for _ in 0..<3 { socket.push(.output(chunk)) }
        try await until { sink.fed.count == 30 * 1024 }
        await settle()
        XCTAssertEqual(socket.acks, [], "30 KiB processed is under the 32 KiB cadence")
        socket.push(.output(chunk))
        try await until { socket.acks == [40 * 1024] }
        XCTAssertEqual(model.ackedBytes, 40 * 1024, "the ack is cumulative")
        model.stop()
    }

    func testAnIdleMomentAcksWhateverIsOutstanding() async throws {
        let (model, client, sink) = make(idleAck: 0.01)
        let socket = client.nextSocket()
        model.attach(sink)
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        socket.push(.output(Data(repeating: 0x61, count: 100)))
        try await until { socket.acks == [100] }
        model.stop()
    }

    func testNothingIsAckedUntilTheEmulatorHasTheBytes() async throws {
        let (model, client, sink) = make(idleAck: 0.01)
        let socket = client.nextSocket()
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        socket.push(.output(Data(repeating: 0x62, count: 40 * 1024)))
        try await until { model.pendingBytes == 40 * 1024 }
        await settle()
        XCTAssertEqual(socket.acks, [], "acked bytes nobody drew: the deck's backpressure would be a lie")
        model.attach(sink)
        try await until { socket.acks == [40 * 1024] }
        XCTAssertEqual(sink.fed.count, 40 * 1024)
        model.stop()
    }

    func testSwitchingWindowReconnectsAndTheAgentWindowIsReadOnly() async throws {
        let (model, client, sink) = make()
        let first = client.nextSocket()
        let second = client.nextSocket()
        model.attach(sink)
        model.start()
        first.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }

        model.switchWindow(to: .agent)
        XCTAssertTrue(model.isSwitching)
        XCTAssertEqual(model.window, .agent)
        try await until { client.opened.count == 2 }
        XCTAssertTrue(first.closed, "the deck window's socket is let go")
        XCTAssertEqual(client.opened.last?.window, .agent)

        second.push(.hello(.fixture(window: .agent, readOnly: true)))
        try await until { model.state == .readOnly }
        XCTAssertFalse(model.isSwitching)
        XCTAssertEqual(sink.resets, 2, "the other window's screen does not draw over this one")

        model.send(input: Data("x".utf8))
        await settle()
        XCTAssertEqual(second.sent.filter { if case .input = $0 { return true } else { return false } }, [],
                       "a read-only mirror never carries his keystrokes")
        model.stop()
    }

    func testSwitchingToTheWindowItIsOnDoesNothing() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        model.switchWindow(to: .deck)
        XCTAssertFalse(model.isSwitching)
        await settle()
        XCTAssertEqual(client.opened.count, 1)
        model.stop()
    }

    func testExitEndsAndDoesNotReconnect() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        socket.push(.exit(code: 0))
        try await until { model.state == .ended(code: 0) }
        await model.runFinished()
        XCTAssertEqual(client.opened.count, 1)
        XCTAssertTrue(socket.closed)
    }

    func testAnErrorFailsWithItsReasonAndDoesNotReconnect() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.push(.error(reason: "computer_not_running", detail: "stopped"))
        try await until { model.state == .failed(.computerNotRunning("stopped")) }
        await model.runFinished()
        XCTAssertEqual(client.opened.count, 1)
        XCTAssertFalse(model.fallsBackToOneShot)
    }

    func testAnOldContainerFallsBackToTheDrawer() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.push(.error(reason: "terminal_unavailable", detail: "old image"))
        try await until { model.fallsBackToOneShot }
        XCTAssertEqual(model.state, .failed(.unavailable("old image")))
    }

    func testARefusalCloseCodeFails() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.fail(TerminalStreamClosed(code: 4401))
        try await until { model.state == .failed(.unauthorized) }
        await model.runFinished()
        XCTAssertEqual(client.opened.count, 1)
    }

    func testADropReconnectsToTheSameWindowWithBackoff() async throws {
        let (model, client, sink) = make()
        let first = client.nextSocket()
        let second = client.nextSocket()
        model.attach(sink)
        model.start()
        first.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        first.fail(TerminalStreamClosed(code: 1006))
        try await until { client.opened.count == 2 }
        XCTAssertEqual(client.opened.last?.window, .deck)
        XCTAssertEqual(client.sleeps.first, ReconnectBackoff.first)
        second.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        model.stop()
    }

    func testRetryAfterAFailureOpensAgain() async throws {
        let (model, client, _) = make()
        let first = client.nextSocket()
        let second = client.nextSocket()
        model.start()
        first.push(.error(reason: "computer_not_running", detail: ""))
        try await until { if case .failed = model.state { return true } else { return false } }
        model.retry()
        XCTAssertEqual(model.state, .connecting)
        second.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        XCTAssertEqual(client.opened.count, 2)
        model.stop()
    }

    func testStopClosesTheSocket() async throws {
        let (model, client, _) = make()
        let socket = client.nextSocket()
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        model.stop()
        await model.runFinished()
        XCTAssertTrue(socket.closed)
    }

    // MARK: - helpers

    private func make(idleAck: TimeInterval? = nil)
        -> (DeskTerminalModel, FakeTerminalClient, RecordingSink) {
        let client = FakeTerminalClient()
        let model = DeskTerminalModel(desk: "atlas", client: client,
                                      sleep: { [client] in client.recordSleep($0) },
                                      idleAckDelay: idleAck)
        return (model, client, RecordingSink())
    }

    private func until(_ condition: @MainActor () -> Bool,
                       file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("condition never became true", file: file, line: line)
        throw CancellationError()
    }

    private func settle() async {
        for _ in 0..<20 { await Task.yield() }
        try? await Task.sleep(nanoseconds: 30_000_000)
    }
}

extension TerminalHello {
    static func fixture(window: TerminalWindow, readOnly: Bool = false) -> TerminalHello {
        TerminalHello(desk: "atlas", window: window, windows: [.deck, .agent],
                      readOnly: readOnly, cols: 80, rows: 24)
    }
}

@MainActor
final class RecordingSink: TerminalOutputSink {
    var fed = Data()
    var resets = 0
    func terminalFeed(_ bytes: Data) { fed.append(bytes) }
    func terminalReset() { resets += 1; fed = Data() }
}

/// One socket. `next()` waits for whatever the test pushes.
final class FakeTerminalSocket: TerminalStreamSession, @unchecked Sendable {
    enum Sent: Equatable { case input(Data), text(String) }

    private let lock = NSLock()
    private var queue: [Result<TerminalStreamEvent, Error>] = []
    private var waiter: CheckedContinuation<TerminalStreamEvent, Error>?
    private var _sent: [Sent] = []
    private var _closed = false

    var sent: [Sent] { lock.withLock { _sent } }
    var closed: Bool { lock.withLock { _closed } }
    var acks: [Int] {
        sent.compactMap {
            guard case .text(let text) = $0, text.contains("\"ack\""),
                  let body = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
            else { return nil }
            return body["bytes"] as? Int
        }
    }

    func push(_ event: TerminalStreamEvent) { deliver(.success(event)) }
    func fail(_ error: Error) { deliver(.failure(error)) }

    private func deliver(_ result: Result<TerminalStreamEvent, Error>) {
        lock.lock()
        if let waiter {
            self.waiter = nil
            lock.unlock()
            waiter.resume(with: result)
        } else {
            queue.append(result)
            lock.unlock()
        }
    }

    func next() async throws -> TerminalStreamEvent {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if _closed {
                lock.unlock()
                continuation.resume(throwing: TerminalStreamClosed(code: 1000))
            } else if !queue.isEmpty {
                let first = queue.removeFirst()
                lock.unlock()
                continuation.resume(with: first)
            } else {
                waiter = continuation
                lock.unlock()
            }
        }
    }

    func send(input: Data) async throws { lock.withLock { _sent.append(.input(input)) } }
    func send(text: String) async throws { lock.withLock { _sent.append(.text(text)) } }

    func close() {
        lock.lock()
        _closed = true
        let waiter = self.waiter
        self.waiter = nil
        lock.unlock()
        waiter?.resume(throwing: TerminalStreamClosed(code: 1000))
    }
}

final class FakeTerminalClient: DeskTerminalStreaming, @unchecked Sendable {
    struct Opened: Equatable { let window: TerminalWindow; let cols: Int; let rows: Int }

    private let lock = NSLock()
    private var sockets: [FakeTerminalSocket] = []
    private var _opened: [Opened] = []
    private var _sleeps: [TimeInterval] = []

    var opened: [Opened] { lock.withLock { _opened } }
    var sleeps: [TimeInterval] { lock.withLock { _sleeps } }

    /// Queues the socket the next open will hand out.
    func nextSocket() -> FakeTerminalSocket {
        let socket = FakeTerminalSocket()
        lock.withLock { sockets.append(socket) }
        return socket
    }

    func recordSleep(_ seconds: TimeInterval) { lock.withLock { _sleeps.append(seconds) } }

    func openTerminalStream(agent: String, window: TerminalWindow,
                            cols: Int, rows: Int) async throws -> TerminalStreamSession {
        lock.withLock {
            _opened.append(Opened(window: window, cols: cols, rows: rows))
            // Out of scripted sockets: one that stays quiet, never a hot loop.
            return sockets.isEmpty ? FakeTerminalSocket() : sockets.removeFirst()
        }
    }
}
