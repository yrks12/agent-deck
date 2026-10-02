import XCTest
@testable import DeckKit

/// **Mac control in the bridge loop (owner, 2026-10-01).**
///
/// What goes wrong without these: an `input` job this build does not know is
/// refused as "update Agent Deck"; the deck never learns he turned control on,
/// so every desk keeps getting `control_off`; a viewer opens the live view and
/// nothing is ever captured (or capture runs while nobody looks); a desk's
/// click lands with no line in the activity log; the waiting asks never reach
/// his Mac, so there is nothing to tap Allow on.
///
/// Fake node client, fake input, fake screen: never the owner's real mouse.
final class MacBridgeControlTests: XCTestCase {
    typealias FakeNode = MacBridgeTests.FakeNode

    final class FakeInput: MacInputPerforming, @unchecked Sendable {
        let lock = NSLock()
        var done: [MacInputGesture] = []
        var refuse: MacOpError?
        func perform(_ gesture: MacInputGesture) async throws {
            try lock.withLock {
                if let refuse { throw refuse }
                done.append(gesture)
            }
        }
        var gestures: [MacInputGesture] { lock.withLock { done } }
    }

    final class FakeScreen: MacScreenStreaming, @unchecked Sendable {
        let lock = NSLock()
        var started: [String] = []
        var stops = 0
        func start(nodeId: String) async { lock.withLock { started.append(nodeId) } }
        func stop() async { lock.withLock { stops += 1 } }
        var starts: [String] { lock.withLock { started } }
        var stopCount: Int { lock.withLock { stops } }
    }

    final class States: @unchecked Sendable {
        let lock = NSLock()
        var last = MacBridgeState()
        func take(_ s: MacBridgeState) { lock.withLock { last = s } }
        var current: MacBridgeState { lock.withLock { last } }
    }

    var dir: String!
    var node: FakeNode!
    var input: FakeInput!
    var screen: FakeScreen!
    var states: States!
    var log: MacActivityLog!
    var bridge: MacBridge?

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-control-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir + "/support", withIntermediateDirectories: true)
        node = FakeNode()
        input = FakeInput()
        screen = FakeScreen()
        states = States()
        log = MacActivityLog(directory: dir + "/support")
    }

    override func tearDown() async throws {
        if let bridge { await bridge.stop() }
        bridge = nil
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func make(mode: MacMode = .ask) -> MacBridge {
        var s = MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0", appVersion: "2.0")
        s.keepalive = 0.05
        s.tick = 0.005
        let policy = MacPolicy(config: MacPolicyConfig(mode: mode), paths: MacPaths(home: dir),
                               seatbeltAvailable: true, tempDirs: [])
        let states = self.states!
        let b = MacBridge(client: node, executor: MacBridgeTests.FakeExecutor(), policy: policy, activity: log,
                          settings: s, input: input, screen: screen,
                          probe: { MacControlProbe(perms: MacPermissions(accessibility: true, screenRecording: false),
                                                   screen: MacSpace(width: 1512, height: 982)) },
                          sleep: { _ in try await Task.sleep(nanoseconds: 1_000_000) },
                          onState: { states.take($0) })
        bridge = b
        return b
    }

    private func eventually(_ what: String, timeout: TimeInterval = 5, file: StaticString = #filePath,
                            line: UInt = #line, _ cond: () async -> Bool) async {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if await cond() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("never happened: \(what)", file: file, line: line)
    }

    private func click(_ id: String, desk: String = "atlas") -> MacWireJob {
        MacWireJob(id: id, desk: desk, kind: .input,
                   args: MacJobArgs(action: "click", x: 3, y: 4, button: "left", count: 1,
                                    space: MacSpace(width: 1512, height: 982)), timeoutS: 30)
    }

    func testAMacThatCanControlOffersInputAndScreenshots() async throws {
        let b = make()
        await b.start()
        await eventually("registered") { node.registerCount >= 1 }
        let reg = try XCTUnwrap(node.lock.withLock { node.registers.first })
        XCTAssertTrue(reg.capabilities.contains(.input))
        XCTAssertTrue(reg.capabilities.contains(.screenshot))
    }

    func testThePollCarriesTheGrantAndTheProbe() async throws {
        let b = make()
        await b.start()
        await eventually("polled") { node.pollCount >= 1 }
        XCTAssertNil(node.lastPoll?.control)
        XCTAssertEqual(node.lastPoll?.perms, MacPermissions(accessibility: true, screenRecording: false))
        XCTAssertEqual(node.lastPoll?.screen, MacSpace(width: 1512, height: 982))
        let grant = MacControlGrant.start(scope: "atlas", now: Date().timeIntervalSince1970)
        await b.setControl(grant)
        await eventually("the deck hears the grant") { node.lastPoll?.control == grant.wire }
        await b.setControl(nil)
        await eventually("the deck hears it end") { node.lastPoll?.control == nil }
        XCTAssertGreaterThanOrEqual(screen.stopCount, 1, "ending control stops any capture")
    }

    func testAnInputJobUnderTheGrantIsPerformedAndLogged() async throws {
        let b = make()
        await b.setControl(.start(scope: "atlas", now: Date().timeIntervalSince1970))
        node.script(.success(MacPollResponse(jobs: [click("mj_c1")])))
        await b.start()
        await eventually("a result") { node.result("mj_c1") != nil }
        XCTAssertEqual(node.result("mj_c1")?.state, .done)
        XCTAssertEqual(input.gestures, [.click(x: 3, y: 4, button: .left, count: 1,
                                               space: MacSpace(width: 1512, height: 982))])
        await eventually("logged") { log.recent().contains { $0.jobId == "mj_c1" && $0.summary == "click 3,4" } }
        XCTAssertEqual(states.current.lastControl?.desk, "atlas")
    }

    func testAnInputJobWithoutTheGrantIsControlOffAndTouchesNothing() async throws {
        let b = make()
        node.script(.success(MacPollResponse(jobs: [click("mj_c2")])))
        await b.start()
        await eventually("a result") { node.result("mj_c2") != nil }
        XCTAssertEqual(node.result("mj_c2")?.state, .refused)
        XCTAssertEqual(node.result("mj_c2")?.reason, "control_off")
        XCTAssertEqual(input.gestures, [])
    }

    func testHisMouseWinsAndTheReasonReachesTheDesk() async throws {
        input.refuse = MacOpError("owner_active", MacControlCopy.ownerActive)
        let b = make()
        await b.setControl(.start(scope: nil, now: Date().timeIntervalSince1970))
        node.script(.success(MacPollResponse(jobs: [click("mj_c3")])))
        await b.start()
        await eventually("a result") { node.result("mj_c3") != nil }
        XCTAssertEqual(node.result("mj_c3")?.state, .refused)
        XCTAssertEqual(node.result("mj_c3")?.reason, "owner_active")
    }

    func testSomeoneWatchingStartsCaptureOnlyUnderAGrant() async throws {
        let b = make()
        node.script(.success(MacPollResponse(watch: true)))
        await b.start()
        await eventually("polled twice") { node.pollCount >= 2 }
        XCTAssertEqual(screen.starts, [], "no grant: nothing is captured")
        await b.setControl(.start(scope: nil, now: Date().timeIntervalSince1970))
        node.script(.success(MacPollResponse(watch: true)))
        await eventually("capture starts") { screen.starts == ["mac_aaaaaaaaaaaa"] }
    }

    func testTheWaitingAsksReachHisMac() async throws {
        let b = make()
        node.script(.success(MacPollResponse(controlAsks: ["atlas"])))
        await b.start()
        await eventually("the ask is shown") { states.current.controlAsks == ["atlas"] }
    }
}
