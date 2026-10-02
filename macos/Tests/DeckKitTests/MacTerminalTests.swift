import XCTest
@testable import DeckKit

/// **His Mac's terminal through the bridge and the app's handle.**
///
/// What goes wrong without these: a shell that runs in Ask mode or while
/// Paused; a refusal the viewer never hears; Stop or ⌃⌥⌘. that leaves the
/// shell running; leaving Full access that does not end it; keystrokes in the
/// activity log. Fake node, fake socket, fake shell — never a real one.
final class MacTerminalTests: XCTestCase {
    typealias FakeSocket = MacTerminalSessionTests.FakeSocket
    typealias FakeShell = MacTerminalSessionTests.FakeShell

    // MARK: fakes

    final class Node: MacNodeClienting, @unchecked Sendable {
        let deckURL = URL(string: "http://10.99.0.1:7789")!
        let lock = NSLock()
        var script: [MacPollResponse] = []
        var sockets: [String: FakeSocket] = [:]
        func register(_ body: MacRegisterRequest) async throws -> MacRegisterResponse {
            MacRegisterResponse(nodeId: "mac_aaaaaaaaaaaa", nodeSecret: nil, name: body.name, primary: true)
        }
        func poll(nodeId: String, _ body: MacPollRequest) async throws -> MacPollResponse {
            if let next = lock.withLock({ script.isEmpty ? nil : script.removeFirst() }) { return next }
            try await Task.sleep(nanoseconds: 10_000_000)
            return MacPollResponse()
        }
        func postEvents(nodeId: String, jobId: String, _ events: [MacJobEvent]) async throws -> MacEventsResponse {
            try JSONDecoder().decode(MacEventsResponse.self, from: Data(#"{"ok":true,"cancel":false}"#.utf8))
        }
        func postGrant(nodeId: String, _ body: MacGrantPost) async throws -> MacGrantResponse {
            try JSONDecoder().decode(MacGrantResponse.self, from: Data(#"{"ok":true,"told":true}"#.utf8))
        }
        func terminalSocket(nodeId: String, session: String) throws -> MacTerminalSocket {
            lock.withLock {
                let s = FakeSocket()
                sockets[session] = s
                return s
            }
        }
        func offer(_ session: String) {
            lock.withLock { script.append(MacPollResponse(terminal: MacTerminalOffer(session: session, cols: 100, rows: 30))) }
        }
        func socket(_ session: String) -> FakeSocket? { lock.withLock { sockets[session] } }
    }

    final class Shells: @unchecked Sendable {
        let lock = NSLock()
        var made: [FakeShell] = []
        func make(_ cols: Int, _ rows: Int) -> FakeShell {
            let s = FakeShell()
            lock.withLock { made.append(s) }
            return s
        }
        var count: Int { lock.withLock { made.count } }
        var last: FakeShell? { lock.withLock { made.last } }
    }

    final class States: @unchecked Sendable {
        let lock = NSLock()
        var last = MacBridgeState()
        func take(_ s: MacBridgeState) { lock.withLock { last = s } }
        var current: MacBridgeState { lock.withLock { last } }
    }

    var dir: String!
    var bridge: MacBridge?

    override func setUpWithError() throws {
        dir = NSTemporaryDirectory() + "mac-term-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        if let bridge { await bridge.stop() }
        bridge = nil
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func make(_ mode: MacMode, node: Node, shells: Shells, states: States,
                      idle: TimeInterval = MacTerminalSession.idleLimit) -> MacBridge {
        let settings = MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0", appVersion: "1.5.0")
        let b = MacBridge(client: node, executor: MacLocalExecutor(), policy: MacPolicy(config: MacPolicyConfig(mode: mode)),
                          activity: MacActivityLog(directory: dir), settings: settings,
                          makeShell: { c, r in shells.make(c, r) }, terminalIdle: idle,
                          sleep: { _ in try await Task.sleep(nanoseconds: 1_000_000) },
                          onState: { states.take($0) })
        bridge = b
        return b
    }

    private func eventually(_ what: String, timeout: TimeInterval = 5, file: StaticString = #filePath, line: UInt = #line,
                            _ cond: () async -> Bool) async {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if await cond() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("never happened: \(what)", file: file, line: line)
    }
    func testInAskModeTheViewerIsRefusedAndNoShellRuns() async {
        let node = Node(), shells = Shells(), states = States()
        node.offer("s-ask")
        await make(.ask, node: node, shells: shells, states: states).start()
        await eventually("the refusal") { node.socket("s-ask")?.texts.isEmpty == false }
        let text = node.socket("s-ask")?.texts.first ?? ""
        XCTAssertTrue(text.contains("full_access_required"), text)
        XCTAssertTrue(text.contains("Turn on Full access on your Mac"), text)
        await eventually("closed") { node.socket("s-ask")?.isClosed == true }
        XCTAssertEqual(shells.count, 0, "a shell ran outside Full access")
        XCTAssertNil(states.current.terminal)
    }

    func testInFullAccessItOpensShowsTheBannerAndLogsStartNotKeys() async {
        let node = Node(), shells = Shells(), states = States()
        node.offer("s-full")
        let b = make(.full, node: node, shells: shells, states: states)
        await b.start()
        await eventually("the shell") { shells.count == 1 }
        await eventually("the banner") { states.current.terminal != nil }
        XCTAssertEqual(states.current.terminal?.banner, "Terminal open from iPhone")
        let sock = node.socket("s-full")!
        sock.push(.bytes(Data("echo secret-password\r".utf8)))
        await eventually("the keys reach the shell") { shells.last?.input == "echo secret-password\r" }
        let rows = states.current.recent.filter { $0.kind == "terminal" }
        XCTAssertEqual(rows.map(\.summary), ["Terminal opened from iPhone"])
        XCTAssertFalse(states.current.recent.contains { $0.summary.contains("secret-password") })
    }

    // MARK: Stop

    func testStopKillsTheShellClosesTheViewerAndLogsIt() async {
        let node = Node(), shells = Shells(), states = States()
        node.offer("s-stop")
        let b = make(.full, node: node, shells: shells, states: states)
        await b.start()
        await eventually("open") { states.current.terminal != nil }
        await b.stopTerminal(.stopped)
        await eventually("the shell is killed") { shells.last?.endedWith == true }
        await eventually("the banner goes") { states.current.terminal == nil }
        let sock = node.socket("s-stop")!
        XCTAssertTrue(sock.isClosed)
        XCTAssertTrue(sock.texts.contains { $0.contains("\"stopped\"") }, "\(sock.texts)")
        XCTAssertTrue(states.current.recent.contains { $0.summary == "Terminal from iPhone closed: stopped on this Mac" })
    }

    func testLeavingFullAccessKillsAnOpenTerminal() async {
        let node = Node(), shells = Shells(), states = States()
        node.offer("s-mode")
        let b = make(.full, node: node, shells: shells, states: states)
        await b.start()
        await eventually("open") { states.current.terminal != nil }
        await b.setMode(.ask)
        await eventually("killed") { shells.last?.endedWith == true }
        XCTAssertTrue(node.socket("s-mode")!.texts.contains { $0.contains("full_access_required") })
    }

    func testTheViewerLeavingDetachesButDoesNotKill() async {
        let node = Node(), shells = Shells(), states = States()
        node.offer("s-left")
        let b = make(.full, node: node, shells: shells, states: states)
        await b.start()
        await eventually("open") { states.current.terminal != nil }
        node.socket("s-left")!.close()
        await eventually("ended") { shells.last?.endedWith != nil }
        XCTAssertEqual(shells.last?.endedWith, false, "a tmux shell must survive a dropped viewer")
    }

    func testTheMacDialsOutWithItsNodeSecretOverWebSocket() throws {
        let tokens = InMemoryTokenStore(token: "tok"), secrets = InMemoryTokenStore(token: "mac_aaaaaaaaaaaa.sec")
        let c = MacNodeClient(deckURL: URL(string: "http://10.0.0.1:7789")!, tokens: tokens, secrets: secrets,
                              performer: URLSessionPerformer(session: .shared))
        let r = try c.terminalRequest(nodeId: "mac_aaaaaaaaaaaa", session: "abc")
        XCTAssertEqual(r.url?.absoluteString, "ws://10.0.0.1:7789/v1/nodes/mac_aaaaaaaaaaaa/terminal/mac?session=abc")
        XCTAssertEqual(r.value(forHTTPHeaderField: "X-Deck-Node"), "mac_aaaaaaaaaaaa.sec")
        XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
    }
    @MainActor
    func testTheHotkeyIsArmedWhileATerminalIsOpenAndStopAllKillsIt() async throws {
        let node = Node(), shells = Shells()
        node.offer("s-host")
        let dir = NSTemporaryDirectory() + "mac-host-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let relay = MacBridgeHost.Relay()
        let config = MacPolicyConfig(mode: .full)
        let bridge = MacBridge(client: node, executor: MacLocalExecutor(), policy: MacPolicy(config: config),
                               activity: MacActivityLog(directory: dir),
                               settings: MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0",
                                                           appVersion: "1.0.0"),
                               makeShell: { c, r in shells.make(c, r) },
                               onState: { relay.send($0) })
        let host = MacBridgeHost(bridge: bridge, relay: relay, config: config, macName: "Test Mac", deckHost: "10.99.0.1")
        host.start()
        defer { Task { await host.stop() } }
        let end = Date().addingTimeInterval(5)
        while host.state.terminal == nil, Date() < end { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertEqual(host.state.terminal?.banner, "Terminal open from iPhone")
        XCTAssertTrue(host.hotkeyArmed, "⌃⌥⌘. must be armed while his terminal is open")
        host.stopAll()   // what ⌃⌥⌘. and the banner's Stop call
        let gone = Date().addingTimeInterval(5)
        while host.state.terminal != nil || shells.last?.endedWith != true, Date() < gone {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(shells.last?.endedWith, true, "Stop did not kill the shell")
        XCTAssertNil(host.state.terminal)
        XCTAssertFalse(host.hotkeyArmed)
    }
}
