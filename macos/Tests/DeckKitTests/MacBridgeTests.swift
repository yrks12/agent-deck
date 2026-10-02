import CoreGraphics
import ImageIO
import XCTest
@testable import DeckKit

/// **A-5: the loop that lets a desk on the box use this Mac.**
///
/// What goes wrong without these: a job that never reports (`lost` after
/// 45 s while it is still running here); a fifth job started when the deck
/// counted four; a dead deck hammered in a hot loop; "Pause" that keeps
/// polling, so desks still see the Mac online; Quit that leaves a `sleep 300`
/// running with nothing to report it; a grant card that parks a job past the
/// server's patience; a cancel from the desk that never reaches the process.
///
/// Everything here runs against a fake node client and a fake executor — no
/// socket, no shell, no microphone and never the owner's screen.
final class MacBridgeTests: XCTestCase {

    // MARK: fakes

    final class FakeNode: MacNodeClienting, @unchecked Sendable {
        let deckURL: URL
        let lock = NSLock()
        var registers: [MacRegisterRequest] = []
        var polls: [MacPollRequest] = []
        var pollScript: [Result<MacPollResponse, Error>] = []
        var registerErrors: [Error] = []
        var batches: [String: [[MacJobEvent]]] = [:]
        var cancelOnEvents: Set<String> = []
        var goneOnEvents: Set<String> = []
        var grants: [MacGrantPost] = []
        var idlePoll: TimeInterval = 0.01

        init(deckURL: URL = URL(string: "http://10.99.0.1:7789")!) { self.deckURL = deckURL }

        func script(_ items: Result<MacPollResponse, Error>...) { lock.withLock { pollScript += items } }

        func register(_ body: MacRegisterRequest) async throws -> MacRegisterResponse {
            let err = lock.withLock { () -> Error? in
                registers.append(body)
                return registerErrors.isEmpty ? nil : registerErrors.removeFirst()
            }
            if let err { throw err }
            return MacRegisterResponse(nodeId: "mac_aaaaaaaaaaaa", nodeSecret: nil, name: body.name, primary: true)
        }

        func poll(nodeId: String, _ body: MacPollRequest) async throws -> MacPollResponse {
            let next = lock.withLock { () -> Result<MacPollResponse, Error>? in
                polls.append(body)
                return pollScript.isEmpty ? nil : pollScript.removeFirst()
            }
            if let next { return try next.get() }
            try await Task.sleep(nanoseconds: UInt64(idlePoll * 1e9))   // a long-poll that found nothing
            return MacPollResponse()
        }

        func postEvents(nodeId: String, jobId: String, _ events: [MacJobEvent]) async throws -> MacEventsResponse {
            try lock.withLock {
                batches[jobId, default: []].append(events)
                if goneOnEvents.contains(jobId) {
                    throw MacNodeError.refused(status: 409, reason: "job_settled", detail: "job is already lost")
                }
                return MacEventsResponse(ok: true, cancel: cancelOnEvents.contains(jobId))
            }
        }

        func postGrant(nodeId: String, _ body: MacGrantPost) async throws -> MacGrantResponse {
            lock.withLock { grants.append(body) }
            return try JSONDecoder().decode(MacGrantResponse.self, from: Data(#"{"ok":true,"told":true}"#.utf8))
        }

        var pollCount: Int { lock.withLock { polls.count } }
        var lastPoll: MacPollRequest? { lock.withLock { polls.last } }
        var registerCount: Int { lock.withLock { registers.count } }
        var grantPosts: [MacGrantPost] { lock.withLock { grants } }
        func events(_ id: String) -> [MacJobEvent] { lock.withLock { (batches[id] ?? []).flatMap { $0 } } }
        func keepalives(_ id: String) -> Int { lock.withLock { (batches[id] ?? []).filter(\.isEmpty).count } }
        func result(_ id: String) -> MacJobResultData? {
            for e in events(id) { if case .result(let r) = e.body { return r } }
            return nil
        }
        func stdout(_ id: String) -> String {
            events(id).compactMap { if case .stdout(let s) = $0.body { return s } else { return nil } }.joined()
        }
    }

    final class FakeRun: MacJobRun, @unchecked Sendable {
        let spec: MacRunSpec
        let pid: Int32 = 4242
        let lock = NSLock()
        private var settled: MacRunResult?
        private var callbacks: [@Sendable (MacRunResult) -> Void] = []
        private(set) var cancelCount = 0
        var sandboxed: Bool { spec.sandboxProfile != nil }
        var cwd: String { spec.cwd ?? "/tmp" }

        init(_ spec: MacRunSpec) { self.spec = spec }

        func cancel() {
            lock.withLock { cancelCount += 1 }
            finish(.cancelled, exit: nil, signal: "TERM")
        }

        func finish(_ state: MacRunResult.State, exit: Int32?, signal: String? = nil, reason: String? = nil) {
            let (r, cbs) = lock.withLock { () -> (MacRunResult?, [@Sendable (MacRunResult) -> Void]) in
                guard settled == nil else { return (nil, []) }
                settled = MacRunResult(state: state, exit: exit, signal: signal, reason: reason, detail: "", durationMs: 7,
                                       sandboxed: spec.sandboxProfile != nil, pid: pid, cwd: cwd,
                                       stdoutBytes: 3, stderrBytes: 0, truncated: false)
                defer { callbacks = [] }
                return (settled, callbacks)
            }
            if let r { for cb in cbs { DispatchQueue.global().async { cb(r) } } }
        }

        func wait() -> MacRunResult {
            while true {
                if let r = lock.withLock({ settled }) { return r }
                usleep(1000)
            }
        }

        func onFinish(_ body: @escaping @Sendable (MacRunResult) -> Void) {
            let r = lock.withLock { () -> MacRunResult? in
                if settled == nil { callbacks.append(body) }
                return settled
            }
            if let r { DispatchQueue.global().async { body(r) } }
        }

        var isSettled: Bool { lock.withLock { settled != nil } }
        var wasCancelled: Bool { lock.withLock { cancelCount > 0 } }
    }

    final class FakeExecutor: MacJobExecuting, @unchecked Sendable {
        let lock = NSLock()
        var specs: [MacRunSpec] = []
        var runs: [String: FakeRun] = [:]
        var active = 0
        var maxActive = 0
        /// Default: the job blocks until it is cancelled or the test ends it.
        var script: @Sendable (FakeRun, @escaping @Sendable (MacStream, Data) -> Void) -> Void = { _, _ in }

        func start(_ spec: MacRunSpec, output: @escaping @Sendable (MacStream, Data) -> Void) -> any MacJobRun {
            let run = FakeRun(spec)
            let body = lock.withLock { () -> @Sendable (FakeRun, @escaping @Sendable (MacStream, Data) -> Void) -> Void in
                specs.append(spec)
                runs[spec.jobId] = run
                active += 1
                maxActive = max(maxActive, active)
                return script
            }
            run.onFinish { [weak self] _ in self?.lock.withLock { self?.active -= 1 } }
            body(run, output)
            return run
        }

        func run(_ id: String) -> FakeRun? { lock.withLock { runs[id] } }
        var startedCount: Int { lock.withLock { specs.count } }
        var peak: Int { lock.withLock { maxActive } }
        func spec(_ id: String) -> MacRunSpec? { lock.withLock { specs.first { $0.jobId == id } } }
    }

    final class Sleeps: @unchecked Sendable {
        let lock = NSLock()
        var delays: [Double] = []
        func take(_ d: Double) { lock.withLock { delays.append(d) } }
        var all: [Double] { lock.withLock { delays } }
    }

    final class States: @unchecked Sendable {
        let lock = NSLock()
        var last = MacBridgeState()
        func take(_ s: MacBridgeState) { lock.withLock { last = s } }
        var current: MacBridgeState { lock.withLock { last } }
    }

    // MARK: fixture

    var dir: String!
    var home: String!
    var node: FakeNode!
    var exec: FakeExecutor!
    var sleeps: Sleeps!
    var states: States!
    var log: MacActivityLog!
    var bridge: MacBridge?

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-bridge-\(UUID().uuidString)"
        home = dir + "/home"
        for d in [home + "/work", dir + "/tmp", dir + "/support"] {
            try FileManager.default.createDirectory(atPath: d, withIntermediateDirectories: true)
        }
        node = FakeNode()
        exec = FakeExecutor()
        sleeps = Sleeps()
        states = States()
        log = MacActivityLog(directory: dir + "/support")
    }

    override func tearDown() async throws {
        if let bridge { await bridge.stop() }
        bridge = nil
        try? FileManager.default.removeItem(atPath: dir)
    }

    private func settings(_ tweak: (inout MacBridgeSettings) -> Void = { _ in }) -> MacBridgeSettings {
        var s = MacBridgeSettings(machineId: "M-1", name: "Test Mac", os: "macOS 26.0", appVersion: "1.5.0")
        s.keepalive = 0.05
        s.tick = 0.005
        s.grantWait = 5
        s.stopGrace = 2
        tweak(&s)
        return s
    }

    private func make(_ config: MacPolicyConfig, client: FakeNode? = nil,
                      _ tweak: (inout MacBridgeSettings) -> Void = { _ in }) -> MacBridge {
        let policy = MacPolicy(config: config, paths: MacPaths(home: home), seatbeltAvailable: true, tempDirs: [dir + "/tmp"])
        let sleeps = self.sleeps!, states = self.states!
        let b = MacBridge(client: client ?? node, executor: exec, policy: policy,
                          policyStore: MacPolicyStore(url: URL(fileURLWithPath: dir + "/support/mac-policy.json")),
                          activity: log, settings: settings(tweak),
                          sleep: { d in sleeps.take(d); try await Task.sleep(nanoseconds: 1_000_000) },
                          onState: { states.take($0) })
        bridge = b
        return b
    }

    private func full(_ tweak: (inout MacPolicyConfig) -> Void = { _ in }) -> MacPolicyConfig {
        var c = MacPolicyConfig(mode: .full)
        tweak(&c)
        return c
    }

    private func runJob(_ id: String, desk: String = "atlas", _ command: String = "sw_vers") -> MacWireJob {
        MacWireJob(id: id, desk: desk, kind: .run, args: MacJobArgs(command: command), timeoutS: 120)
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

    private func pause(_ seconds: TimeInterval) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)) }

    // MARK: register + poll

    func testRegistersOnceThenLongPollsWithSlotsModeAndGrants() async throws {
        let b = make(full { $0.grants = ["atlas": MacGrant(state: .always, until: nil)] })
        await b.start()
        await eventually("two polls") { node.pollCount >= 2 }
        XCTAssertEqual(node.registerCount, 1)
        let reg = try XCTUnwrap(node.lock.withLock { node.registers.first })
        XCTAssertEqual(reg.machineId, "M-1")
        XCTAssertEqual(reg.name, "Test Mac")
        XCTAssertEqual(reg.mode, .full)
        // Open on, screenshots off by default: the deck must not offer what this Mac refuses.
        XCTAssertEqual(reg.capabilities, [.run, .read, .write, .list, .open])
        let poll = try XCTUnwrap(node.lastPoll)
        XCTAssertEqual(poll.wait, 25)
        XCTAssertEqual(poll.freeSlots, 4)
        XCTAssertEqual(poll.running, [])
        XCTAssertEqual(poll.mode, .full)
        XCTAssertEqual(poll.grants, ["atlas": MacWireGrant(state: .always, until: nil)])
        XCTAssertEqual(states.current.connection, .online)
    }

    func testOffNeverRegistersOrPolls() async {
        let b = make(MacPolicyConfig(mode: .off))
        await b.start()
        await pause(0.1)
        XCTAssertEqual(node.registerCount, 0)
        XCTAssertEqual(node.pollCount, 0)
        XCTAssertEqual(states.current.connection, .off)
    }

    /// A-6 at the loop: plain HTTP to a public host never dials out at all.
    func testTransportGuardKeepsThePlainHTTPPublicDeckOff() async {
        let b = make(full(), client: FakeNode(deckURL: URL(string: "http://1.2.3.4:7789")!))
        await b.start()
        await pause(0.1)
        XCTAssertEqual(node.registerCount, 0)
        XCTAssertEqual(states.current.connection, .refused(MacTransport.refusal))
    }

    // MARK: A-5 seq

    func testARunJobReportsStartedOutputResultWithMonotonicSeq() async throws {
        exec.script = { run, out in
            DispatchQueue.global().async {
                out(.stdout, Data("Product".utf8))
                out(.stdout, Data("Version:\t26.0\n".utf8))
                out(.stderr, Data("warn\n".utf8))
                run.finish(.done, exit: 0)
            }
        }
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000001")])))
        let b = make(full())
        await b.start()
        await eventually("result posted") { node.result("mj_000000000001") != nil }

        let events = node.events("mj_000000000001")
        let seqs = events.map(\.seq)
        XCTAssertEqual(seqs.first, 1)
        XCTAssertEqual(seqs, seqs.sorted())
        XCTAssertEqual(Set(seqs).count, seqs.count, "no seq is ever sent twice as two different events")
        XCTAssertEqual(events.first?.type, "started")
        XCTAssertEqual(events.last?.type, "result")
        if case .started(let s) = events[0].body {
            XCTAssertEqual(s.pid, 4242)
            XCTAssertFalse(s.sandboxed, "Full access runs unconfined")
        }
        XCTAssertEqual(node.stdout("mj_000000000001"), "ProductVersion:\t26.0\n")
        let r = try XCTUnwrap(node.result("mj_000000000001"))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.exit, 0)

        let spec = try XCTUnwrap(exec.spec("mj_000000000001"))
        XCTAssertEqual(spec.command, "sw_vers")
        XCTAssertEqual(spec.desk, "atlas")
        XCTAssertEqual(spec.timeout, 120)
        XCTAssertNil(spec.sandboxProfile)

        await eventually("activity row") { log.recent().contains { $0.jobId == "mj_000000000001" && $0.decision == .ran } }
        let row = try XCTUnwrap(log.recent().first { $0.jobId == "mj_000000000001" })
        XCTAssertEqual(row.state, "done")
        XCTAssertEqual(row.exit, 0)
        await eventually("in-use bar clears") { states.current.running.isEmpty }
        XCTAssertEqual(states.current.recent.first?.jobId, "mj_000000000001")
    }

    func testLongOutputIsChunkedWithinTheServersLimit() async {
        let big = String(repeating: "a", count: 150_000)
        exec.script = { run, out in
            DispatchQueue.global().async { out(.stdout, Data(big.utf8)); run.finish(.done, exit: 0) }
        }
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000002")])))
        await make(full()).start()
        await eventually("result") { node.result("mj_000000000002") != nil }
        let chunks = node.events("mj_000000000002").compactMap { e -> String? in
            if case .stdout(let s) = e.body { return s } else { return nil }
        }
        XCTAssertGreaterThanOrEqual(chunks.count, 3)
        XCTAssertTrue(chunks.allSatisfy { $0.unicodeScalars.count <= MacWire.chunkMax })
        XCTAssertEqual(chunks.joined(), big)
    }

    /// The server marks a silent job `lost` after 45 s; the Mac says it is
    /// alive at least every 2 s (here: 50 ms) with an empty events list.
    func testASilentJobIsKeptAliveWithEmptyEventBatches() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000003", "sleep 300")])))
        await make(full()).start()
        await eventually("three keepalives") { node.keepalives("mj_000000000003") >= 3 }
    }

    // MARK: A-5 concurrency

    func testNeverMoreThanFourAtOnceNorTwoPerDesk() async throws {
        let jobs = [
            runJob("mj_00000000000a", desk: "atlas"), runJob("mj_00000000000b", desk: "atlas"),
            runJob("mj_00000000000c", desk: "atlas"),                                             // 3rd atlas: busy
            runJob("mj_00000000000d", desk: "orion"), runJob("mj_00000000000e", desk: "orion"),
            runJob("mj_00000000000f", desk: "vega"),                                              // 5th overall: busy
        ]
        node.script(.success(MacPollResponse(jobs: jobs)))
        await make(full()).start()
        await eventually("two refusals") {
            node.result("mj_00000000000c")?.reason == "node_busy" && node.result("mj_00000000000f")?.reason == "node_busy"
        }
        XCTAssertEqual(node.result("mj_00000000000c")?.state, .refused)
        XCTAssertEqual(exec.peak, 4)
        XCTAssertEqual(exec.startedCount, 4)
        await eventually("a poll with no free slots") {
            guard let p = node.lastPoll else { return false }
            return p.freeSlots == 0 && Set(p.running) == ["mj_00000000000a", "mj_00000000000b", "mj_00000000000d", "mj_00000000000e"]
        }
    }

    func testFreeSlotsComeBackWhenAJobEnds() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000011")])))
        await make(full()).start()
        await eventually("slot taken") { node.lastPoll?.freeSlots == 3 }
        exec.run("mj_000000000011")?.finish(.done, exit: 0)
        await eventually("slot back") { node.lastPoll?.freeSlots == 4 && node.lastPoll?.running == [] }
    }

    // MARK: A-5 backoff

    func testBackoffDoublesToThirtyOnServerTroubleAndResetsOnSuccess() async {
        let down = MacNodeError.refused(status: 503, reason: "http_503", detail: "")
        node.script(.failure(down), .failure(down), .failure(down), .failure(down), .failure(down), .failure(down),
                    .failure(MacNodeError.transport("offline")),
                    .success(MacPollResponse()), .failure(down))
        await make(full()).start()
        await eventually("nine polls") { node.pollCount >= 10 }
        XCTAssertEqual(Array(sleeps.all.prefix(8)), [1, 2, 4, 8, 16, 30, 30, 1])
        await eventually("online again") { states.current.connection == .online }
    }

    func testBackoffSequenceIsPure() {
        XCTAssertEqual((1...8).map { MacBridge.backoff(attempt: $0) }, [1, 2, 4, 8, 16, 30, 30, 30])
        XCTAssertEqual(MacBridge.backoff(attempt: 100), 30)
    }

    func testUnknownNodeRegistersAgain() async {
        node.script(.failure(MacNodeError.refused(status: 401, reason: "unknown_node", detail: "")))
        await make(full()).start()
        await eventually("re-registered") { node.registerCount == 2 && node.pollCount >= 2 }
    }

    func testRegisterFailureBacksOff() async {
        node.lock.withLock { node.registerErrors = [MacNodeError.transport("no route"), MacNodeError.transport("no route")] }
        await make(full()).start()
        await eventually("registered on the third try") { node.registerCount == 3 && node.pollCount >= 1 }
        XCTAssertEqual(Array(sleeps.all.prefix(2)), [1, 2])
    }

    // MARK: A-5 pause / stop / cancel

    func testPauseKillsJobsTellsTheDeckOnceAndStopsPolling() async throws {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000021", "sleep 300")])))
        let b = make(full())
        await b.start()
        await eventually("running") { exec.run("mj_000000000021") != nil }

        await b.setMode(.paused)
        await eventually("killed") { exec.run("mj_000000000021")?.wasCancelled == true }
        await eventually("cancelled reported") { node.result("mj_000000000021")?.state == .cancelled }
        await eventually("paused heartbeat") { node.lastPoll?.mode == .paused }
        let paused = try XCTUnwrap(node.lastPoll)
        XCTAssertEqual(paused.wait, 0)
        XCTAssertEqual(paused.freeSlots, 0)
        let count = node.pollCount
        await pause(0.2)
        XCTAssertEqual(node.pollCount, count, "Pause must stop polling")
        XCTAssertEqual(states.current.connection, .paused)
        XCTAssertEqual(states.current.mode, .paused)

        await b.setMode(.ask)
        await eventually("polling again") { node.pollCount > count + 1 && node.lastPoll?.mode == .ask }
    }

    func testOffKillsJobsAndStopsPolling() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000022", "sleep 300")])))
        let b = make(full())
        await b.start()
        await eventually("running") { exec.run("mj_000000000022") != nil }
        await b.setMode(.off)
        await eventually("killed") { exec.run("mj_000000000022")?.wasCancelled == true }
        let count = node.pollCount
        await pause(0.2)
        XCTAssertEqual(node.pollCount, count)
        XCTAssertEqual(states.current.connection, .off)
    }

    /// Quit: nothing executes while the app is not running.
    func testStopKillsEveryRunningJobReportsAndStopsPolling() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000031", "sleep 300"),
                                                    runJob("mj_000000000032", desk: "orion", "sleep 300")])))
        let b = make(full())
        await b.start()
        await eventually("both running") { exec.startedCount == 2 }
        await b.stop()
        XCTAssertEqual(exec.run("mj_000000000031")?.wasCancelled, true)
        XCTAssertEqual(exec.run("mj_000000000032")?.wasCancelled, true)
        XCTAssertEqual(node.result("mj_000000000031")?.state, .cancelled)
        XCTAssertEqual(node.result("mj_000000000032")?.state, .cancelled)
        let count = node.pollCount
        await pause(0.1)
        XCTAssertEqual(node.pollCount, count)
        let state = await b.snapshot
        XCTAssertTrue(state.running.isEmpty)
        XCTAssertEqual(state.connection, .off)
    }

    func testCancelListedByAPollKillsTheJob() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000041", "sleep 300")])))
        await make(full()).start()
        await eventually("running") { exec.run("mj_000000000041") != nil }
        node.script(.success(MacPollResponse(cancel: ["mj_000000000041"])))
        await eventually("killed") { exec.run("mj_000000000041")?.wasCancelled == true }
        await eventually("reported") { node.result("mj_000000000041")?.state == .cancelled }
    }

    func testCancelTrueOnAnEventsReplyKillsTheJob() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000042", "sleep 300")])))
        await make(full()).start()
        await eventually("running") { exec.run("mj_000000000042") != nil }
        node.lock.withLock { node.cancelOnEvents = ["mj_000000000042"] }
        await eventually("killed") { exec.run("mj_000000000042")?.wasCancelled == true }
        await eventually("reported") { node.result("mj_000000000042")?.state == .cancelled }
    }

    /// Cancelled while it waited for its turn: it never starts at all.
    func testCancelBeforeStartNeverRuns() async {
        node.lock.withLock { node.cancelOnEvents = ["mj_000000000044"] }
        let b = make(MacPolicyConfig(mode: .ask, folders: ["~/work"]))
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000044", "ls")])))
        await b.start()
        await eventually("cancelled") { node.result("mj_000000000044")?.state == .cancelled }
        XCTAssertEqual(exec.startedCount, 0)
        XCTAssertTrue(states.current.pendingGrants.isEmpty || states.current.pendingGrants.allSatisfy { $0.jobId == "mj_000000000044" })
    }

    /// The deck already gave up on it (`409 job_settled`): kill it here too.
    /// Settled only once it is running: settled from the first keepalive, the
    /// reporter can win the race to the executor and the job (rightly) never
    /// starts, so "was the run cancelled" had no run to ask about — measured
    /// failing whenever the suite ran slower (PRs #88, #92 merged).
    func testAJobTheDeckSettledIsKilled() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000043", "sleep 300")])))
        await make(full()).start()
        await eventually("running") { exec.run("mj_000000000043") != nil }
        node.lock.withLock { node.goneOnEvents = ["mj_000000000043"] }
        await eventually("killed") { exec.run("mj_000000000043")?.wasCancelled == true }
    }

    /// Settled while it waited for his answer: it never starts at all.
    func testAJobTheDeckSettledBeforeItStartedNeverRuns() async {
        node.lock.withLock { node.goneOnEvents = ["mj_000000000045"] }
        let b = make(MacPolicyConfig(mode: .ask, folders: ["~/work"]))
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000045", "ls")])))
        await b.start()
        // Its end is logged; the card is the desk's, not the job's, and stays.
        await eventually("given up") {
            states.current.recent.contains { $0.jobId == "mj_000000000045" && $0.state == "cancelled" }
        }
        XCTAssertEqual(exec.startedCount, 0)
    }

    func testStopDeskKillsOnlyThatDesksJobs() async {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000051", desk: "atlas", "sleep 300"),
                                                    runJob("mj_000000000052", desk: "orion", "sleep 300")])))
        let b = make(full())
        await b.start()
        await eventually("both running") { exec.startedCount == 2 }
        await b.stopDesk("atlas")
        await eventually("atlas killed") { exec.run("mj_000000000051")?.wasCancelled == true }
        await pause(0.05)
        XCTAssertEqual(exec.run("mj_000000000052")?.wasCancelled, false)
    }

    // MARK: policy + grant wait

    func testAPolicyRefusalNeverReachesTheExecutor() async throws {
        let read = MacWireJob(id: "mj_000000000061", desk: "atlas", kind: .read, args: MacJobArgs(path: "~/.ssh/id_ed25519"))
        node.script(.success(MacPollResponse(jobs: [read])))
        await make(MacPolicyConfig(mode: .ask, folders: ["~/work"])).start()
        await eventually("refused") { node.result("mj_000000000061") != nil }
        let r = try XCTUnwrap(node.result("mj_000000000061"))
        XCTAssertEqual(r.state, .refused)
        XCTAssertEqual(r.reason, "blocked_path")
        XCTAssertNil(r.exit)
        XCTAssertEqual(exec.startedCount, 0)
        XCTAssertEqual(node.events("mj_000000000061").map(\.type), ["result"])
        await eventually("logged") { log.recent().first?.decision == .refused }
        XCTAssertEqual(log.recent().first?.reason, "blocked_path")
    }

    func testUnknownKindIsRefusedNotLost() async {
        let odd = try! JSONDecoder().decode(MacWireJob.self, from: Data(
            #"{"id":"mj_000000000062","desk":"atlas","kind":"teleport","args":{},"timeout_s":5,"background":false,"created_at":1}"#.utf8))
        node.script(.success(MacPollResponse(jobs: [odd])))
        await make(full()).start()
        await eventually("refused") { node.result("mj_000000000062")?.reason == "bad_input" }
    }

    private func askReadJob(_ id: String) throws -> MacWireJob {
        try "hello".write(toFile: home + "/work/a.txt", atomically: true, encoding: .utf8)
        return MacWireJob(id: id, desk: "atlas", kind: .read, args: MacJobArgs(path: "~/work/a.txt", offset: 0, length: 100, encoding: "text"))
    }

    func testAskWaitsForTheCardThenRunsWithoutTellingTheDeckTwice() async throws {
        node.script(.success(MacPollResponse(jobs: [try askReadJob("mj_000000000071")])))
        let b = make(MacPolicyConfig(mode: .ask, folders: ["~/work"]))
        await b.start()
        await eventually("awaiting grant") { node.events("mj_000000000071").first?.type == "awaiting_grant" }
        await eventually("card shown") { states.current.pendingGrants.map(\.desk) == ["atlas"] }
        XCTAssertEqual(states.current.pendingGrants.first?.summary, "read ~/work/a.txt")

        await b.answerGrant(desk: "atlas", decision: .hour)
        await eventually("read done") { node.result("mj_000000000071")?.state == .done }
        let r = try XCTUnwrap(node.result("mj_000000000071"))
        XCTAssertEqual(r.payload["text"], "hello")
        XCTAssertEqual(r.payload["size"], 5)
        XCTAssertEqual(r.payload["truncated"], false)
        XCTAssertTrue(node.grantPosts.isEmpty, "a tap in time runs the job; the desk needs no second line")
        XCTAssertTrue(states.current.pendingGrants.isEmpty)
        let saved = MacPolicyStore(url: URL(fileURLWithPath: dir + "/support/mac-policy.json")).load()
        XCTAssertEqual(saved.grants["atlas"]?.state, .hour)
        let polled = await b.snapshot
        XCTAssertEqual(polled.mode, .ask)
    }

    func testNoAnswerInTimeRefusesAwaitingGrantAndALateTapTellsTheDeck() async throws {
        node.script(.success(MacPollResponse(jobs: [try askReadJob("mj_000000000072")])))
        let b = make(MacPolicyConfig(mode: .ask, folders: ["~/work"])) { $0.grantWait = 0.1 }
        await b.start()
        await eventually("gave up waiting") { node.result("mj_000000000072") != nil }
        let r = try XCTUnwrap(node.result("mj_000000000072"))
        XCTAssertEqual(r.state, .refused)
        XCTAssertEqual(r.reason, "awaiting_grant")
        XCTAssertEqual(states.current.pendingGrants.map(\.desk), ["atlas"], "the card stays until he answers")

        await b.answerGrant(desk: "atlas", decision: .always)
        await eventually("grant posted") { !node.grantPosts.isEmpty }
        XCTAssertEqual(node.grantPosts, [MacGrantPost(desk: "atlas", decision: .always, until: nil)])
        XCTAssertTrue(states.current.pendingGrants.isEmpty)
    }

    func testDenyInTimeRefusesDenied() async throws {
        node.script(.success(MacPollResponse(jobs: [try askReadJob("mj_000000000073")])))
        let b = make(MacPolicyConfig(mode: .ask, folders: ["~/work"]))
        await b.start()
        await eventually("awaiting grant") { !states.current.pendingGrants.isEmpty }
        await b.answerGrant(desk: "atlas", decision: .deny)
        await eventually("refused") { node.result("mj_000000000073")?.reason == "denied" }
        XCTAssertTrue(node.grantPosts.isEmpty)
    }

    func testKeepalivesContinueWhileWaitingForTheCard() async throws {
        node.script(.success(MacPollResponse(jobs: [try askReadJob("mj_000000000074")])))
        await make(MacPolicyConfig(mode: .ask, folders: ["~/work"])).start()
        await eventually("keepalives while the card is up") { node.keepalives("mj_000000000074") >= 3 }
    }

    func testRevokeAlwaysTellsTheDeck() async {
        let b = make(full { $0.grants = ["atlas": MacGrant(state: .always, until: nil)] })
        await b.start()
        await eventually("registered") { node.pollCount >= 1 }
        await b.answerGrant(desk: "atlas", decision: .revoke)
        await eventually("revoke posted") { node.grantPosts.map(\.decision) == [.revoke] }
    }

    func testAskModeRunGetsTheSeatbeltProfileAndAFolderCwd() async throws {
        node.script(.success(MacPollResponse(jobs: [runJob("mj_000000000075", "ls")])))
        exec.script = { run, _ in run.finish(.done, exit: 0) }
        await make(MacPolicyConfig(mode: .ask, folders: ["~/work"], grants: ["atlas": MacGrant(state: .always, until: nil)])).start()
        await eventually("ran") { node.result("mj_000000000075")?.state == .done }
        let spec = try XCTUnwrap(exec.spec("mj_000000000075"))
        XCTAssertNotNil(spec.sandboxProfile)
        XCTAssertEqual(spec.cwd, home + "/work")
        XCTAssertEqual(spec.folders, [home + "/work"])
        if case .started(let s) = node.events("mj_000000000075").first?.body { XCTAssertTrue(s.sandboxed) }
        else { XCTFail("started first") }
    }

    // MARK: open + screenshot go through the injected executor

    func testOpenGoesThroughTheExecutorQuotedAndRefusesOddSchemes() async throws {
        exec.script = { run, _ in run.finish(.done, exit: 0) }
        node.script(.success(MacPollResponse(jobs: [
            MacWireJob(id: "mj_000000000081", desk: "atlas", kind: .open, args: MacJobArgs(target: "https://example.com/a'b")),
            MacWireJob(id: "mj_000000000082", desk: "atlas", kind: .open, args: MacJobArgs(target: "file:///etc/passwd")),
            MacWireJob(id: "mj_000000000083", desk: "orion", kind: .open, args: MacJobArgs(target: "-a Terminal")),
        ])))
        await make(full()).start()
        await eventually("all settled") { ["mj_000000000081", "mj_000000000082", "mj_000000000083"].allSatisfy { node.result($0) != nil } }
        XCTAssertEqual(node.result("mj_000000000081")?.state, .done)
        let spec = try XCTUnwrap(exec.spec("mj_000000000081"))
        XCTAssertEqual(spec.command, #"/usr/bin/open 'https://example.com/a'\''b'"#)
        XCTAssertEqual(spec.shell, "/bin/sh")
        XCTAssertEqual(node.result("mj_000000000082")?.reason, "bad_input")
        XCTAssertEqual(node.result("mj_000000000083")?.reason, "bad_input")
        XCTAssertEqual(exec.startedCount, 1)
    }

    private static func jpeg(width: Int, height: Int) -> Data {
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

    func testScreenshotRunsScreencaptureThroughTheExecutorAndReturnsAJPEG() async throws {
        exec.script = { run, _ in
            // Stand in for `screencapture`: write a JPEG where it was told to.
            let path = run.spec.command.components(separatedBy: " ").last!.trimmingCharacters(in: CharacterSet(charactersIn: "'"))
            try? MacBridgeTests.jpeg(width: 40, height: 30).write(to: URL(fileURLWithPath: path))
            run.finish(.done, exit: 0)
        }
        node.script(.success(MacPollResponse(jobs: [MacWireJob(id: "mj_000000000091", desk: "atlas", kind: .screenshot)])))
        await make(full { $0.screenshotEnabled = true }).start()
        await eventually("shot") { node.result("mj_000000000091") != nil }
        let spec = try XCTUnwrap(exec.spec("mj_000000000091"))
        XCTAssertTrue(spec.command.hasPrefix("/usr/sbin/screencapture -x -t jpg "), spec.command)
        let r = try XCTUnwrap(node.result("mj_000000000091"))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.payload["mime"], "image/jpeg")
        XCTAssertEqual(r.payload["width"], 40)
        XCTAssertEqual(r.payload["height"], 30)
        guard case .string(let b64)? = r.payload["base64"], let data = Data(base64Encoded: b64) else {
            return XCTFail("base64 JPEG expected")
        }
        XCTAssertEqual(Array(data.prefix(2)), [0xFF, 0xD8])
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: NSTemporaryDirectory()).filter { $0.contains("mj_000000000091") }
        XCTAssertEqual(leftovers, [], "the capture file is removed")
    }

    /// Ask me guards screenshots with their own switch; Full access includes
    /// them (MacPolicyTests.testFullAccessIncludesScreenshots).
    func testScreenshotOffIsRefusedByPolicy() async {
        node.script(.success(MacPollResponse(jobs: [MacWireJob(id: "mj_000000000092", desk: "atlas", kind: .screenshot)])))
        await make(MacPolicyConfig(mode: .ask, folders: ["~/work"],
                                   grants: ["atlas": MacGrant(state: .always, until: nil)])).start()
        await eventually("refused") { node.result("mj_000000000092")?.reason == "capability_off" }
        XCTAssertEqual(exec.startedCount, 0)
    }

    /// The bridge takes the executor as the injected contract; naming the
    /// concrete type would drag it into a build that must not link it.
    func testBridgeSourceNeverNamesTheConcreteExecutor() throws {
        let here = URL(fileURLWithPath: #filePath)
        let src = here.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckKit/MacBridge")
        for file in ["MacBridge.swift", "MacNodeClient.swift", "MacWire.swift"] {
            let text = try String(contentsOf: src.appendingPathComponent(file), encoding: .utf8)
            XCTAssertFalse(text.contains("MacExecutor"), "\(file) names MacExecutor")
            XCTAssertFalse(text.contains("MacLocalExecutor"), "\(file) names MacLocalExecutor")
        }
    }
}
