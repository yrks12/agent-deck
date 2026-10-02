import Foundation

/// Everything that can go wrong redeeming a pairing code, each with its own
/// sentence. Separate from `DeckError` (a closed enum shared with the wire
/// contract) so adding these does not touch files other slices own.
public enum PairingFailure: Error, Equatable, Sendable {
    case code(PairingCodeError)
    case malformed
    case unknownCode
    case expired
    case alreadyUsed
    case rateLimited(retryAfter: Int?)
    /// The server answered with a TLS key that is not the one in the code.
    case pinMismatch
    case badResponse
    case refused(status: Int, reason: String)
    case transport(String)

    public var userFacingText: String {
        switch self {
        case .code(let e): return e.userFacingText
        case .malformed:
            return "The server could not read that code. Copy the whole line and try again."
        case .unknownCode:
            return "The server does not know that code. Run the pairing command on the server again."
        case .expired:
            return "That code has expired (they last 15 minutes). Run the pairing command on the server for a new one."
        case .alreadyUsed:
            return "That code was already used. Each code works once; run the pairing command on the server for a new one."
        case .rateLimited(let after):
            let wait = after.map { " Try again in \(max(1, ($0 + 59) / 60)) minute(s)." } ?? " Try again in a few minutes."
            return "Too many attempts from this network." + wait
        case .pinMismatch:
            return "This is not the server the code was made for, so nothing was sent. Check the address, or make a new code on the server."
        case .badResponse:
            return "The server answered, but not with a pairing this app understands. Is it the same Agent Deck version?"
        case .refused(let status, _):
            return "The server refused the pairing (\(status))."
        case .transport(let detail):
            return "Could not reach the server: \(detail)"
        }
    }
}

public struct PairedDeck: Equatable, Sendable {
    public let url: URL
    public let name: String
    public let version: String
    public let deviceID: String
}

/// What is stored on this Mac about the server (never the token).
public struct SavedConnection: Equatable, Sendable {
    public let url: URL
    /// `""`-stored means CA mode; surfaced here as nil.
    public let pin: String?
    public let name: String
}

/// K9: redeem a code for a per-device token and keep URL, pin, name and token.
/// The pairing code itself is never stored.
public final class DeckConnection: @unchecked Sendable {
    public static let urlKey = "deckBaseURL"
    public static let pinKey = "deckTLSPin"
    public static let nameKey = "deckName"

    private let defaults: UserDefaults
    private let tokens: TokenStore
    private let now: @Sendable () -> Date
    private let performerFor: @Sendable (String?) -> (RequestPerformer, PinnedTrust?)

    public init(
        defaults: UserDefaults,
        tokens: TokenStore,
        now: @escaping @Sendable () -> Date = { Date() },
        performerFor: (@Sendable (String?) -> (RequestPerformer, PinnedTrust?))? = nil
    ) {
        self.defaults = defaults
        self.tokens = tokens
        self.now = now
        self.performerFor = performerFor ?? { pin in
            let made = PinnedTrust.session(pin: pin)
            return (URLSessionPerformer(session: made.session), made.trust)
        }
    }

    /// Both the suite and the Keychain account come from one place.
    public convenience init(suite: StateSuite = StateSuite()) {
        self.init(defaults: suite.defaults, tokens: suite.tokenStore())
    }

    public var saved: SavedConnection? {
        guard let text = defaults.string(forKey: Self.urlKey), let url = URL(string: text), url.host != nil else { return nil }
        let name = defaults.string(forKey: Self.nameKey) ?? ""
        let pin = defaults.string(forKey: Self.pinKey) ?? ""
        return SavedConnection(url: url, pin: pin.isEmpty ? nil : pin, name: name)
    }

    public func pair(rawCode: String, device: String) async throws -> PairedDeck {
        do { return try await pair(code: PairingCode.parse(rawCode), device: device) }
        catch let e as PairingCodeError { throw PairingFailure.code(e) }
    }

    public func pair(code: PairingCode, device: String) async throws -> PairedDeck {
        // A dead code costs a rate-limited attempt for nothing; say so locally.
        if code.isExpired(now: now()) { throw PairingFailure.expired }

        var request = URLRequest(url: code.url.appendingPathComponent("v1/pair"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpShouldHandleCookies = false
        request.httpBody = try JSONSerialization.data(
            withJSONObject: ["code": code.code, "device": String(device.prefix(60))], options: [.sortedKeys])

        let (performer, trust) = performerFor(code.tls == .pin ? code.pin : nil)
        let data: Data, response: HTTPURLResponse
        do {
            (data, response) = try await performer.perform(request)
        } catch {
            if trust?.didRejectKey == true { throw PairingFailure.pinMismatch }
            if case DeckError.transport(let detail) = error { throw PairingFailure.transport(detail) }
            throw PairingFailure.transport(error.localizedDescription)
        }

        guard response.statusCode == 200 else { throw Self.failure(status: response.statusCode, body: data, response: response) }

        struct Body: Decodable {
            struct Deck: Decodable { let name: String; let version: String }
            let token: String
            let deviceId: String
            let deck: Deck
            enum CodingKeys: String, CodingKey { case token, deviceId = "device_id", deck }
        }
        guard let body = try? JSONDecoder().decode(Body.self, from: data), !body.token.isEmpty else {
            throw PairingFailure.badResponse
        }

        try tokens.setToken(body.token)
        defaults.set(code.url.absoluteString, forKey: Self.urlKey)
        defaults.set(code.tls == .pin ? (code.pin ?? "") : "", forKey: Self.pinKey)
        defaults.set(body.deck.name, forKey: Self.nameKey)
        NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
        return PairedDeck(url: code.url, name: body.deck.name, version: body.deck.version, deviceID: body.deviceId)
    }

    /// Keeps a hand-typed deck exactly as a redeemed pairing code is kept:
    /// token in the Keychain, address in defaults, no pin (a typed address was
    /// never pinned), and the same change notification.
    public func save(_ entry: ManualDeckEntry) throws {
        try tokens.setToken(entry.token)
        defaults.set(entry.url.absoluteString, forKey: Self.urlKey)
        defaults.set("", forKey: Self.pinKey)
        defaults.set(entry.url.host ?? "", forKey: Self.nameKey)
        NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
    }

    /// "Disconnect and forget this server": token, URL, pin and name.
    public func forget() throws {
        try tokens.removeToken()
        for key in [Self.urlKey, Self.pinKey, Self.nameKey] { defaults.removeObject(forKey: key) }
        NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
    }

    /// Transports for the live client, pinned iff the saved connection is.
    public func requestPerformer() -> RequestPerformer {
        URLSessionPerformer(session: PinnedTrust.session(pin: saved?.pin).session)
    }

    public func sseTransport() -> SSETransport {
        URLSessionSSETransport(session: PinnedTrust.session(pin: saved?.pin).session)
    }

    private static func failure(status: Int, body: Data, response: HTTPURLResponse) -> PairingFailure {
        struct Envelope: Decodable { let error: String?; let reason: String? }
        let env = try? JSONDecoder().decode(Envelope.self, from: body)
        let reason = env?.error ?? env?.reason ?? ""
        switch reason {
        case "pair_malformed": return .malformed
        case "pair_unknown": return .unknownCode
        case "pair_expired": return .expired
        case "pair_used": return .alreadyUsed
        case "rate_limited": return .rateLimited(retryAfter: response.value(forHTTPHeaderField: "Retry-After").flatMap { Int($0) })
        default: return .refused(status: status, reason: reason)
        }
    }
}
