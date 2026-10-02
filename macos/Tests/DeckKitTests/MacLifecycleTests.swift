import XCTest
@testable import DeckKit

/// **A4: the app actually runs the Mac bridge.**
///
/// Measured 2026-09-30: every MacBridge part was built and tested, and nothing
/// started it — the installed app never registered with the deck, so no desk
/// could ever use this Mac. These pin the host the app shell starts: only the
/// direct build carries it, his card is what a desk's request becomes, his
/// tap is what lets it run, and a deny means the shell is never started.
///
/// Fakes only (MacBridgeTests'): no socket, no shell, no banner, no sound.
@MainActor
final class MacLifecycleTests: XCTestCase {
    typealias Node = MacBridgeTests.FakeNode
    typealias Exec = MacBridgeTests.FakeExecutor

    var dir: String!
    var host: MacBridgeHost?

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-lifecycle-\(UUID().uuidString)"
        for d in [dir + "/home/work", dir + "/tmp", dir + "/support"] {
            try FileManager.default.createDirectory(atPath: d, withIntermediateDirectories: true)
        }
    }

    override func tearDown() async throws {
        if let host { await host.stop() }
        host = nil
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func make(_ config: MacPolicyConfig, node: Node, exec: Exec) -> MacBridgeHost {
        var settings = MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0", appVersion: "1.0.0")
        settings.keepalive = 0.05
        settings.tick = 0.005
        settings.grantWait = 5
        settings.stopGrace = 2
        let relay = MacBridgeHost.Relay()
        let policy = MacPolicy(config: config, paths: MacPaths(home: dir + "/home"), seatbeltAvailable: true,
                               tempDirs: [dir + "/tmp"])
        let bridge = MacBridge(client: node, executor: exec, policy: policy,
                               policyStore: MacPolicyStore(url: URL(fileURLWithPath: dir + "/support/mac-policy.json")),
                               activity: MacActivityLog(directory: dir + "/support"), settings: settings,
                               onState: { relay.send($0) })
        let h = MacBridgeHost(bridge: bridge, relay: relay, config: config, macName: "Test Mac", deckHost: "10.99.0.1")
        host = h
        return h
    }

    private func eventually(_ what: String, timeout: TimeInterval = 5, file: StaticString = #filePath, line: UInt = #line,
                            _ cond: () -> Bool) async {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if cond() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("never happened: \(what)", file: file, line: line)
    }

    private func swVers(_ id: String) -> MacWireJob {
        MacWireJob(id: id, desk: "wake-probe", kind: .run, args: MacJobArgs(command: "sw_vers"), timeoutS: 120)
    }

    // MARK: which build

    func testOnlyTheDirectBuildCarriesTheBridge() {
        XCTAssertTrue(MacBridgeHost.carriesBridge(.direct))
        XCTAssertFalse(MacBridgeHost.carriesBridge(.appStore))
    }

    func testTheAppStoreBuildNeverMakesAHost() {
        XCTAssertNil(MacBridgeHost.live(channel: .appStore))
    }

    func testNoDeckYetMeansNoHost() {
        let suite = StateSuite(environment: ["DECK_STATE_SUITE": "mac-lifecycle-\(UUID().uuidString.prefix(8))"])
        XCTAssertNil(MacBridgeHost.live(channel: .direct, suite: suite))
    }

    // MARK: the request, his card, his tap

    func testStartRegistersAndPollsSoTheDeckSeesThisMac() async {
        let node = Node()
        let h = make(MacPolicyConfig(mode: .ask), node: node, exec: Exec())
        h.start()
        await eventually("registered and polling") { node.registerCount == 1 && node.pollCount >= 1 }
        await eventually("online on the host") { h.state.connection == .online }
    }

    func testADesksRunBecomesHisCardAndRunsOnlyAfterHeAllows() async {
        let node = Node(), exec = Exec()
        exec.script = { run, output in
            output(.stdout, Data("ProductName:\tmacOS\n".utf8))
            run.finish(.done, exit: 0)
        }
        node.script(.success(MacPollResponse(jobs: [swVers("mj_0000000000a1")])))
        let h = make(MacPolicyConfig(mode: .ask), node: node, exec: exec)
        h.start()
        await eventually("the card is up") { h.state.pendingGrants.map(\.desk) == ["wake-probe"] }
        XCTAssertEqual(h.state.pendingGrants.first?.summary, "sw_vers")
        XCTAssertEqual(exec.startedCount, 0, "nothing runs before he taps")

        h.answer(desk: "wake-probe", .hour)
        await eventually("ran") { node.result("mj_0000000000a1")?.state == .done }
        XCTAssertEqual(exec.startedCount, 1)
        XCTAssertEqual(node.stdout("mj_0000000000a1"), "ProductName:\tmacOS\n")
        await eventually("card gone") { h.state.pendingGrants.isEmpty }
    }

    func testDenyMeansTheShellIsNeverStarted() async {
        let node = Node(), exec = Exec()
        node.script(.success(MacPollResponse(jobs: [swVers("mj_0000000000a2")])))
        let h = make(MacPolicyConfig(mode: .ask), node: node, exec: exec)
        h.start()
        await eventually("the card is up") { !h.state.pendingGrants.isEmpty }
        h.answer(desk: "wake-probe", .deny)
        await eventually("refused") { node.result("mj_0000000000a2")?.reason == "denied" }
        XCTAssertEqual(exec.startedCount, 0)
    }

    func testPauseAndResumeComeBackToTheModeHeHad() async {
        let node = Node()
        let h = make(MacPolicyConfig(mode: .ask), node: node, exec: Exec())
        h.start()
        h.pause()
        XCTAssertEqual(h.config.mode, .paused)
        h.resume()
        XCTAssertEqual(h.config.mode, .ask)
        XCTAssertTrue(h.keepsAppAlive)
        h.setMode(.off)
        XCTAssertFalse(h.keepsAppAlive, "Off: closing the window quits, as before")
    }

    // MARK: the shell starts it

    func testTheAppShellStartsTheHostOnTheDirectBuildOnly() throws {
        let main = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/DeckApp/DeckAppMain.swift")
        let text = try String(contentsOf: main, encoding: .utf8)
        XCTAssertTrue(text.contains("MacBridgeHost.live("), "the app never makes the bridge")
        XCTAssertTrue(text.contains(".start()"), "the app never starts the bridge")
        XCTAssertTrue(text.contains("DECK_APPSTORE"), "the App Store build must not carry the bridge")
        XCTAssertTrue(text.contains("MacGrantCardView"), "his card is never on screen")
    }
}
