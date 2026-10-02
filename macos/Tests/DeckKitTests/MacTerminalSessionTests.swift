import XCTest
@testable import DeckKit

/// **His Mac's terminal: the gate and the wire, without a bridge.**
///
/// What goes wrong without these: a terminal that opens outside Full access;
/// bytes that cross mangled or a keystroke that is dropped; a text message
/// that is not on the desks' wire acted on; a session nobody uses that stays
/// open forever. A fake socket and a fake shell: no network, no process.
final class MacTerminalSessionTests: XCTestCase {

    // MARK: fakes

    final class FakeSocket: MacTerminalSocket, @unchecked Sendable {
        let lock = NSLock()
        var sent: [MacTerminalFrame] = []
        var inbox: [MacTerminalFrame] = []
        var closed = false
        func push(_ f: MacTerminalFrame) { lock.withLock { inbox.append(f) } }
        func receive() async throws -> MacTerminalFrame {
            while true {
                let next = lock.withLock { () -> MacTerminalFrame?? in
                    if closed { return .some(nil) }
                    return inbox.isEmpty ? nil : .some(inbox.removeFirst())
                }
                if let next { guard let f = next else { throw URLError(.networkConnectionLost) }; return f }
                try await Task.sleep(nanoseconds: 2_000_000)
            }
        }
        func send(_ frame: MacTerminalFrame) async throws {
            try lock.withLock {
                if closed { throw URLError(.networkConnectionLost) }
                sent.append(frame)
            }
        }
        func close() { lock.withLock { closed = true } }
        var texts: [String] { lock.withLock { sent.compactMap { if case .text(let t) = $0 { t } else { nil } } } }
        var bytes: Data { lock.withLock { sent.reduce(into: Data()) { if case .bytes(let d) = $1 { $0.append(d) } } } }
        var isClosed: Bool { lock.withLock { closed } }
    }

    final class FakeShell: MacTerminalShelling, @unchecked Sendable {
        let lock = NSLock()
        var out: [Data] = []
        var exited = false
        var written = Data()
        var sizes: [[Int]] = []
        var acks: [Int] = []
        var ended: Bool?
        var reattachable = false
        func emit(_ s: String) { lock.withLock { out.append(Data(s.utf8)) } }
        func exit() { lock.withLock { exited = true } }
        func next() async -> Data? {
            while !Task.isCancelled {
                let n = lock.withLock { () -> Data?? in
                    if !out.isEmpty { return .some(out.removeFirst()) }
                    return exited ? .some(nil) : nil
                }
                if let n { return n }
                try? await Task.sleep(nanoseconds: 2_000_000)
            }
            return nil
        }
        func write(_ data: Data) { lock.withLock { written.append(data) } }
        func resize(cols: Int, rows: Int) { lock.withLock { sizes.append([cols, rows]) } }
        func acked(_ total: Int) { lock.withLock { acks.append(total) } }
        var isAlive: Bool { lock.withLock { ended == nil && !exited } }
        func detach() {}
        func reattach() -> Data { Data() }
        func end(kill: Bool) { lock.withLock { ended = kill } }
        var input: String { lock.withLock { String(decoding: written, as: UTF8.self) } }
        var endedWith: Bool? { lock.withLock { ended } }
    }

    // MARK: the gate

    func testOnlyFullAccessOpensItAndTheRefusalSaysWhatToTurnOn() {
        XCTAssertNil(MacTerminalGate.refusal(mode: .full))
        for mode in [MacMode.ask, .paused, .off] {
            XCTAssertEqual(MacTerminalGate.refusal(mode: mode),
                           "Turn on Full access on your Mac (Shaliach → Settings → Mac) to use its terminal.")
        }
    }

    func testAnIdleSessionCloses() async {
        let shell = FakeShell(), sock = FakeSocket()
        let session = MacTerminalSession(socket: sock, shell: shell, idle: 0.2, tick: 0.05)
        let end = await session.run(hello: "{}")
        XCTAssertEqual(end, .idle)
        XCTAssertTrue(sock.isClosed)
    }

    // MARK: the framing

    func testTheWireIsTheDesksTerminalWireFromTheOtherEnd() async {
        let shell = FakeShell(), sock = FakeSocket()
        let session = MacTerminalSession(socket: sock, shell: shell)
        let run = Task { await session.run(hello: MacTerminalControl.hello(name: "Test Mac", cols: 100, rows: 30),
                                           replay: Data("missed".utf8)) }
        shell.emit("$ ")
        sock.push(.bytes(Data([0x6c, 0x73, 0x0d])))
        sock.push(.text(#"{"type":"resize","cols":120,"rows":40}"#))
        sock.push(.text(#"{"type":"ack","bytes":2}"#))
        sock.push(.text(#"{"type":"run","command":"rm -rf /"}"#))   // not on the wire: ignored
        try? await Task.sleep(nanoseconds: 200_000_000)
        shell.exit()
        let end = await run.value
        XCTAssertEqual(end, .shellExited)
        let hello = sock.texts.first ?? ""
        XCTAssertTrue(hello.contains(#""type":"hello""#) && hello.contains(#""windows":["deck"]"#)
                      && hello.contains(#""read_only":false"#), hello)
        XCTAssertEqual(String(decoding: sock.bytes, as: UTF8.self), "missed$ ")
        XCTAssertEqual(shell.input, "ls\r")
        XCTAssertEqual(shell.lock.withLock { shell.sizes }, [[120, 40]])
        XCTAssertEqual(shell.lock.withLock { shell.acks }, [2])
        XCTAssertEqual(sock.texts.last, #"{"code":0,"type":"exit"}"#)
    }

    func testThePollCarriesTheOffer() throws {
        let json = #"{"jobs":[],"cancel":[],"server_ts":1,"terminal":{"session":"abc","cols":100,"rows":30,"from":"iPhone"}}"#
        let r = try JSONDecoder().decode(MacPollResponse.self, from: Data(json.utf8))
        XCTAssertEqual(r.terminal, MacTerminalOffer(session: "abc", cols: 100, rows: 30, from: "iPhone"))
        XCTAssertNil(try JSONDecoder().decode(MacPollResponse.self, from: Data(#"{"jobs":[]}"#.utf8)).terminal)
    }

}
