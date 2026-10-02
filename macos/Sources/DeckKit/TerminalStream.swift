import Foundation

/// **The desk's terminal as a stream** — `WS /v1/agents/{name}/terminal/stream`.
///
/// *"The terminal of each computer should look and feel like a terminal."*
/// The one-shot `POST /terminal` (`DeskShell.swift`) is a fresh `bash -lc` per
/// command, cut off at twenty seconds: vim, top and ssh cannot work in it. This
/// socket is a real PTY on the desk's computer.
///
/// The wire, in full:
/// * text `hello` first: the window, the windows there are, whether he may
///   type (`read_only`) and the grid.
/// * **binary = raw terminal output.** A UTF-8 character or an escape sequence
///   may be split across two messages, so bytes go straight to the emulator and
///   nothing here decodes them.
/// * text `exit` when the shell ends; text `error` (with a `reason`) before the
///   deck closes on a refusal.
/// * this side sends binary = input bytes, text `resize` whenever the grid
///   changes, and text `ack` with the **cumulative** count of output bytes the
///   emulator has processed. The deck stops reading the PTY past 256 KiB
///   unacked, so a client that never acks freezes its own terminal.
///
/// Windows: `deck` is his own bash in a persistent tmux session; `agent` is a
/// read-only mirror of every Bash command the desk's agent runs. Switching is a
/// reconnect with the other `window=`.

public enum TerminalWindow: String, CaseIterable, Sendable {
    /// His own shell, in a tmux session that outlives the socket.
    case deck
    /// What the desk's agent is running, mirrored. Read-only.
    case agent
}

public struct TerminalHello: Equatable, Sendable {
    public let desk: String
    public let window: TerminalWindow
    public let windows: [TerminalWindow]
    public let readOnly: Bool
    public let cols: Int
    public let rows: Int

    public init(desk: String, window: TerminalWindow, windows: [TerminalWindow],
                readOnly: Bool, cols: Int, rows: Int) {
        self.desk = desk
        self.window = window
        self.windows = windows
        self.readOnly = readOnly
        self.cols = cols
        self.rows = rows
    }
}

/// One thing the deck said on the socket.
public enum TerminalStreamEvent: Equatable, Sendable {
    case hello(TerminalHello)
    case output(Data)
    case exit(code: Int)
    case error(reason: String, detail: String)

    /// Binary is output, whole or in pieces. PURE.
    public static func parse(binary data: Data) -> TerminalStreamEvent {
        .output(data)
    }

    /// A text message, or nil for one this client does not know. PURE.
    public static func parse(text: String) -> TerminalStreamEvent? {
        guard let body = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any],
              let type = body["type"] as? String else { return nil }
        switch type {
        case "hello":
            let window = (body["window"] as? String).flatMap(TerminalWindow.init(rawValue:)) ?? .deck
            let windows = (body["windows"] as? [String])?.compactMap(TerminalWindow.init(rawValue:))
            return .hello(TerminalHello(
                desk: body["desk"] as? String ?? "",
                window: window,
                windows: (windows?.isEmpty == false ? windows : nil) ?? TerminalWindow.allCases,
                readOnly: body["read_only"] as? Bool ?? false,
                cols: body["cols"] as? Int ?? 80,
                rows: body["rows"] as? Int ?? 24))
        case "exit":
            return .exit(code: body["code"] as? Int ?? 0)
        case "error":
            return .error(reason: body["reason"] as? String ?? "error",
                          detail: body["detail"] as? String ?? "")
        default:
            return nil
        }
    }
}

/// What this side puts on the socket as text. PURE.
public enum TerminalStreamWire {
    public static let colsRange = 1...500
    public static let rowsRange = 1...200
    /// Ack at least this often. A quarter of the deck's 256 KiB window, so the
    /// PTY is never stalled waiting for a client that has drawn the bytes.
    public static let ackEvery = 32 * 1024

    public static func clamp(cols: Int, rows: Int) -> (cols: Int, rows: Int) {
        (min(max(cols, colsRange.lowerBound), colsRange.upperBound),
         min(max(rows, rowsRange.lowerBound), rowsRange.upperBound))
    }

    public static func resize(cols: Int, rows: Int) -> String {
        let grid = clamp(cols: cols, rows: rows)
        return "{\"type\":\"resize\",\"cols\":\(grid.cols),\"rows\":\(grid.rows)}"
    }

    public static func ack(bytes: Int) -> String {
        "{\"type\":\"ack\",\"bytes\":\(bytes)}"
    }
}

/// The socket ended with this close code (or, for a refused upgrade, `4000 +`
/// the HTTP status — the deck's own convention for a refusal before accept).
public struct TerminalStreamClosed: Error, Equatable, Sendable {
    public let code: Int
    /// True when the deck refused the upgrade itself, so `code - 4000` is an
    /// HTTP status rather than one the deck chose.
    public let handshake: Bool

    public init(code: Int, handshake: Bool = false) {
        self.code = code
        self.handshake = handshake
    }
}

/// Why the terminal is not up, as a reason he can act on.
public enum TerminalStreamFailure: Equatable, Sendable {
    case computerNotRunning(String)
    /// The desk's container has no terminal for the stream (an older image).
    case unavailable(String)
    case unauthorized
    case unknownDesk
    /// The deck itself has no stream route.
    case notOnThisDeck
    case other(reason: String, detail: String)

    /// Only these two keep the old one-command drawer: there is a computer to
    /// run a command on, just no live terminal into it.
    public var fallsBackToOneShot: Bool {
        switch self {
        case .unavailable, .notOnThisDeck: return true
        default: return false
        }
    }

    public var sentence: String {
        switch self {
        case .computerNotRunning(let detail):
            return withDetail("This desk's computer is not running, so there is no terminal to open.", detail)
        case .unavailable(let detail):
            return withDetail("This desk's computer has no live terminal yet. One command at a time still works.", detail)
        case .unauthorized:
            return "The deck rejected this token. Set a different one in Settings."
        case .unknownDesk:
            return "That desk is no longer on this deck."
        case .notOnThisDeck:
            return "This deck has no live terminal yet. One command at a time still works."
        case .other(_, let detail):
            return withDetail("The terminal closed.", detail)
        }
    }

    public static func fromError(reason: String, detail: String) -> TerminalStreamFailure {
        switch reason {
        case "computer_not_running": return .computerNotRunning(detail)
        case "terminal_unavailable": return .unavailable(detail)
        case "unauthorized", "auth_required": return .unauthorized
        case "unknown_agent": return .unknownDesk
        default: return .other(reason: reason, detail: detail)
        }
    }

    /// A refusal close code, or nil for a drop worth reconnecting after.
    public static func fromClose(code: Int) -> TerminalStreamFailure? {
        switch code {
        case 4401, 4403: return .unauthorized
        case 4404: return .unknownDesk
        case 4409: return .computerNotRunning("")
        default: return nil
        }
    }

    /// The upgrade refused with a plain HTTP status. A deck without the route
    /// answers 403 (Starlette's close-before-accept), which is "not here".
    public static func fromHandshake(status: Int) -> TerminalStreamFailure {
        switch status {
        case 401: return .unauthorized
        case 404: return .unknownDesk
        case 409: return .computerNotRunning("")
        default: return .notOnThisDeck
        }
    }
}

private func withDetail(_ sentence: String, _ detail: String) -> String {
    let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? sentence : "\(sentence) \(trimmed)"
}

/// One open socket.
public protocol TerminalStreamSession: AnyObject, Sendable {
    /// The next thing the deck said. Throws (`TerminalStreamClosed` when the
    /// close code is known) once the socket is gone.
    func next() async throws -> TerminalStreamEvent
    func send(input: Data) async throws
    func send(text: String) async throws
    func close()
}

/// A client that can open the terminal stream. `HTTPDeckClient` is one.
public protocol DeskTerminalStreaming: Sendable {
    func openTerminalStream(agent: String, window: TerminalWindow,
                            cols: Int, rows: Int) async throws -> TerminalStreamSession
}

// MARK: - URLSession

/// The socket, on `URLSessionWebSocketTask`, bearer token on the upgrade.
public final class URLSessionTerminalStream: TerminalStreamSession, @unchecked Sendable {
    private let task: URLSessionWebSocketTask

    public init(request: URLRequest, session: URLSession = .shared) {
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 4 * 1024 * 1024
        task.resume()
    }

    public func next() async throws -> TerminalStreamEvent {
        while true {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await task.receive()
            } catch {
                throw closure(of: error)
            }
            switch message {
            case .data(let data):
                return TerminalStreamEvent.parse(binary: data)
            case .string(let text):
                if let event = TerminalStreamEvent.parse(text: text) { return event }
            @unknown default:
                continue
            }
        }
    }

    /// How it ended: the close code when there was one, the refused upgrade's
    /// status when there was not, and the error itself for a plain drop.
    private func closure(of error: Error) -> Error {
        let code = task.closeCode.rawValue
        if code != URLSessionWebSocketTask.CloseCode.invalid.rawValue {
            return TerminalStreamClosed(code: code)
        }
        if let http = task.response as? HTTPURLResponse, http.statusCode != 101 {
            return TerminalStreamClosed(code: 4000 + http.statusCode, handshake: true)
        }
        return error
    }

    public func send(input: Data) async throws {
        try await task.send(.data(input))
    }

    public func send(text: String) async throws {
        try await task.send(.string(text))
    }

    public func close() {
        task.cancel(with: .normalClosure, reason: nil)
    }
}

// MARK: - what the bar says (Mac and phone)

extension TerminalWindow {
    public var title: String {
        switch self {
        case .deck: return "My shell"
        case .agent: return "Agent's commands"
        }
    }
}

extension TerminalStreamState {
    /// The line over the terminal, or nil when the terminal speaks for itself.
    public func caption(desk: String) -> String? {
        switch self {
        case .connecting: return "Connecting to \(desk)'s terminal…"
        case .live: return nil
        case .readOnly: return "Watching what \(desk) runs. Read-only."
        case .ended(let code): return code == 0 ? "The shell ended." : "The shell ended (exit \(code))."
        case .failed(let failure): return failure.sentence
        }
    }

    public var offersRestart: Bool {
        switch self {
        case .ended, .failed: return true
        default: return false
        }
    }
}
