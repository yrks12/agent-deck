#if os(macOS)
import Darwin
import Foundation

/// **His login shell on a PTY on this Mac** — in tmux when tmux is here.
///
/// With tmux the shell lives in the `shaliach` session: a dropped socket or a
/// closed tab detaches, and the next viewer attaches to the same shell, as the
/// desks' terminals do. Without tmux it is a plain PTY that stays for
/// `MacPTYShellGrace.seconds` after the viewer goes, keeping its last `ringMax` bytes of
/// output to replay to the next one.
///
/// `posix_spawn` with `SETSID` and the slave opened in the child makes the PTY
/// the shell's controlling terminal, so a resize reaches it as SIGWINCH.
/// `CLOEXEC_DEFAULT`: the shell inherits no descriptor of the app's (none of
/// its sockets, none of its files). Nothing here listens: tmux's own server is
/// a Unix socket under `$TMPDIR`, local to this user.
///
/// Backpressure: output is read only while the viewer has acked within
/// `window` bytes, so a phone on a slow link stops the reading, the PTY fills
/// and the shell slows down — nothing buffers without end.
public final class MacPTYShell: MacTerminalShelling, @unchecked Sendable {
    public static let window = 256 * 1024
    public static let ringMax = 64 * 1024
    public static let sessionName = "shaliach"
    static let tmuxPlaces = ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"]

    public let pid: pid_t
    public let tmux: String?
    private let master: Int32
    private let cond = NSCondition()
    private var pending = Data()
    private var finished = false
    private var waiter: CheckedContinuation<Data?, Never>?
    private var handed = 0
    private var ackedBytes = 0
    private var attached = true
    private var ring = Data()

    /// The first tmux on this Mac, or nil.
    public static func findTmux(_ isExecutable: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) })
        -> String? {
        tmuxPlaces.first(where: isExecutable)
    }

    /// His login shell: the account's, then `$SHELL`, then zsh.
    public static func loginShell() -> String {
        if let pw = getpwuid(getuid()), let raw = pw.pointee.pw_shell {
            let s = String(cString: raw)
            if !s.isEmpty { return s }
        }
        return ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
    }

    /// What runs on the PTY. PURE.
    public static func argv(shell: String, tmux: String?) -> [String] {
        guard let tmux else { return [shell, "-l"] }
        return [tmux, "-u", "new-session", "-A", "-s", sessionName, shell, "-l"]
    }

    public init(cols: Int, rows: Int, shell: String = MacPTYShell.loginShell(),
                tmux: String? = MacPTYShell.findTmux(), home: String = NSHomeDirectory()) throws {
        var m: Int32 = -1, s: Int32 = -1
        guard openpty(&m, &s, nil, nil, nil) == 0 else { throw POSIXError(.ENOTTY) }
        let path = String(cString: ptsname(m))
        // The slave stays open here until the child has it: the last close
        // of a tty resets it, size and all (MEASURED: `stty size` said 0 0).
        defer { close(s) }
        Self.setSize(m, cols: cols, rows: rows)
        var fa: posix_spawn_file_actions_t?
        var attr: posix_spawnattr_t?
        posix_spawn_file_actions_init(&fa)
        posix_spawnattr_init(&attr)
        defer {
            posix_spawn_file_actions_destroy(&fa)
            posix_spawnattr_destroy(&attr)
        }
        posix_spawn_file_actions_addopen(&fa, 0, path, O_RDWR, 0)
        posix_spawn_file_actions_adddup2(&fa, 0, 1)
        posix_spawn_file_actions_adddup2(&fa, 0, 2)
        posix_spawn_file_actions_addchdir_np(&fa, home)
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT))
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"
        env["SHALIACH_TERMINAL"] = "1"
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        let args = Self.argv(shell: shell, tmux: tmux)
        let cArgs = args.map { strdup($0) } + [nil]
        let cEnv = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            cArgs.forEach { free($0) }
            cEnv.forEach { free($0) }
        }
        var child: pid_t = 0
        let rc = posix_spawn(&child, args[0], &fa, &attr, cArgs, cEnv)
        guard rc == 0 else {
            close(m)
            throw POSIXError(POSIXErrorCode(rawValue: rc) ?? .EIO)
        }
        pid = child
        master = m
        self.tmux = tmux
        let reader = Thread { [self] in readLoop() }
        reader.name = "shaliach.terminal.read"
        reader.start()
    }

    static func setSize(_ fd: Int32, cols: Int, rows: Int) {
        var ws = winsize(ws_row: UInt16(max(1, rows)), ws_col: UInt16(max(1, cols)), ws_xpixel: 0, ws_ypixel: 0)
        _ = ioctl(fd, TIOCSWINSZ, &ws)
    }

    // MARK: the reader thread

    private func readLoop() {
        var buf = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            cond.lock()
            while attached && handed + pending.count - ackedBytes >= Self.window && !finished { cond.wait() }
            cond.unlock()
            let n = read(master, &buf, buf.count)
            if n < 0 && errno == EINTR { continue }
            cond.lock()
            if n <= 0 {
                finished = true
                let w = waiter
                waiter = nil
                cond.broadcast()
                cond.unlock()
                w?.resume(returning: nil)
                break
            }
            let chunk = Data(buf[0..<n])
            if tmux == nil {
                ring.append(chunk)
                if ring.count > Self.ringMax { ring.removeFirst(ring.count - Self.ringMax) }
            }
            var deliver: (CheckedContinuation<Data?, Never>, Data)?
            if attached {
                if let w = waiter {
                    waiter = nil
                    handed += chunk.count
                    deliver = (w, chunk)
                } else {
                    pending.append(chunk)
                }
            }
            cond.unlock()
            if let (w, d) = deliver { w.resume(returning: d) }
        }
        close(master)
        var status: Int32 = 0
        _ = waitpid(pid, &status, 0)
    }

    // MARK: MacTerminalShelling

    public func next() async -> Data? {
        await withTaskCancellationHandler {
            await withCheckedContinuation { (c: CheckedContinuation<Data?, Never>) in
                cond.lock()
                if !pending.isEmpty {
                    let d = pending
                    pending = Data()
                    handed += d.count
                    cond.broadcast()
                    cond.unlock()
                    c.resume(returning: d)
                } else if finished || Task.isCancelled {
                    cond.unlock()
                    c.resume(returning: nil)
                } else {
                    waiter = c
                    cond.unlock()
                }
            }
        } onCancel: {
            cond.lock()
            let w = waiter
            waiter = nil
            cond.unlock()
            w?.resume(returning: nil)
        }
    }

    public func write(_ data: Data) {
        data.withUnsafeBytes { raw in
            guard var p = raw.baseAddress else { return }
            var left = raw.count
            while left > 0 {
                let n = Darwin.write(master, p, left)
                if n < 0 { if errno == EINTR || errno == EAGAIN { continue } else { return } }
                left -= n
                p += n
            }
        }
    }

    public func resize(cols: Int, rows: Int) { Self.setSize(master, cols: cols, rows: rows) }

    public func acked(_ total: Int) {
        cond.lock()
        ackedBytes = max(ackedBytes, min(total, handed))
        cond.broadcast()
        cond.unlock()
    }

    public var reattachable: Bool { tmux == nil }

    public var isAlive: Bool {
        cond.lock(); defer { cond.unlock() }
        return !finished
    }

    // MARK: lifecycle

    /// The viewer left: keep reading into the ring, unblocked by acks.
    public func detach() {
        cond.lock()
        attached = false
        pending = Data()
        cond.broadcast()
        cond.unlock()
    }

    /// A new viewer on the parked plain shell: what it missed, and a fresh count.
    public func reattach() -> Data {
        cond.lock(); defer { cond.unlock() }
        attached = true
        handed = 0
        ackedBytes = 0
        return ring
    }

    /// Hang up. `kill` ends the tmux session too (Stop, Full access off);
    /// without it the tmux client only detaches and the shell stays.
    public func end(kill: Bool) {
        detach()
        if kill, let tmux {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: tmux)
            p.arguments = ["kill-session", "-t", Self.sessionName]
            try? p.run()
            p.waitUntilExit()
        }
        Darwin.kill(-pid, SIGHUP)
        let pid = self.pid
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { [weak self] in
            if self?.isAlive ?? false { Darwin.kill(-pid, SIGKILL) }
        }
    }
}
#endif
