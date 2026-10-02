import Darwin
import Foundation

/// **Runs one shell line as the user, and makes sure nothing it started
/// outlives it.**
///
/// `posix_spawn` into a new process group (`POSIX_SPAWN_SETPGROUP`), argv
/// `[$SHELL, -l, -c, command]` — measured: `-l -c` finds brew, node, git and
/// python3 in 0.04 s, while `-l -i` costs 0.8 s and prints zsh session noise —
/// wrapped in `sandbox-exec -p` when there is a profile. stdin is
/// `/dev/null`, so `sudo` fails fast instead of waiting for a password.
///
/// Timeout, cancel and Stop send `SIGTERM` to the whole group and `SIGKILL`
/// after `killGrace`. Both pipes are drained on their own threads for the
/// whole life of the job — past the 8 MiB cap too — so a chatty command can
/// never block on a full pipe and deadlock the wait.
public final class MacRunHandle: MacJobRun, @unchecked Sendable {
    public let pid: pid_t
    public let sandboxed: Bool
    public let cwd: String

    private let lock = NSLock()
    private var stopReason: MacRunResult.State?
    private var result: MacRunResult?
    private let finished = DispatchSemaphore(value: 0)
    private var waiters: [@Sendable (MacRunResult) -> Void] = []
    private let killGrace: TimeInterval

    init(pid: pid_t, sandboxed: Bool, cwd: String, killGrace: TimeInterval) {
        self.pid = pid
        self.sandboxed = sandboxed
        self.cwd = cwd
        self.killGrace = killGrace
    }

    init(failed result: MacRunResult) {
        pid = 0
        sandboxed = result.sandboxed
        cwd = result.cwd
        killGrace = 0
        self.result = result
        finished.signal()
    }

    /// Stop now (desk cancel, Stop in the bar, Quit).
    public func cancel() { stop(.cancelled) }

    /// Blocks until the job has settled.
    public func wait() -> MacRunResult {
        finished.wait()
        finished.signal()
        return lock.withLock { result! }
    }

    /// Called once, on a background queue, when the job settles (at once if it already has).
    public func onFinish(_ body: @escaping @Sendable (MacRunResult) -> Void) {
        let settled: MacRunResult? = lock.withLock {
            if let result { return result }
            waiters.append(body)
            return nil
        }
        if let settled { DispatchQueue.global().async { body(settled) } }
    }

    var reasonForStop: MacRunResult.State? { lock.withLock { stopReason } }
    var isSettled: Bool { lock.withLock { result != nil } }

    func stop(_ why: MacRunResult.State) {
        let first: Bool = lock.withLock {
            guard result == nil, stopReason == nil else { return false }
            stopReason = why
            return true
        }
        guard first, pid > 0 else { return }
        Darwin.kill(-pid, SIGTERM)
        DispatchQueue.global().asyncAfter(deadline: .now() + killGrace) { [self] in
            if !isSettled { Darwin.kill(-pid, SIGKILL) }
        }
    }

    func settle(_ r: MacRunResult) {
        let callbacks: [@Sendable (MacRunResult) -> Void] = lock.withLock {
            result = r
            defer { waiters = [] }
            return waiters
        }
        finished.signal()
        for cb in callbacks { DispatchQueue.global().async { cb(r) } }
    }
}

/// The real `MacJobExecuting`: spawns on this Mac.
public struct MacLocalExecutor: MacJobExecuting {
    public let home: String
    public let baseEnvironment: [String: String]

    public init(home: String = MacExecutor.resolvedHome(), baseEnvironment: [String: String] = ProcessInfo.processInfo.environment) {
        self.home = home
        self.baseEnvironment = baseEnvironment
    }

    public func start(_ spec: MacRunSpec, output: @escaping @Sendable (MacStream, Data) -> Void) -> any MacJobRun {
        MacExecutor.start(spec, home: home, baseEnvironment: baseEnvironment, output: output)
    }
}

/// Self-contained on purpose: this file uses only the OS and the contract in
/// `MacJobExecuting.swift` — no policy, Seatbelt, paths or bridge types — so it
/// can move into its own target.
public enum MacExecutor {

    public static let sandboxExec = "/usr/bin/sandbox-exec"

    /// NSHomeDirectory, resolved (`/var` → `/private/var`), as TCC paths print.
    public static func resolvedHome() -> String {
        guard let r = realpath(NSHomeDirectory(), nil) else { return NSHomeDirectory() }
        defer { free(r) }
        return String(cString: r)
    }

    /// This user's temp dir, resolved — the default cwd.
    static func userTempDirectory() -> String {
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
        guard confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count) > 0, let r = realpath(buf, nil) else { return "/private/tmp" }
        defer { free(r) }
        return String(cString: r)
    }

    static func isInside(_ path: String, _ folder: String) -> Bool {
        path == folder || path.hasPrefix(folder.hasSuffix("/") ? folder : folder + "/")
    }

    /// The user's login shell from the password database; `/bin/zsh` if unset.
    public static func loginShell() -> String {
        if let pw = getpwuid(getuid()), let sh = pw.pointee.pw_shell {
            let s = String(cString: sh)
            if !s.isEmpty, FileManager.default.isExecutableFile(atPath: s) { return s }
        }
        return "/bin/zsh"
    }

    public static func argv(shell: String, command: String, sandboxProfile: String?) -> [String] {
        let inner = [shell, "-l", "-c", command]
        guard let sandboxProfile else { return inner }
        return [sandboxExec, "-p", sandboxProfile] + inner
    }

    /// App env + the job's env + fixed keys; Homebrew dirs put on PATH if absent.
    public static func environment(base: [String: String], desk: String, jobId: String,
                                   extra: [String: String]) -> [String: String] {
        var env = base
        for (k, v) in extra where !k.isEmpty && !k.contains("=") { env[k] = v }
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env["AGENT_DECK_DESK"] = desk
        env["AGENT_DECK_JOB"] = jobId
        var path = (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        for dir in ["/usr/local/bin", "/opt/homebrew/bin"] where !path.contains(dir) { path.insert(dir, at: 0) }
        env["PATH"] = path.joined(separator: ":")
        return env
    }

    /// Tell a macOS privacy block (`tcc_denied`), our own sandbox
    /// (`out_of_scope`) and a password prompt (`needs_admin`) apart from an
    /// ordinary failing command, from the tail of stderr.
    public static func classifyDenial(stderr: String, sandboxed: Bool, folders: [String],
                                      home: String) -> (reason: String, detail: String)? {
        if stderr.contains("sudo: a terminal is required") || stderr.contains("sudo: a password is required") {
            return ("needs_admin", "This needs an administrator password, which agents cannot give. Tell the owner the exact command to run himself.")
        }
        for line in stderr.split(separator: "\n") where line.contains("Operation not permitted") {
            if let f = MacTCC.folder(in: line, home: home),
               !sandboxed || folders.contains(where: { isInside(f, $0) || isInside($0, f) }) {
                return ("tcc_denied", MacTCC.detail(f))
            }
            if sandboxed {
                return ("out_of_scope", "The command touched a path outside the folders agents may use on this Mac (\(line.prefix(200))). Ask the owner to add the folder in Shaliach → Settings → Mac.")
            }
        }
        return nil
    }

    // MARK: - keep the Mac awake while anything runs

    private static let activityLock = NSLock()
    nonisolated(unsafe) private static var activeJobs = 0
    nonisolated(unsafe) private static var activity: NSObjectProtocol?

    static func jobBegan() {
        activityLock.withLock {
            activeJobs += 1
            if activeJobs == 1 {
                activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled],
                                                                 reason: "Shaliach is running a job on this Mac")
            }
        }
    }

    static func jobEnded() {
        activityLock.withLock {
            activeJobs -= 1
            if activeJobs == 0, let a = activity {
                ProcessInfo.processInfo.endActivity(a)
                activity = nil
            }
        }
    }

    // MARK: - spawn

    /// Start the job. A spawn failure returns a handle that has already
    /// settled `failed`. `output` gets batched chunks (≤ `flushInterval` late,
    /// ≤ `flushBytes` big) on reader threads.
    public static func start(_ spec: MacRunSpec, home: String = resolvedHome(),
                             baseEnvironment: [String: String] = ProcessInfo.processInfo.environment,
                             output: @escaping @Sendable (MacStream, Data) -> Void) -> MacRunHandle {
        let started = Date()
        let cwd = spec.cwd ?? userTempDirectory()
        let sandboxed = spec.sandboxProfile != nil
        func failed(_ reason: String, _ detail: String) -> MacRunHandle {
            MacRunHandle(failed: MacRunResult(state: .failed, exit: nil, signal: nil, reason: reason, detail: detail,
                                              durationMs: 0, sandboxed: sandboxed, pid: 0, cwd: cwd,
                                              stdoutBytes: 0, stderrBytes: 0, truncated: false))
        }

        #if os(macOS)
        var st = stat()
        if stat(cwd, &st) != 0 {
            if errno == EPERM || errno == EACCES, let f = MacTCC.folder(in: Substring(cwd), home: home) {
                return failed("tcc_denied", MacTCC.detail(f))
            }
            return failed("no_such_path", "cwd \(cwd) does not exist.")
        }
        if st.st_mode & S_IFMT != S_IFDIR { return failed("not_a_directory", "cwd \(cwd) is not a directory.") }

        var outPipe: [Int32] = [-1, -1], errPipe: [Int32] = [-1, -1], stopPipe: [Int32] = [-1, -1]
        guard pipe(&outPipe) == 0, pipe(&errPipe) == 0, pipe(&stopPipe) == 0 else {
            for fd in outPipe + errPipe + stopPipe where fd >= 0 { close(fd) }
            return failed("queue_failed", "Could not create pipes: \(String(cString: strerror(errno))).")
        }
        for fd in [outPipe[0], errPipe[0], stopPipe[0], stopPipe[1]] { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }

        let args = argv(shell: spec.shell ?? loginShell(), command: spec.command, sandboxProfile: spec.sandboxProfile)
        let env = environment(base: baseEnvironment, desk: spec.desk, jobId: spec.jobId, extra: spec.env)

        var fa: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fa)
        defer { posix_spawn_file_actions_destroy(&fa) }
        posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&fa, outPipe[1], 1)
        posix_spawn_file_actions_adddup2(&fa, errPipe[1], 2)
        posix_spawn_file_actions_addchdir_np(&fa, cwd)

        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK
                                              | POSIX_SPAWN_CLOEXEC_DEFAULT))
        posix_spawnattr_setpgroup(&attr, 0)
        var noMask = sigset_t()
        sigemptyset(&noMask)
        posix_spawnattr_setsigmask(&attr, &noMask)
        var defaults = sigset_t()
        sigemptyset(&defaults)
        for sig in 1..<NSIG where sig != SIGKILL && sig != SIGSTOP { sigaddset(&defaults, sig) }
        posix_spawnattr_setsigdefault(&attr, &defaults)

        let cArgs = args.map { strdup($0) } + [nil]
        let cEnv = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { for p in cArgs + cEnv { free(p) } }

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, args[0], &fa, &attr, cArgs, cEnv)
        close(outPipe[1])
        close(errPipe[1])
        if rc != 0 {
            for fd in [outPipe[0], errPipe[0], stopPipe[0], stopPipe[1]] { close(fd) }
            if rc == EPERM || rc == EACCES, let f = MacTCC.folder(in: Substring(cwd), home: home) {
                return failed("tcc_denied", MacTCC.detail(f))
            }
            return failed("queue_failed", "Could not start \(args[0]): \(String(cString: strerror(rc))).")
        }

        jobBegan()
        let handle = MacRunHandle(pid: pid, sandboxed: sandboxed, cwd: cwd, killGrace: spec.killGrace)
        let readers = DispatchGroup()
        let outPump = Pump(fd: outPipe[0], stopFd: stopPipe[0], stream: .stdout, spec: spec, output: output)
        let errPump = Pump(fd: errPipe[0], stopFd: stopPipe[0], stream: .stderr, spec: spec, output: output)
        for pump in [outPump, errPump] {
            readers.enter()
            let t = Thread { pump.run(); readers.leave() }
            t.name = "mac-job-\(pump.stream.rawValue)"
            t.start()
        }

        DispatchQueue.global().asyncAfter(deadline: .now() + spec.timeout) { handle.stop(.timedOut) }

        let waiter = Thread {
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            // Output still held open by something the shell left behind: give
            // it a moment, then end the group so nothing outlives the job.
            if readers.wait(timeout: .now() + 1) == .timedOut {
                Darwin.kill(-pid, SIGKILL)
                if readers.wait(timeout: .now() + 2) == .timedOut {
                    var one: UInt8 = 1
                    _ = write(stopPipe[1], &one, 1)
                    readers.wait()
                }
            }
            close(stopPipe[0])
            close(stopPipe[1])

            let exited = (status & 0x7f) == 0
            let exit: Int32? = exited ? (status >> 8) & 0xff : nil
            let sig: String? = exited ? nil : signalName(status & 0x7f)
            var state: MacRunResult.State = handle.reasonForStop ?? .done
            var reason: String? = state == .timedOut ? "timed_out" : state == .cancelled ? "cancelled" : nil
            var detail = state == .timedOut ? "Stopped after \(Int(spec.timeout)) s." : state == .cancelled ? "Stopped." : ""
            if state == .done, exit != 0,
               let denial = classifyDenial(stderr: errPump.tail, sandboxed: sandboxed, folders: spec.folders, home: home) {
                state = .failed
                reason = denial.reason
                detail = denial.detail
            }
            jobEnded()
            handle.settle(MacRunResult(state: state, exit: exit, signal: sig, reason: reason, detail: detail,
                                       durationMs: Int(Date().timeIntervalSince(started) * 1000), sandboxed: sandboxed,
                                       pid: pid, cwd: cwd, stdoutBytes: outPump.total, stderrBytes: errPump.total,
                                       truncated: outPump.dropped > 0 || errPump.dropped > 0))
        }
        waiter.name = "mac-job-wait"
        waiter.start()
        return handle
        #else
        // iOS cannot spawn processes: the bridge executor is a Mac-only role.
        return failed("unsupported", "Running commands needs the Mac app.")
        #endif
    }

    static func signalName(_ sig: Int32) -> String {
        switch sig {
        case SIGTERM: return "TERM"
        case SIGKILL: return "KILL"
        case SIGINT: return "INT"
        case SIGHUP: return "HUP"
        case SIGSEGV: return "SEGV"
        case SIGABRT: return "ABRT"
        default: return "SIG\(sig)"
        }
    }
}

/// Drains one pipe for the whole life of the job, batching what it hands on.
final class Pump: @unchecked Sendable {
    let fd: Int32
    let stopFd: Int32
    let stream: MacStream
    let flushInterval: TimeInterval
    let flushBytes: Int
    let cap: Int
    let output: @Sendable (MacStream, Data) -> Void
    private(set) var total = 0
    private(set) var dropped = 0
    /// The last 4 KiB seen (for classifying a failure), kept even past the cap.
    private(set) var tail = ""
    private var tailBytes = Data()

    init(fd: Int32, stopFd: Int32, stream: MacStream, spec: MacRunSpec, output: @escaping @Sendable (MacStream, Data) -> Void) {
        self.fd = fd
        self.stopFd = stopFd
        self.stream = stream
        self.flushInterval = spec.flushInterval
        self.flushBytes = spec.flushBytes
        self.cap = spec.captureCap
        self.output = output
    }

    func run() {
        defer { close(fd) }
        var pending = Data()
        var delivered = 0
        var lastFlush = Date()
        let size = 64 * 1024
        let buf = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
        defer { buf.deallocate() }
        func flush() {
            if !pending.isEmpty { output(stream, pending); delivered += pending.count; pending = Data() }
            lastFlush = Date()
        }
        var fds = [pollfd(fd: fd, events: Int16(POLLIN), revents: 0), pollfd(fd: stopFd, events: Int16(POLLIN), revents: 0)]
        while true {
            let r = poll(&fds, 2, Int32(flushInterval * 1000))
            if r < 0 { if errno == EINTR { continue } else { break } }
            if r == 0 { flush(); continue }
            if fds[1].revents != 0 && fds[0].revents & Int16(POLLIN) == 0 { break }
            let n = read(fd, buf, size)
            if n < 0 { if errno == EINTR || errno == EAGAIN { continue } else { break } }
            if n == 0 { break }
            total += n
            tailBytes.append(buf, count: n)
            if tailBytes.count > 4096 { tailBytes = tailBytes.suffix(4096) }
            let room = cap - delivered - pending.count
            if room > 0 { pending.append(buf, count: min(n, room)) }
            dropped += max(0, n - max(room, 0))
            if pending.count >= flushBytes || Date().timeIntervalSince(lastFlush) >= flushInterval { flush() }
        }
        flush()
        tail = String(decoding: tailBytes, as: UTF8.self)
    }
}
