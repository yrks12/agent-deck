import Foundation

// The contract between the bridge and whatever runs a job. It stays in DeckKit;
// the implementation (`MacExecutor.swift`) depends on nothing but this file and
// the OS, so a later slice can move it into its own target that an App Store
// build does not link. The bridge (A2) receives a `MacJobExecuting` by
// injection and never names `MacExecutor`.

public enum MacStream: String, Sendable { case stdout, stderr }

/// One `run` job, after the policy said yes.
public struct MacRunSpec: Sendable {
    public var command: String
    /// Resolved working directory; nil → the user's temp dir.
    public var cwd: String?
    /// The job's own env (≤ 32 keys); cannot override the `AGENT_DECK_*` keys.
    public var env: [String: String]
    public var desk: String
    public var jobId: String
    public var timeout: TimeInterval
    /// SBPL text for `sandbox-exec -p` (`MacSeatbeltProfile.text`); nil →
    /// unconfined (Full access, or the fallback grant).
    public var sandboxProfile: String?
    /// nil → the user's login shell.
    public var shell: String?
    /// Resolved folders the job may use (to tell TCC from our own sandbox).
    public var folders: [String]
    public var killGrace: TimeInterval = 3
    public var flushInterval: TimeInterval = 0.5
    public var flushBytes = 32 * 1024
    public var captureCap = 8 << 20

    public init(command: String, cwd: String? = nil, env: [String: String] = [:], desk: String, jobId: String,
                timeout: TimeInterval = 120, sandboxProfile: String? = nil, shell: String? = nil, folders: [String] = []) {
        self.command = command
        self.cwd = cwd
        self.env = env
        self.desk = desk
        self.jobId = jobId
        self.timeout = timeout
        self.sandboxProfile = sandboxProfile
        self.shell = shell
        self.folders = folders
    }
}

public struct MacRunResult: Equatable, Sendable {
    public enum State: String, Sendable { case done, failed, cancelled, timedOut = "timed_out" }
    public var state: State
    public var exit: Int32?
    /// "TERM" / "KILL" / other signal name when the shell died of a signal.
    public var signal: String?
    public var reason: String?
    public var detail: String
    public var durationMs: Int
    public var sandboxed: Bool
    public var pid: Int32
    public var cwd: String
    public var stdoutBytes: Int
    public var stderrBytes: Int
    /// Output beyond the local cap was read and dropped.
    public var truncated: Bool

    public init(state: State, exit: Int32?, signal: String?, reason: String?, detail: String, durationMs: Int,
                sandboxed: Bool, pid: Int32, cwd: String, stdoutBytes: Int, stderrBytes: Int, truncated: Bool) {
        self.state = state
        self.exit = exit
        self.signal = signal
        self.reason = reason
        self.detail = detail
        self.durationMs = durationMs
        self.sandboxed = sandboxed
        self.pid = pid
        self.cwd = cwd
        self.stdoutBytes = stdoutBytes
        self.stderrBytes = stderrBytes
        self.truncated = truncated
    }
}

/// A job that has been started.
public protocol MacJobRun: AnyObject, Sendable {
    var pid: Int32 { get }
    var sandboxed: Bool { get }
    var cwd: String { get }
    /// Stop now (desk cancel, Stop in the bar, Quit): TERM the group, KILL after the grace.
    func cancel()
    /// Blocks until the job has settled.
    func wait() -> MacRunResult
    /// Called once, off the main thread, when the job settles (at once if it already has).
    func onFinish(_ body: @escaping @Sendable (MacRunResult) -> Void)
}

/// What the bridge is given to run jobs. A spawn failure is a run that has
/// already settled `failed`, never a throw.
public protocol MacJobExecuting: Sendable {
    func start(_ spec: MacRunSpec, output: @escaping @Sendable (MacStream, Data) -> Void) -> any MacJobRun
}

/// Folders macOS guards with TCC ("Files and Folders"): an `EPERM` there is
/// macOS privacy, not our policy, and the desk is told where he must click.
public enum MacTCC {
    public static let homeFolders = ["Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music",
                                     "Library/Mobile Documents"]

    /// The TCC-guarded folder `text` names, if any.
    public static func folder(in text: Substring, home: String) -> String? {
        for f in homeFolders where text.contains(home + "/" + f) { return home + "/" + f }
        return text.contains("/Volumes/") ? "/Volumes" : nil
    }

    public static func detail(_ folder: String) -> String {
        "macOS blocked Agent Deck from \(folder). The owner must allow it in System Settings → Privacy & Security → Files and Folders (or Full Disk Access) → Agent Deck, then you can retry."
    }
}

// MARK: - Mac control

/// Posts one gesture on this Mac. Throws `MacOpError` with the reason a desk
/// can act on: `owner_active` (he is using it), `secure_field` (a password
/// field has focus), `tcc_denied` (no Accessibility permission).
public protocol MacInputPerforming: Sendable {
    func perform(_ gesture: MacInputGesture) async throws
}

/// The live view: capture and push frames while someone watches.
public protocol MacScreenStreaming: Sendable {
    /// Someone is watching: capture until the deck
    /// says nobody is, or `stop()`.
    func start(nodeId: String) async
    func stop() async
}

/// What this Mac allows right now, for the poll.
public struct MacControlProbe: Sendable {
    public var perms: MacPermissions?
    public var screen: MacSpace?

    public init(perms: MacPermissions? = nil, screen: MacSpace? = nil) {
        self.perms = perms
        self.screen = screen
    }
}
