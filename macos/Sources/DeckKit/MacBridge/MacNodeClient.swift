import Foundation

/// What the bridge needs from the deck: the four MB1 node routes. Behind a
/// protocol so the loop is tested against a fake and never opens a socket.
public protocol MacNodeClienting: Sendable {
    /// The deck this client dials (the transport guard reads it).
    var deckURL: URL { get }
    /// `POST /v1/nodes`. Stores a new secret when the server hands one out.
    func register(_ body: MacRegisterRequest) async throws -> MacRegisterResponse
    func poll(nodeId: String, _ body: MacPollRequest) async throws -> MacPollResponse
    func postEvents(nodeId: String, jobId: String, _ events: [MacJobEvent]) async throws -> MacEventsResponse
    func postGrant(nodeId: String, _ body: MacGrantPost) async throws -> MacGrantResponse
    /// Mac control's live view: push one frame of `display`; the answer says
    /// whether anyone still watches, and which displays.
    func postFrame(nodeId: String, jpeg: Data, width: Int, height: Int, display: Int?) async throws -> MacFrameAnswer
    /// His Mac's terminal: dial `WS /v1/nodes/{id}/terminal/mac?session=` OUT.
    func terminalSocket(nodeId: String, session: String) throws -> MacTerminalSocket
}

/// The deck's answer to one live-view frame.
public struct MacFrameAnswer: Decodable, Equatable, Sendable {
    public var watch: Bool
    /// The displays viewers want now; nil from an older deck (keep going).
    public var displays: [Int]?

    enum CodingKeys: String, CodingKey {
        case watch
        case displays = "watch_displays"
    }

    public init(watch: Bool, displays: [Int]? = nil) {
        self.watch = watch
        self.displays = displays
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        watch = (try? c.decodeIfPresent(Bool.self, forKey: .watch)) ?? false
        displays = try? c.decodeIfPresent([Int].self, forKey: .displays)
    }
}

public extension MacNodeClienting {
    /// A client that predates the live view has nobody watching.
    func postFrame(nodeId: String, jpeg: Data, width: Int, height: Int, display: Int?) async throws -> MacFrameAnswer {
        MacFrameAnswer(watch: false)
    }

    func terminalSocket(nodeId: String, session: String) throws -> MacTerminalSocket {
        throw MacNodeError.transport("this client has no terminal")
    }
}

public enum MacNodeError: Error, Equatable, Sendable {
    /// The deck answered with a refusal (`reason` is the slug, or `http_<status>`).
    case refused(status: Int, reason: String, detail: String)
    case transport(String)
    /// A node route before this Mac has a node id + secret.
    case notPaired
    /// No `/v1` bearer token in the Keychain.
    case missingToken
    case decoding(String)

    /// The server no longer knows this Mac's secret: register again.
    public var isUnknownNode: Bool {
        if case .refused(401, "unknown_node", _) = self { return true }
        return false
    }

    /// Worth backing off and retrying (5xx, network), as opposed to a
    /// refusal that will say the same thing next time.
    public var isServerTrouble: Bool {
        switch self {
        case .refused(let status, _, _): return status >= 500
        case .transport: return true
        default: return false
        }
    }

    /// The job is gone or settled on the server: stop reporting it.
    public var jobIsGone: Bool {
        if case .refused(let status, _, _) = self { return [403, 404, 409].contains(status) }
        return false
    }
}

/// **The real client: `URLSession` to MB1, its own session, bearer + node header.**
///
/// Independent of `HTTPDeckClient` and the SSE stream: a slow SSE client loses
/// events, and a lost job would be a hang, so the bridge has its own
/// claim-based long-poll with a 40 s request timeout (the server holds a poll
/// up to 25 s). The node secret lives in the Keychain as
/// `<node_id>.<node_secret>`, under the StateSuite-aware account.
public struct MacNodeClient: MacNodeClienting {
    public static let header = "X-Deck-Node"
    public static let requestTimeout: TimeInterval = 40

    public let deckURL: URL
    private let tokens: TokenStore
    private let secrets: TokenStore
    private let performer: RequestPerformer
    /// The terminal's socket session: pinned like the rest, without the
    /// 40 s request timeout a quiet shell would trip.
    private let sockets: URLSession?

    public init(deckURL: URL, tokens: TokenStore, secrets: TokenStore, performer: RequestPerformer,
                sockets: URLSession? = nil) {
        self.deckURL = deckURL
        self.tokens = tokens
        self.secrets = secrets
        self.performer = performer
        self.sockets = sockets
    }

    /// The saved deck, the saved bearer, this suite's node-secret item, and a
    /// session of the bridge's own (pinned like the rest of the app). Nil
    /// when the app is not connected to a deck yet.
    public static func live(suite: StateSuite = StateSuite()) -> MacNodeClient? {
        guard let saved = DeckConnection(suite: suite).saved else { return nil }
        let session = PinnedTrust.session(pin: saved.pin, configuration: sessionConfiguration()).session
        let sockets = PinnedTrust.session(pin: saved.pin, configuration: socketConfiguration()).session
        return MacNodeClient(deckURL: saved.url, tokens: suite.tokenStore(), secrets: suite.macNodeSecretStore(),
                             performer: URLSessionPerformer(session: session), sockets: sockets)
    }

    public static func socketConfiguration() -> URLSessionConfiguration {
        let c = sessionConfiguration()
        c.timeoutIntervalForRequest = 2 * 3600
        return c
    }

    public static func sessionConfiguration() -> URLSessionConfiguration {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = requestTimeout
        c.httpShouldSetCookies = false
        c.httpCookieAcceptPolicy = .never
        c.urlCache = nil
        return c
    }

    // MARK: routes

    public func register(_ body: MacRegisterRequest) async throws -> MacRegisterResponse {
        let saved = try? secrets.token()
        let r: MacRegisterResponse = try await send("v1/nodes", body: body, node: saved)
        if let secret = r.nodeSecret, !secret.isEmpty {
            do { try secrets.setToken("\(r.nodeId).\(secret)") } catch {
                throw MacNodeError.transport("could not keep this Mac's node secret in the Keychain")
            }
        }
        return r
    }

    public func poll(nodeId: String, _ body: MacPollRequest) async throws -> MacPollResponse {
        try await send("v1/nodes/\(nodeId)/poll", body: body, node: try credential())
    }

    public func postEvents(nodeId: String, jobId: String, _ events: [MacJobEvent]) async throws -> MacEventsResponse {
        try await send("v1/nodes/\(nodeId)/jobs/\(jobId)/events", body: MacEventsRequest(events: events), node: try credential())
    }

    public func postGrant(nodeId: String, _ body: MacGrantPost) async throws -> MacGrantResponse {
        try await send("v1/nodes/\(nodeId)/grants", body: body, node: try credential())
    }

    public func postFrame(nodeId: String, jpeg: Data, width: Int, height: Int,
                          display: Int?) async throws -> MacFrameAnswer {
        guard let token = try? tokens.token(), !token.isEmpty else { throw MacNodeError.missingToken }
        var request = URLRequest(url: url("v1/nodes/\(nodeId)/screen/frame"), timeoutInterval: 15)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = false
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(try credential(), forHTTPHeaderField: Self.header)
        request.setValue(String(width), forHTTPHeaderField: "X-Frame-Width")
        request.setValue(String(height), forHTTPHeaderField: "X-Frame-Height")
        if let display { request.setValue(String(display), forHTTPHeaderField: "X-Frame-Display-Id") }
        request.httpBody = jpeg
        let (data, response): (Data, HTTPURLResponse)
        do { (data, response) = try await performer.perform(request) } catch {
            throw MacNodeError.transport(String(describing: error))
        }
        guard (200..<300).contains(response.statusCode) else {
            let refusal = try? JSONDecoder().decode(MacWireRefusal.self, from: data)
            throw MacNodeError.refused(status: response.statusCode, reason: refusal?.reason ?? "http_\(response.statusCode)",
                                       detail: refusal?.detail ?? "")
        }
        return (try? JSONDecoder().decode(MacFrameAnswer.self, from: data)) ?? MacFrameAnswer(watch: false)
    }

    /// Bearer AND node secret on the upgrade, `ws://` over WireGuard (or
    /// `wss://`). An outbound client socket: nothing on this Mac listens.
    public func terminalRequest(nodeId: String, session: String) throws -> URLRequest {
        guard let token = try? tokens.token(), !token.isEmpty else { throw MacNodeError.missingToken }
        guard var parts = URLComponents(url: url("v1/nodes/\(nodeId)/terminal/mac"), resolvingAgainstBaseURL: false)
        else { throw MacNodeError.transport("bad deck URL") }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        parts.queryItems = [URLQueryItem(name: "session", value: session)]
        guard let wsURL = parts.url else { throw MacNodeError.transport("bad deck URL") }
        var request = URLRequest(url: wsURL)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(try credential(), forHTTPHeaderField: Self.header)
        return request
    }

    public func terminalSocket(nodeId: String, session: String) throws -> MacTerminalSocket {
        URLSessionMacTerminalSocket(request: try terminalRequest(nodeId: nodeId, session: session),
                                    session: sockets ?? URLSession(configuration: Self.socketConfiguration()))
    }

    // MARK: plumbing

    private func credential() throws -> String {
        guard let value = try? secrets.token(), value.contains(".") else { throw MacNodeError.notPaired }
        return value
    }

    private func url(_ path: String) -> URL {
        var base = deckURL.absoluteString
        if !base.hasSuffix("/") { base += "/" }
        return URL(string: base + path) ?? deckURL.appendingPathComponent(path)
    }

    private func send<Body: Encodable, Out: Decodable>(_ path: String, body: Body, node: String?) async throws -> Out {
        guard let token = try? tokens.token(), !token.isEmpty else { throw MacNodeError.missingToken }
        var request = URLRequest(url: url(path), timeoutInterval: Self.requestTimeout)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let node { request.setValue(node, forHTTPHeaderField: Self.header) }
        request.httpBody = try JSONEncoder().encode(body)

        let data: Data, response: HTTPURLResponse
        do {
            (data, response) = try await performer.perform(request)
        } catch is CancellationError {
            throw CancellationError()
        } catch let DeckError.transport(text) {
            throw MacNodeError.transport(text)
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw MacNodeError.transport(error.localizedDescription)
        }
        guard (200..<300).contains(response.statusCode) else {
            let refusal = try? JSONDecoder().decode(MacWireRefusal.self, from: data)
            let reason = refusal?.reason.isEmpty == false ? refusal!.reason : "http_\(response.statusCode)"
            throw MacNodeError.refused(status: response.statusCode, reason: reason, detail: refusal?.detail ?? "")
        }
        do { return try JSONDecoder().decode(Out.self, from: data) } catch {
            throw MacNodeError.decoding(String(describing: error))
        }
    }
}

// MARK: - Where this Mac's identity lives

public extension StateSuite {
    static let macNodeSecretBaseAccount = "mac-node-secret"

    /// `mac-node-secret`, or `mac-node-secret.<suite>` under `DECK_STATE_SUITE`,
    /// so an automated run never overwrites the owner's node secret.
    var macNodeSecretAccount: String {
        name.map { "\(Self.macNodeSecretBaseAccount).\($0)" } ?? Self.macNodeSecretBaseAccount
    }

    func macNodeSecretStore() -> KeychainTokenStore {
        KeychainTokenStore(service: Self.keychainService, account: macNodeSecretAccount)
    }
}

/// `machine_id`: a UUID the app makes once and keeps in
/// `~/Library/Application Support/Agent Deck/node.json` — not a hardware id.
/// A named StateSuite gets `node.<suite>.json`, i.e. a different "Mac".
public struct MacNodeIdentity: Sendable {
    public let fileURL: URL

    public init(directory: String = MacPaths().supportDirectory, suite: StateSuite = StateSuite()) {
        let file = suite.name.map { "node.\($0).json" } ?? "node.json"
        fileURL = URL(fileURLWithPath: directory).appendingPathComponent(file)
    }

    private struct Stored: Codable {
        var machineId: String
        enum CodingKeys: String, CodingKey { case machineId = "machine_id" }
    }

    public func machineId() -> String {
        if let data = try? Data(contentsOf: fileURL),
           let stored = try? JSONDecoder().decode(Stored.self, from: data),
           UUID(uuidString: stored.machineId) != nil {
            return stored.machineId
        }
        let id = UUID().uuidString
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        if let data = try? JSONEncoder().encode(Stored(machineId: id)) {
            try? data.write(to: fileURL, options: .atomic)
        }
        return id
    }

    /// The name desks see and the OS line, within the server's limits
    /// (name ≤ 64 characters, no control characters).
    public static func describe(name: String? = nil) -> (name: String, os: String) {
        #if os(macOS)
        let raw = name ?? Host.current().localizedName ?? "Mac"
        #else
        let raw = name ?? ProcessInfo.processInfo.hostName
        #endif
        let clean = String(String.UnicodeScalarView(raw.unicodeScalars.map {
            CharacterSet.controlCharacters.contains($0) ? " " : $0
        })).split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let v = ProcessInfo.processInfo.operatingSystemVersion
        var os = "macOS \(v.majorVersion).\(v.minorVersion)"
        if v.patchVersion > 0 { os += ".\(v.patchVersion)" }
        let full = ProcessInfo.processInfo.operatingSystemVersionString
        if let r = full.range(of: "Build ") {
            os += " (\(full[r.upperBound...].trimmingCharacters(in: CharacterSet(charactersIn: ")"))))"
        }
        return (String((clean.isEmpty ? "Mac" : clean).prefix(64)), os)
    }
}
