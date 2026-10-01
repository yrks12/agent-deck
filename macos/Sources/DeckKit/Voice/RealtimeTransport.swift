import Foundation

/// **The socket to the Realtime API**, behind a protocol so every event rule
/// is tested against a fake instead of OpenAI.
public protocol RealtimeTransport: AnyObject, Sendable {
    /// Open the socket. Returns once the request is under way; a refused
    /// connection surfaces as the first `receive()` throwing.
    func connect(url: URL, secret: String) async throws
    func send(_ text: String) async throws
    /// The next text frame. Throws once the socket is closed.
    func receive() async throws -> String
    func close()
}

/// `URLSessionWebSocketTask`, authenticated the way the deck's mint expects:
/// subprotocols `realtime` and `openai-insecure-api-key.<ephemeral secret>`.
/// (Verified against the live deck: that handshake receives
/// `session.created` with the desk's tools.) The secret is short-lived and
/// scoped to this one call — it is not the deck's API key.
public final class URLSessionRealtimeTransport: RealtimeTransport, @unchecked Sendable {
    private let session: URLSession
    private let lock = NSLock()
    private var task: URLSessionWebSocketTask?

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func connect(url: URL, secret: String) async throws {
        let task = session.webSocketTask(with: url, protocols: ["realtime", "openai-insecure-api-key.\(secret)"])
        task.maximumMessageSize = 16 * 1024 * 1024
        store(task)
        task.resume()
    }

    public func send(_ text: String) async throws {
        guard let task = current() else { throw DeckError.transport("the call's socket is closed") }
        try await task.send(.string(text))
    }

    public func receive() async throws -> String {
        guard let task = current() else { throw DeckError.transport("the call's socket is closed") }
        switch try await task.receive() {
        case .string(let text): return text
        case .data(let data): return String(decoding: data, as: UTF8.self)
        @unknown default: return ""
        }
    }

    public func close() {
        lock.lock(); let task = self.task; self.task = nil; lock.unlock()
        task?.cancel(with: .normalClosure, reason: nil)
    }

    private func store(_ task: URLSessionWebSocketTask) {
        lock.lock(); self.task = task; lock.unlock()
    }

    private func current() -> URLSessionWebSocketTask? {
        lock.lock(); defer { lock.unlock() }
        return task
    }
}
