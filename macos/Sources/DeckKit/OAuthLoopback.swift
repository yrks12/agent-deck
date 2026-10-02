import Foundation
import Network

/// **Where the browser comes back after an OAuth Connect.**
///
/// The provider redirects to `http://127.0.0.1:47689/callback?code&state`
/// (`docs/connectors.md`, "OAuth: Connect"). This is the pure half: what one
/// request line means, what the browser is answered, and whether a pasted
/// address is a callback. `OAuthLoopbackListener` is the socket.
///
/// The code and state are passed through to the deck and nowhere else: the
/// page shown in the browser names neither, and nothing here logs them.
public enum OAuthLoopback {
    public static let defaultPort: UInt16 = 47689
    public static let path = "/callback"

    public enum Request: Hashable, Sendable {
        /// The sign-in came back (with a code, or a provider refusal).
        case callback(URL)
        /// Not the callback: answered with this status, and the wait goes on.
        case ignored(status: Int)
    }

    /// The listener's port, from the deck's `redirect_uri`. Nil for anything
    /// that is not plain http on IPv4 loopback — this Mac could not catch it.
    public static func port(fromRedirectURI uri: String) -> UInt16? {
        guard let url = URL(string: uri), url.scheme == "http", url.host == "127.0.0.1",
              let port = url.port, let value = UInt16(exactly: port) else { return nil }
        return value
    }

    /// One request head (`GET /callback?code=…&state=… HTTP/1.1` and headers).
    public static func parse(requestHead: String, port: UInt16) -> Request {
        // "\r\n" is one Character in Swift, and a newline.
        guard let line = requestHead.split(whereSeparator: \.isNewline).first else {
            return .ignored(status: 400)
        }
        let parts = line.split(separator: " ")
        guard parts.count == 3, parts[2].hasPrefix("HTTP/") else { return .ignored(status: 400) }
        guard parts[0] == "GET" else { return .ignored(status: 405) }
        let target = String(parts[1])
        guard let url = URL(string: "http://127.0.0.1:\(port)\(target)"),
              url.path == path, isCallback(url) else { return .ignored(status: 404) }
        return .callback(url)
    }

    /// A callback names its state and either a code or a refusal.
    static func isCallback(_ url: URL) -> Bool {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func has(_ name: String) -> Bool { items.contains { $0.name == name && !($0.value ?? "").isEmpty } }
        return has("state") && (has("code") || has("error"))
    }

    static func errorText(_ url: URL) -> String? {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .first { $0.name == "error" }?.value
    }

    /// The whole HTTP answer, headers and page.
    public static func response(for request: Request) -> Data {
        let status: String
        let title: String
        let line: String
        switch request {
        case .callback(let url):
            status = "200 OK"
            if let error = errorText(url) {
                title = "Not connected"
                line = "The sign-in was not completed: \(escape(String(error.prefix(120)))). "
                    + "You can close this tab and try Connect again in Shaliach."
            } else {
                title = "Connected — you can close this tab"
                line = "Shaliach is finishing the install."
            }
        case .ignored(let code):
            status = code == 405 ? "405 Method Not Allowed" : code == 400 ? "400 Bad Request" : "404 Not Found"
            title = "Not here"
            line = "This address only answers Shaliach's sign-in."
        }
        let page = """
        <!doctype html><html><head><meta charset="utf-8"><title>Shaliach</title>
        <style>body{font:16px -apple-system,system-ui,sans-serif;margin:15vh auto;max-width:28em;text-align:center;color:#222}
        @media(prefers-color-scheme:dark){body{background:#1e1e1e;color:#eee}}</style></head>
        <body><h1>\(title)</h1><p>\(line)</p></body></html>
        """
        let body = Data(page.utf8)
        let head = "HTTP/1.1 \(status)\r\nContent-Type: text/html; charset=utf-8\r\n"
            + "Content-Length: \(body.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        return Data(head.utf8) + body
    }

    static func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }

    /// The paste fallback: the address his browser ended on, trimmed, when it
    /// is a callback the deck can match; nil otherwise.
    public static func pastedCallbackURL(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme == "http" || url.scheme == "https",
              url.path.hasSuffix(path), isCallback(url) else { return nil }
        return trimmed
    }
}

public enum OAuthLoopbackError: Error, Hashable, Sendable {
    /// Something else holds the port (another sign-in, another app).
    case portUnavailable(String)
    case timedOut
    case cancelled
}

/// **One loopback listener for one sign-in.** Binds `127.0.0.1:<port>` only
/// (IPv4 loopback, so nothing off this Mac can reach it), answers every
/// request, and stops after the first real callback, at the deadline, or on
/// `cancel()`.
public final class OAuthLoopbackListener: @unchecked Sendable {
    public let port: UInt16
    private let queue = DispatchQueue(label: "deck.oauth-loopback")
    private let lock = NSLock()
    private var listener: NWListener?
    private var outcome: Result<URL, OAuthLoopbackError>?
    private var waiter: CheckedContinuation<URL, Error>?

    public init(port: UInt16 = OAuthLoopback.defaultPort) {
        self.port = port
    }

    deinit { listener?.cancel() }

    /// Returns once the port is held — open the browser after this, not before.
    public func start() async throws {
        guard let nwPort = NWEndpoint.Port(rawValue: port) else {
            throw OAuthLoopbackError.portUnavailable("port \(port)")
        }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: nwPort)
        parameters.allowLocalEndpointReuse = false
        let listener: NWListener
        do {
            listener = try NWListener(using: parameters)
        } catch {
            throw OAuthLoopbackError.portUnavailable(String(describing: error))
        }
        lock.lock(); self.listener = listener; lock.unlock()

        final class Once: @unchecked Sendable { var done = false }
        let once = Once()
        try await withCheckedThrowingContinuation { (ready: CheckedContinuation<Void, Error>) in
            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    if !once.done { once.done = true; ready.resume() }
                case .failed(let error), .waiting(let error):
                    if !once.done {
                        once.done = true
                        listener.cancel()
                        ready.resume(throwing: OAuthLoopbackError.portUnavailable(String(describing: error)))
                    } else {
                        self?.finish(.failure(.portUnavailable(String(describing: error))))
                    }
                case .cancelled:
                    if !once.done { once.done = true; ready.resume(throwing: OAuthLoopbackError.cancelled) }
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in self?.serve(connection) }
            listener.start(queue: queue)
        }
    }

    /// The callback URL, or `.timedOut` at `deadline`.
    public func callback(until deadline: Date) async throws -> URL {
        let timeout = Task { [weak self] in
            let seconds = max(0, deadline.timeIntervalSinceNow)
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            if !Task.isCancelled { self?.finish(.failure(.timedOut)) }
        }
        defer { timeout.cancel() }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
                lock.lock()
                if let outcome {
                    lock.unlock()
                    continuation.resume(with: outcome.mapError { $0 as Error })
                } else {
                    waiter = continuation
                    lock.unlock()
                }
            }
        } onCancel: {
            self.finish(.failure(.cancelled))
        }
    }

    public func cancel() { finish(.failure(.cancelled)) }

    private func finish(_ result: Result<URL, OAuthLoopbackError>) {
        lock.lock()
        guard outcome == nil else { lock.unlock(); return }
        outcome = result
        let waiting = waiter
        waiter = nil
        let listener = self.listener
        lock.unlock()
        listener?.cancel()
        waiting?.resume(with: result.mapError { $0 as Error })
    }

    private func serve(_ connection: NWConnection) {
        connection.start(queue: queue)
        read(connection, buffer: Data())
    }

    private static let headLimit = 16 * 1024

    private func read(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] data, _, isComplete, error in
            guard let self else { connection.cancel(); return }
            var buffer = buffer
            if let data { buffer.append(data) }
            let ended = buffer.range(of: Data("\r\n\r\n".utf8)) != nil
            if !ended && error == nil && !isComplete && buffer.count < Self.headLimit {
                return self.read(connection, buffer: buffer)
            }
            let request = OAuthLoopback.parse(requestHead: String(decoding: buffer, as: UTF8.self),
                                              port: self.port)
            connection.send(content: OAuthLoopback.response(for: request),
                            completion: .contentProcessed { _ in
                connection.cancel()
                if case .callback(let url) = request { self.finish(.success(url)) }
            })
        }
    }
}
