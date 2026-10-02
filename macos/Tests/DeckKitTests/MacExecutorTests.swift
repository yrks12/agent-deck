import Darwin
import XCTest
@testable import DeckKit

/// **A-3: a job runs as the user, streams, and leaves nothing behind.**
///
/// The failures this guards are the ones a desk cannot see from the box: a
/// timeout that kills the shell but leaves its `sleep` children running on his
/// Mac; a 20 MiB log that fills the pipe and wedges the wait forever; output
/// that only arrives at the end, so the activity view shows nothing for ten
/// minutes.
final class MacExecutorTests: XCTestCase {
    var dir: String!

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-exec-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(atPath: dir) }

    final class Sink: @unchecked Sendable {
        let lock = NSLock()
        var out = Data(), err = Data()
        var firstOutAt: Date?
        var outBytes = 0
        func take(_ s: MacStream, _ d: Data) {
            lock.withLock {
                if s == .stdout {
                    if firstOutAt == nil { firstOutAt = Date() }
                    outBytes += d.count
                    if out.count < 1 << 16 { out.append(d) }
                } else { err.append(d) }
            }
        }
        var stdout: String { lock.withLock { String(decoding: out, as: UTF8.self) } }
        var stderr: String { lock.withLock { String(decoding: err, as: UTF8.self) } }
    }

    private func spec(_ command: String, timeout: TimeInterval = 20, shell: String? = "/bin/sh") -> MacRunSpec {
        var s = MacRunSpec(command: command, cwd: dir, desk: "atlas", jobId: "mj_test", timeout: timeout, shell: shell)
        s.killGrace = 1
        return s
    }

    private func run(_ s: MacRunSpec, _ sink: Sink = Sink()) -> (MacRunResult, Sink) {
        let h = MacExecutor.start(s, output: { sink.take($0, $1) })
        return (h.wait(), sink)
    }

    private func gone(_ pid: pid_t, within seconds: TimeInterval = 5) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if Darwin.kill(pid, 0) != 0 && errno == ESRCH { return true }
            usleep(50_000)
        }
        return false
    }

    private func pids(_ text: String) -> [pid_t] {
        text.split(whereSeparator: \.isNewline).compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
    }

    func testEchoHiInTheLoginShell() {
        let (r, sink) = run(spec("echo hi", shell: nil))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.exit, 0)
        XCTAssertEqual(sink.stdout, "hi\n")
        XCTAssertFalse(r.sandboxed)
    }

    func testOutputStreamsBeforeTheJobEnds() {
        let started = Date()
        let (r, sink) = run(spec("echo a; sleep 1.5; echo b"))
        XCTAssertEqual(sink.stdout, "a\nb\n")
        XCTAssertLessThan(sink.firstOutAt!.timeIntervalSince(started), 1.2, "first line must flush within ~500 ms")
        XCTAssertGreaterThan(Double(r.durationMs), 1400)
    }

    func testExitThreeIsDoneWithExitThree() {
        let (r, _) = run(spec("exit 3"))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.exit, 3)
        XCTAssertNil(r.reason)
    }

    func testCwdIsHonoured() {
        let (r, sink) = run(spec("pwd -P"))
        XCTAssertEqual(r.cwd, dir)
        XCTAssertEqual(sink.stdout, dir + "\n")
    }

    func testStdinIsDevNull() {
        let (r, _) = run(spec("cat", timeout: 5))
        XCTAssertEqual(r.state, .done, "cat must see EOF at once, not wait for a keyboard")
    }

    func testTheEnvironmentNamesTheDeskAndCannotBeOverridden() {
        var s = spec("echo $AGENT_DECK_DESK $AGENT_DECK_JOB $TERM $PAGER $FOO")
        s.env = ["FOO": "bar", "AGENT_DECK_DESK": "forged"]
        let (_, sink) = run(s)
        XCTAssertEqual(sink.stdout, "atlas mj_test dumb cat bar\n")
    }

    func testPathGetsHomebrew() {
        let env = MacExecutor.environment(base: ["PATH": "/usr/bin:/bin"], desk: "d", jobId: "j", extra: [:])
        XCTAssertTrue(env["PATH"]!.hasPrefix("/opt/homebrew/bin:/usr/local/bin:"), env["PATH"]!)
    }

    /// The children do not hold the output pipe, so only the process-group
    /// signal can reach them — the leftover-output backstop cannot mask a
    /// signal sent to the shell alone.
    func testTimeoutKillsTheWholeProcessGroup() {
        let (r, sink) = run(spec("sleep 30 >/dev/null 2>&1 & echo $!; sleep 30 >/dev/null 2>&1 & echo $!; wait", timeout: 1))
        XCTAssertEqual(r.state, .timedOut)
        XCTAssertEqual(r.reason, "timed_out")
        let children = pids(sink.stdout)
        XCTAssertEqual(children.count, 2)
        for pid in children { XCTAssertTrue(gone(pid), "sleep \(pid) outlived the job") }
    }

    func testATermIgnoringJobIsKilledAfterTheGrace() {
        let (r, _) = run(spec("trap '' TERM; sleep 30", timeout: 0.5))
        XCTAssertEqual(r.state, .timedOut)
        XCTAssertEqual(r.signal, "KILL")
        XCTAssertLessThan(r.durationMs, 5000)
    }

    func testCancelKillsTheWholeProcessGroup() {
        let sink = Sink()
        let h = MacExecutor.start(spec("sleep 30 >/dev/null 2>&1 & echo $!; wait"), output: { sink.take($0, $1) })
        let end = Date().addingTimeInterval(5)
        while sink.stdout.isEmpty && Date() < end { usleep(20_000) }
        h.cancel()
        let r = h.wait()
        XCTAssertEqual(r.state, .cancelled)
        for pid in pids(sink.stdout) { XCTAssertTrue(gone(pid), "sleep \(pid) outlived Stop") }
    }

    func testABackgroundChildHoldingStdoutDoesNotHangTheJob() {
        let started = Date()
        let (r, sink) = run(spec("sleep 30 & echo $!", timeout: 20))
        XCTAssertEqual(r.state, .done)
        XCTAssertLessThan(Date().timeIntervalSince(started), 6)
        for pid in pids(sink.stdout) { XCTAssertTrue(gone(pid)) }
    }

    func testTwentyMebibytesIsCappedWithoutDeadlock() {
        let started = Date()
        let (r, sink) = run(spec("head -c 20971520 /dev/zero; echo tail >&2", timeout: 60))
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(r.stdoutBytes, 20 << 20)
        XCTAssertEqual(sink.outBytes, 8 << 20)
        XCTAssertTrue(r.truncated)
        XCTAssertEqual(sink.stderr, "tail\n")
        XCTAssertLessThan(Date().timeIntervalSince(started), 30)
    }

    func testAMissingCwdFailsWithoutRunning() {
        var s = spec("true")
        s.cwd = dir + "/nope"
        let (r, _) = run(s)
        XCTAssertEqual(r.state, .failed)
        XCTAssertEqual(r.reason, "no_such_path")
    }

    func testASandboxedWriteOutsideIsOutOfScope() throws {
        try XCTSkipUnless(MacSeatbelt.isAvailable)
        let home = MacPaths(home: dir)
        try FileManager.default.createDirectory(atPath: dir + "/w", withIntermediateDirectories: true)
        let profile = MacSeatbelt.profile(paths: home, folders: [dir + "/w"], neverTouch: [], exceptions: [],
                                          tempDirs: ["/private/tmp"])
        var s = spec("echo x > \(dir!)/outside.txt")
        s.cwd = dir + "/w"
        s.sandboxProfile = profile.text
        s.folders = [dir + "/w"]
        let sink = Sink()
        let h = MacExecutor.start(s, home: dir, output: { sink.take($0, $1) })
        let r = h.wait()
        XCTAssertTrue(r.sandboxed)
        XCTAssertEqual(r.state, .failed, sink.stderr)
        XCTAssertEqual(r.reason, "out_of_scope")
    }

    // MARK: classifying a failure

    func testTccDenialIsNamedWithTheSettingsPath() {
        let d = MacExecutor.classifyDenial(stderr: "ls: /Users/y/Documents: Operation not permitted\n",
                                           sandboxed: false, folders: [], home: "/Users/y")
        XCTAssertEqual(d?.reason, "tcc_denied")
        XCTAssertTrue(d!.detail.contains("Privacy & Security → Files and Folders"))
    }

    func testSudoIsNeedsAdmin() {
        XCTAssertEqual(MacExecutor.classifyDenial(stderr: "sudo: a terminal is required to read the password", sandboxed: false,
                                                  folders: [], home: "/Users/y")?.reason, "needs_admin")
    }

    func testAnOrdinaryFailureIsNotClassified() {
        XCTAssertNil(MacExecutor.classifyDenial(stderr: "grep: x: No such file or directory", sandboxed: true,
                                                folders: [], home: "/Users/y"))
        XCTAssertNil(MacExecutor.classifyDenial(stderr: "rm: /etc/hosts: Operation not permitted", sandboxed: false,
                                                folders: [], home: "/Users/y"))
    }

    func testArgvWrapsInSandboxExecOnlyWithAProfile() {
        XCTAssertEqual(MacExecutor.argv(shell: "/bin/zsh", command: "ls", sandboxProfile: nil), ["/bin/zsh", "-l", "-c", "ls"])
        XCTAssertEqual(MacExecutor.argv(shell: "/bin/zsh", command: "ls", sandboxProfile: "P"),
                       ["/usr/bin/sandbox-exec", "-p", "P", "/bin/zsh", "-l", "-c", "ls"])
    }

    // MARK: the executor is injected, and stands alone

    /// A2 receives `any MacJobExecuting`; the real one must work through it.
    func testTheLocalExecutorRunsThroughTheProtocol() {
        let exec: any MacJobExecuting = MacLocalExecutor(home: dir)
        let sink = Sink()
        let run = exec.start(spec("echo via-protocol"), output: { sink.take($0, $1) })
        let r = run.wait()
        XCTAssertEqual(r.state, .done)
        XCTAssertEqual(sink.stdout, "via-protocol\n")
    }

    /// A later slice moves `MacExecutor.swift` into its own target that the
    /// App Store build does not link. It may lean on the OS and the contract
    /// file only — never on policy, Seatbelt, paths or the bridge (A2).
    func testMacExecutorSwiftNamesNoPolicyOrBridgeType() throws {
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../Sources/DeckKit/MacBridge/MacExecutor.swift").standardized
        let code = try String(contentsOf: source, encoding: .utf8)
            .split(separator: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
        for banned in ["MacPaths", "MacSeatbelt", "MacPolicy", "MacBridge", "MacNodeClient", "MacWire",
                       "MacActivityLog", "MacFileOps", "MacTransport", "import DeckUI", "import AppKit", "import SwiftUI"] {
            XCTAssertFalse(code.contains(banned), "MacExecutor.swift depends on \(banned)")
        }
    }
}
