import Foundation

/// **Any JSON value**, for the parts of the Realtime protocol this app passes
/// through without owning (a tool's schema) and for reading server events.
public enum JSONValue: Hashable, Sendable, Codable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    /// Foundation's view of it, for `JSONSerialization`.
    public var foundation: Any {
        switch self {
        case .string(let v): return v
        case .number(let v): return v
        case .bool(let v): return v
        case .object(let v): return v.mapValues(\.foundation)
        case .array(let v): return v.map(\.foundation)
        case .null: return NSNull()
        }
    }
}

/// **C3: the phone call the deck dialled for us.** `POST /v1/calls` mints a
/// short-lived OpenAI Realtime session — instructions, voice and the
/// `send_to_desk` tool already baked in — and hands the app only what it needs
/// to pick up: where to connect and the ephemeral secret. The real API key
/// never leaves the deck.
public struct RealtimeOffer: Hashable, Sendable, Decodable {
    public var wsURL: URL
    public var clientSecret: String
    public var expiresAt: Date?
    public var model: String?
    public var voice: String?
    public var instructions: String?
    public var tools: [JSONValue]

    public init(wsURL: URL, clientSecret: String, expiresAt: Date? = nil, model: String? = nil,
                voice: String? = nil, instructions: String? = nil, tools: [JSONValue] = []) {
        self.wsURL = wsURL
        self.clientSecret = clientSecret
        self.expiresAt = expiresAt
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.tools = tools
    }

    enum CodingKeys: String, CodingKey {
        case model, voice, instructions, tools
        case wsURL = "ws_url"
        case clientSecret = "client_secret"
        case expiresAt = "expires_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = try c.decode(String.self, forKey: .wsURL)
        guard let url = URL(string: raw), let scheme = url.scheme, ["wss", "ws"].contains(scheme) else {
            throw DecodingError.dataCorruptedError(forKey: .wsURL, in: c, debugDescription: "not a websocket URL")
        }
        wsURL = url
        // `client_secret` is a string today; older mints nested it as {value}.
        if let secret = try? c.decode(String.self, forKey: .clientSecret) {
            clientSecret = secret
        } else if case .object(let nested)? = try? c.decode(JSONValue.self, forKey: .clientSecret),
                  case .string(let secret)? = nested["value"] {
            clientSecret = secret
        } else {
            throw DecodingError.dataCorruptedError(forKey: .clientSecret, in: c, debugDescription: "no secret")
        }
        guard !clientSecret.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .clientSecret, in: c, debugDescription: "empty secret")
        }
        switch try? c.decode(JSONValue.self, forKey: .expiresAt) {
        case .number(let seconds)?: expiresAt = Date(timeIntervalSince1970: seconds)
        case .string(let text)?: expiresAt = ISO8601DateFormatter().date(from: text)
        default: expiresAt = nil
        }
        model = try? c.decodeIfPresent(String.self, forKey: .model)
        voice = try? c.decodeIfPresent(String.self, forKey: .voice)
        instructions = try? c.decodeIfPresent(String.self, forKey: .instructions)
        tools = (try? c.decodeIfPresent([JSONValue].self, forKey: .tools)) ?? []
    }
}
