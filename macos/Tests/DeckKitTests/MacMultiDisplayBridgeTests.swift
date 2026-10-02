import ImageIO
import XCTest
@testable import DeckKit

/// **The bridge serves every display, not just the main one.**
///
/// MEASURED 2026-10-02: with a second monitor on his Mac, the poll reported
/// one `screen`, the live view captured only the main display and the agent's
/// screenshot ran a bare `screencapture` (main only), so a window on the
/// second display could be neither seen nor clicked.
///
/// Fake node, fake executor, fake screen: never his real displays.
final class MacMultiDisplayBridgeTests: XCTestCase {
    typealias FakeNode = MacBridgeTests.FakeNode

    static let builtIn = MacDisplayInfo(id: 1, name: "Built-in Retina Display", widthPx: 3024, heightPx: 1964,
                                        scale: 2, originX: 0, originY: 0, isMain: true)
    static let monitor = MacDisplayInfo(id: 3, name: "PM1561P", widthPx: 1920, heightPx: 1080,
                                        scale: 1, originX: -1920, originY: -100, isMain: false)

    var dir: String!
    var node: FakeNode!
    var exec: MacBridgeTests.FakeExecutor!
    var screen: MacBridgeControlTests.FakeScreen!
    var bridge: MacBridge?

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-multi-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir + "/support", withIntermediateDirectories: true)
        node = FakeNode()
        exec = MacBridgeTests.FakeExecutor()
        screen = MacBridgeControlTests.FakeScreen()
    }

    override func tearDown() async throws {
        if let bridge { await bridge.stop() }
        bridge = nil
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func make() -> MacBridge {
        var s = MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0", appVersion: "2.0")
        s.keepalive = 0.05
        s.tick = 0.005
        let policy = MacPolicy(config: MacPolicyConfig(mode: .full), paths: MacPaths(home: dir),
                               seatbeltAvailable: true, tempDirs: [])
        let b = MacBridge(client: node, executor: exec, policy: policy,
                          activity: MacActivityLog(directory: dir + "/support"), settings: s,
                          input: MacBridgeControlTests.FakeInput(), screen: screen,
                          probe: { MacControlProbe(perms: MacPermissions(accessibility: true, screenRecording: true),
                                                   screen: MacSpace(width: 1512, height: 982),
                                                   displays: [Self.builtIn, Self.monitor]) },
                          sleep: { _ in try await Task.sleep(nanoseconds: 1_000_000) },
                          onState: { _ in })
        bridge = b
        return b
    }

    private func eventually(_ what: String, file: StaticString = #filePath, line: UInt = #line,
                            _ cond: () async -> Bool) async {
        let end = Date().addingTimeInterval(5)
        while Date() < end {
            if await cond() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("never happened: \(what)", file: file, line: line)
    }

    func testThePollReportsEveryDisplay() async throws {
        await make().start()
        await eventually("polled") { node.lastPoll?.displays != nil }
        XCTAssertEqual(node.lastPoll?.displays, [Self.builtIn, Self.monitor])
        XCTAssertEqual(node.lastPoll?.screen, MacSpace(width: 1512, height: 982), "screen stays the main display")
    }

    func testTheWatchedDisplayIsTheOneCaptured() async throws {
        node.script(.success(MacPollResponse(watch: true, watchDisplays: [3])))
        await make().start()
        await eventually("capture of display 3") { screen.displays.first == [3] }
    }

    func testAScreenshotOfTheSecondDisplayCapturesItsRectangle() async throws {
        exec.script = { run, _ in
            let path = run.spec.command.components(separatedBy: " ").last!
                .trimmingCharacters(in: CharacterSet(charactersIn: "'"))
            try? Self.jpeg(width: 48, height: 27).write(to: URL(fileURLWithPath: path))
            run.finish(.done, exit: 0)
        }
        node.script(.success(MacPollResponse(jobs: [MacWireJob(id: "mj_000000000301", desk: "atlas",
                                                               kind: .screenshot, args: MacJobArgs(display: 3))])))
        await make().start()
        await eventually("shot") { node.result("mj_000000000301") != nil }
        let spec = try XCTUnwrap(exec.spec("mj_000000000301"))
        XCTAssertTrue(spec.command.hasPrefix("/usr/sbin/screencapture -x -t jpg -R -1920,-100,1920,1080 "),
                      spec.command)
        let r = try XCTUnwrap(node.result("mj_000000000301"))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.payload["display"], 3)
    }

    func testAScreenshotOfAGoneDisplayIsRefusedWithTheDisplaysThereAre() async throws {
        node.script(.success(MacPollResponse(jobs: [MacWireJob(id: "mj_000000000302", desk: "atlas",
                                                               kind: .screenshot, args: MacJobArgs(display: 9))])))
        await make().start()
        await eventually("refused") { node.result("mj_000000000302") != nil }
        let r = try XCTUnwrap(node.result("mj_000000000302"))
        XCTAssertEqual(r.reason, "no_such_display")
        XCTAssertTrue(r.detail.contains("Display 2 · PM1561P"), r.detail)
        XCTAssertNil(exec.spec("mj_000000000302"), "nothing is captured")
    }

    static func jpeg(width: Int, height: Int) -> Data {
        let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.2, green: 0.4, blue: 0.6, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let out = NSMutableData()
        let dst = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil)!
        CGImageDestinationAddImage(dst, ctx.makeImage()!, nil)
        CGImageDestinationFinalize(dst)
        return out as Data
    }

    func testTheMainDisplayScreenshotIsUnchanged() {
        XCTAssertEqual(MacBridge.screenshotCommand(file: "/tmp/a.jpg", display: nil),
                       "/usr/sbin/screencapture -x -t jpg '/tmp/a.jpg'")
    }
}
