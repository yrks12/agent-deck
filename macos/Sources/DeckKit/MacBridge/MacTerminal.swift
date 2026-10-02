import Foundation

/// **His Mac's terminal, opened from his phone or his other Mac.**
///
/// The Mac never listens, so the deck cannot dial it. A viewer opens
/// `WS /v1/nodes/{id}/terminal/stream` on the deck; the next long-poll answers
/// with `terminal: {session, cols, rows, from}`; this Mac dials
/// `WS /v1/nodes/{id}/terminal/mac?session=` outbound — bearer plus its node
/// secret, over the same WireGuard link as every poll — and the deck pipes the
/// two together (`server/mac_terminal.py`).
///
/// The wire is the desks' terminal wire (`TerminalStream.swift`), spoken from
/// the other end: binary is raw PTY bytes both ways; this side says `hello`
/// first and `exit` or `error` last; the viewer sends `resize` and cumulative
/// `ack`s. Bytes are passed and forgotten: nothing here logs a keystroke.
///
/// Only in Full access (`MacTerminalGate`), checked here as well as on the
/// deck. Agents never reach it: only an offer from the owner's viewer starts
/// one, and the `mac` tools have no terminal.
public struct MacTerminalOffer: Decodable, Equatable, Sendable {
    public var session: String
    public var cols: Int
    public var rows: Int
    /// "iPhone", "Mac" or "another device": for the banner.
    public var from: String

    public init(session: String, cols: Int = 80, rows: Int = 24, from: String = "iPhone") {
        self.session = session
        self.cols = cols
        self.rows = rows
        self.from = from
    }

    enum CodingKeys: String, CodingKey { case session, cols, rows, from }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        session = try c.decode(String.self, forKey: .session)
        let grid = TerminalStreamWire.clamp(cols: (try? c.decode(Int.self, forKey: .cols)) ?? 80,
                                            rows: (try? c.decode(Int.self, forKey: .rows)) ?? 24)
        cols = grid.cols
        rows = grid.rows
        from = (try? c.decode(String.self, forKey: .from)) ?? "another device"
    }
}

/// The one rule: his Mac's terminal opens only in Full access.
public enum MacTerminalGate {
    public static let copy = "Turn on Full access on your Mac (Shaliach → Settings → Mac) to use its terminal."

    /// The sentence to send back, or nil when it may open. PURE.
    public static func refusal(mode: MacMode) -> String? { mode == .full ? nil : copy }
}

/// How long a plain PTY waits for the next viewer once one leaves.
public enum MacPTYShellGrace {
    public static let seconds: TimeInterval = 5 * 60
}

/// The open session, for the banner and the hotkey.
public struct MacTerminalOpen: Equatable, Sendable {
    public var session: String
    public var from: String
    public var startedAt: Date

    public init(session: String, from: String, startedAt: Date) {
        self.session = session
        self.from = from
        self.startedAt = startedAt
    }

    /// "Terminal open from iPhone".
    public var banner: String { "Terminal open from \(from)" }
}

/// Why a session ended: the activity row says it.
public enum MacTerminalEnd: String, Equatable, Sendable {
    case viewerLeft = "the viewer left"
    case stopped = "stopped on this Mac"
    case idle = "idle for 30 minutes"
    case shellExited = "the shell exited"
    case fullAccessOff = "Full access was turned off"
    case failed = "the connection failed"

    /// Stop and Full access off end the shell too; the rest only detach it.
    public var killsShell: Bool { self == .stopped || self == .fullAccessOff }
}

/// One message on the socket.
public enum MacTerminalFrame: Equatable, Sendable {
    case bytes(Data)
    case text(String)
}

/// The Mac's end of the socket. `URLSessionMacTerminalSocket` is the real one.
public protocol MacTerminalSocket: AnyObject, Sendable {
    func receive() async throws -> MacTerminalFrame
    func send(_ frame: MacTerminalFrame) async throws
    func close()
}

/// What the viewer may say in text. PURE.
public enum MacTerminalControl: Equatable, Sendable {
    case resize(cols: Int, rows: Int)
    case ack(Int)

    public static func parse(_ text: String) -> MacTerminalControl? {
        guard let body = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] else { return nil }
        switch body["type"] as? String {
        case "resize":
            guard let c = body["cols"] as? Int, let r = body["rows"] as? Int,
                  (1...500).contains(c), (1...200).contains(r) else { return nil }
            return .resize(cols: c, rows: r)
        case "ack":
            guard let n = body["bytes"] as? Int, n >= 0 else { return nil }
            return .ack(n)
        default:
            return nil
        }
    }

    static func json(_ object: [String: Any]) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    /// The desks' `hello`: one window, his own, typed into.
    public static func hello(name: String, cols: Int, rows: Int) -> String {
        json(["type": "hello", "desk": name, "window": "deck", "windows": ["deck"], "read_only": false,
              "cols": cols, "rows": rows])
    }

    public static func exit(code: Int) -> String { json(["type": "exit", "code": code]) }

    public static func error(reason: String, detail: String) -> String {
        json(["type": "error", "reason": reason, "detail": detail])
    }
}

/// The shell on the PTY, as a session sees it. `MacPTYShell` is the real one.
public protocol MacTerminalShelling: AnyObject, Sendable {
    /// Output since the last call, or nil once the shell is gone. Returns
    /// only while the viewer has acked enough (`MacPTYShell.window`).
    func next() async -> Data?
    func write(_ data: Data)
    func resize(cols: Int, rows: Int)
    /// The viewer processed this many bytes in total.
    func acked(_ total: Int)
    /// Still running?
    var isAlive: Bool { get }
    /// A plain PTY (no tmux) is parked after the viewer leaves, for a reattach.
    var reattachable: Bool { get }
    /// The viewer left: keep the shell, stop handing out output.
    func detach()
    /// A new viewer: what the parked shell printed meanwhile (plain PTY only).
    func reattach() -> Data
    /// Hang up; `kill` ends a tmux session too.
    func end(kill: Bool)
}

/// Makes the shell for an offer. nil on a build without a PTY.
public typealias MacTerminalShellMaker = @Sendable (_ cols: Int, _ rows: Int) throws -> any MacTerminalShelling

/// **One viewer's session: the socket and the shell, until either goes.**
public actor MacTerminalSession {
    public static let idleLimit: TimeInterval = 30 * 60

    private let socket: MacTerminalSocket
    private let shell: MacTerminalShelling
    private let idle: TimeInterval
    private let tick: TimeInterval
    private let now: @Sendable () -> Date
    private var last: Date
    private var reason: MacTerminalEnd?

    public init(socket: MacTerminalSocket, shell: MacTerminalShelling, idle: TimeInterval = idleLimit,
                tick: TimeInterval = 30, now: @escaping @Sendable () -> Date = { Date() }) {
        self.socket = socket
        self.shell = shell
        self.idle = idle
        self.tick = tick
        self.now = now
        last = now()
    }

    /// Say hello (and what a reattach missed), then pipe until one side ends.
    public func run(hello: String, replay: Data = Data()) async -> MacTerminalEnd {
        do {
            try await socket.send(.text(hello))
            if !replay.isEmpty { try await socket.send(.bytes(replay)) }
        } catch {
            return reason ?? .failed
        }
        let socket = self.socket, shell = self.shell
        let first: MacTerminalEnd = await withTaskGroup(of: MacTerminalEnd.self) { group in
            group.addTask {   // the shell -> the viewer
                while let chunk = await shell.next() {
                    do { try await socket.send(.bytes(chunk)) } catch { return .viewerLeft }
                    await self.touch()
                }
                return .shellExited
            }
            group.addTask {   // the viewer -> the shell
                while true {
                    guard let frame = try? await socket.receive() else { return .viewerLeft }
                    switch frame {
                    case .bytes(let data):
                        shell.write(data)
                        await self.touch()
                    case .text(let text):
                        switch MacTerminalControl.parse(text) {
                        case .resize(let c, let r)?: shell.resize(cols: c, rows: r)
                        case .ack(let n)?: shell.acked(n)
                        case nil: break
                        }
                    }
                }
            }
            group.addTask {   // the idle clock
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: UInt64(self.tick * 1e9))
                    if await self.idleFor() >= self.idle { return .idle }
                }
                return .viewerLeft
            }
            let out = await group.next() ?? .failed
            group.cancelAll()
            if out == .shellExited { try? await socket.send(.text(MacTerminalControl.exit(code: 0))) }
            socket.close()   // the receiver is parked in `receive`: this frees it
            return out
        }
        return reason ?? first
    }

    /// Stop (banner, ⌃⌥⌘.) or Full access off: tell the viewer and hang up.
    public func stop(_ why: MacTerminalEnd) async {
        guard reason == nil else { return }
        reason = why
        let detail = why == .fullAccessOff ? MacTerminalGate.copy : "Stopped on the Mac."
        try? await socket.send(.text(MacTerminalControl.error(reason: why == .fullAccessOff ? "full_access_required"
                                                                                           : "stopped",
                                                               detail: detail)))
        socket.close()
    }

    private func touch() { last = now() }
    private func idleFor() -> TimeInterval { now().timeIntervalSince(last) }
}

// MARK: - URLSession

/// The Mac's outbound socket to the deck, on `URLSessionWebSocketTask`.
/// A client task: nothing on this Mac listens.
public final class URLSessionMacTerminalSocket: MacTerminalSocket, @unchecked Sendable {
    private let task: URLSessionWebSocketTask

    public init(request: URLRequest, session: URLSession) {
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 4 * 1024 * 1024
        task.resume()
    }

    public func receive() async throws -> MacTerminalFrame {
        switch try await task.receive() {
        case .data(let data): return .bytes(data)
        case .string(let text): return .text(text)
        @unknown default: return .text("")
        }
    }

    public func send(_ frame: MacTerminalFrame) async throws {
        switch frame {
        case .bytes(let data): try await task.send(.data(data))
        case .text(let text): try await task.send(.string(text))
        }
    }

    public func close() { task.cancel(with: .normalClosure, reason: nil) }
}
