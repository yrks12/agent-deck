import Foundation

/// Most of what moves through this deck is agent-to-agent. The sidebar preview
/// and the in-thread separator both say so, in the same words, from one place.
public enum Relay: Hashable, Codable, Sendable {
    /// This agent wrote to another one.
    case messaged(String)
    /// Another agent wrote to this one.
    case received(String)

    public var peer: String {
        switch self {
        case .messaged(let name), .received(let name): return name
        }
    }

    /// The small centred line in the thread, and the prefix in the sidebar.
    public var separatorText: String {
        switch self {
        case .messaged(let name): return "Messaged \(name)"
        case .received(let name): return "Message from \(name)"
        }
    }

    enum CodingKeys: String, CodingKey { case kind, name }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let name = try container.decode(String.self, forKey: .name)
        switch try container.decode(String.self, forKey: .kind) {
        case "messaged": self = .messaged(name)
        case "received", "from": self = .received(name)
        case let other:
            throw DecodingError.dataCorruptedError(
                forKey: .kind, in: container, debugDescription: "unknown relay kind \(other)"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .messaged(let name):
            try container.encode("messaged", forKey: .kind)
            try container.encode(name, forKey: .name)
        case .received(let name):
            try container.encode("received", forKey: .kind)
            try container.encode(name, forKey: .name)
        }
    }
}

/// The one-line summary under an agent's name in the sidebar.
public struct ThreadPreview: Hashable, Codable, Sendable {
    public var text: String
    public var relay: Relay?

    public init(text: String, relay: Relay? = nil) {
        self.text = text
        self.relay = relay
    }

    /// "Messaged Chief: draft is ready", or just the text when it is the user's
    /// own conversation.
    public var line: String {
        guard let relay else { return text }
        return "\(relay.separatorText): \(text)"
    }

    enum CodingKeys: String, CodingKey { case text, relay }
}
