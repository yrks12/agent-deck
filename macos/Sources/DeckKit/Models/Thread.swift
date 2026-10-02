import Foundation

public enum ThreadKind: String, Codable, Sendable {
    /// Owner ↔ agent. `direct:<agent>`.
    case direct
    /// Agent ↔ agent. `peer:<a>|<b>`, names sorted. Always read-only.
    case peer

    public init(wire: String) {
        self = ThreadKind(rawValue: wire) ?? .direct
    }
}

/// One row's worth of thread state.
///
/// For the sidebar this is synthesised from the agent row that `/v1/agents`
/// already returned — that is what keeps the sidebar to one request. The
/// `/v1/threads` list is only fetched when the peer threads themselves are
/// wanted.
public struct ThreadSummary: Identifiable, Hashable, Decodable, Sendable {
    public var id: String
    public var kind: ThreadKind
    /// Already assembled by the deck for a peer thread: "chief ⇄ hemingway".
    public var title: String
    /// The sidebar row this thread belongs to.
    public var agentName: String
    /// `[DeckOwner.name, agent]` for a direct thread, two agent names for a peer one.
    public var participants: [String]
    /// Authority on whether the composer appears. Never inferred from `kind`,
    /// from `participants`, or from anything else.
    public var isReadOnly: Bool
    public var unreadCount: Int
    public var messageCount: Int
    public var lastActivity: Date?
    public var lastCursor: String
    public var preview: ThreadPreview

    public init(
        id: String,
        kind: ThreadKind = .direct,
        title: String = "",
        agentName: String,
        participants: [String],
        isReadOnly: Bool,
        unreadCount: Int,
        messageCount: Int = 0,
        lastActivity: Date?,
        lastCursor: String = "",
        preview: ThreadPreview
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.agentName = agentName
        self.participants = participants
        self.isReadOnly = isReadOnly
        self.unreadCount = unreadCount
        self.messageCount = messageCount
        self.lastActivity = lastActivity
        self.lastCursor = lastCursor
        self.preview = preview
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, title, participants, preview
        case agentName = "agent"
        case isReadOnly = "read_only"
        case unreadCount = "unread"
        case messageCount = "message_count"
        case lastTS = "last_ts"
        case lastCursor = "last_cursor"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        kind = ThreadKind(wire: try container.decodeIfPresent(String.self, forKey: .kind) ?? "direct")
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? ""
        participants = try container.decodeIfPresent([String].self, forKey: .participants) ?? []
        // The row this belongs on: the first participant that is not the owner.
        agentName = try container.decodeIfPresent(String.self, forKey: .agentName)
            ?? participants.first { $0 != DeckOwner.name }
            ?? title
        isReadOnly = try container.decodeIfPresent(Bool.self, forKey: .isReadOnly) ?? false
        unreadCount = try container.decodeIfPresent(Int.self, forKey: .unreadCount) ?? 0
        messageCount = try container.decodeIfPresent(Int.self, forKey: .messageCount) ?? 0
        // `last_ts` is 0.0 for a thread with no messages; that is "no time".
        let ts = try container.decodeIfPresent(Double.self, forKey: .lastTS)
        lastActivity = (ts ?? 0) > 0 ? Date(timeIntervalSince1970: ts!) : nil
        lastCursor = try container.decodeIfPresent(String.self, forKey: .lastCursor) ?? ""
        let previewText = try container.decodeIfPresent(String.self, forKey: .preview) ?? ""
        preview = ThreadPreview(text: previewText)
    }
}

/// `{"agents": [...], "generated_at": ...}` — the sidebar, in one call.
public struct AgentsResponse: Decodable, Sendable {
    public var agents: [Agent]
    public var generatedAt: Date?

    enum CodingKeys: String, CodingKey {
        case agents
        case generatedAt = "generated_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        agents = try container.decodeIfPresent([Agent].self, forKey: .agents) ?? []
        generatedAt = try container.decodeIfPresent(Double.self, forKey: .generatedAt)
            .map(Date.init(timeIntervalSince1970:))
    }

    /// Folds the flat agent rows into the shape the sidebar builder wants. The
    /// section list is derived from the agents, because nothing on the server
    /// enumerates sections.
    public func asRosterPayload() -> RosterPayload {
        Self.roster(from: agents)
    }
}

public struct ThreadsResponse: Decodable, Sendable {
    public var threads: [ThreadSummary]

    enum CodingKeys: String, CodingKey { case threads }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        threads = try container.decodeIfPresent([ThreadSummary].self, forKey: .threads) ?? []
    }
}

/// Everything the sidebar needs, in one object, from one call.
public struct RosterPayload: Sendable {
    public var agents: [Agent]
    public var threads: [ThreadSummary]
    /// Derived from the agents. Empty means "order of first appearance".
    public var sectionOrder: [String]

    public init(agents: [Agent], threads: [ThreadSummary], sectionOrder: [String]) {
        self.agents = agents
        self.threads = threads
        self.sectionOrder = sectionOrder
    }
}

/// The answer to `GET /v1/threads/{id}/messages`. Always oldest-first.
public struct MessagePage: Decodable, Sendable {
    public var threadID: String
    public var kind: ThreadKind
    public var messages: [Message]
    public var isReadOnly: Bool
    public var participants: [String]
    public var hasMoreBefore: Bool
    public var hasMoreAfter: Bool
    /// Echo these back; never build one.
    public var nextSince: String?
    public var nextBefore: String?

    public init(
        threadID: String,
        kind: ThreadKind = .direct,
        messages: [Message],
        isReadOnly: Bool,
        participants: [String],
        hasMoreBefore: Bool = false,
        hasMoreAfter: Bool = false,
        nextSince: String? = nil,
        nextBefore: String? = nil
    ) {
        self.threadID = threadID
        self.kind = kind
        self.messages = messages
        self.isReadOnly = isReadOnly
        self.participants = participants
        self.hasMoreBefore = hasMoreBefore
        self.hasMoreAfter = hasMoreAfter
        self.nextSince = nextSince
        self.nextBefore = nextBefore
    }

    enum CodingKeys: String, CodingKey {
        case messages, participants, kind
        case threadID = "thread_id"
        case isReadOnly = "read_only"
        case hasMoreBefore = "has_more_before"
        case hasMoreAfter = "has_more_after"
        case nextSince = "next_since"
        case nextBefore = "next_before"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        threadID = try container.decode(String.self, forKey: .threadID)
        kind = ThreadKind(wire: try container.decodeIfPresent(String.self, forKey: .kind) ?? "direct")
        messages = try container.decodeIfPresent([Message].self, forKey: .messages) ?? []
        isReadOnly = try container.decodeIfPresent(Bool.self, forKey: .isReadOnly) ?? false
        participants = try container.decodeIfPresent([String].self, forKey: .participants) ?? []
        hasMoreBefore = try container.decodeIfPresent(Bool.self, forKey: .hasMoreBefore) ?? false
        hasMoreAfter = try container.decodeIfPresent(Bool.self, forKey: .hasMoreAfter) ?? false
        nextSince = try container.decodeIfPresent(String.self, forKey: .nextSince)
        nextBefore = try container.decodeIfPresent(String.self, forKey: .nextBefore)
    }
}

/// The answer to `POST /v1/threads/{id}/messages`.
public struct SendResponse: Decodable, Sendable {
    /// `false` means queued, not lost. Never retry on it — that sends twice.
    public var delivered: Bool
    public var message: Message

    enum CodingKeys: String, CodingKey { case delivered, message }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        delivered = try container.decodeIfPresent(Bool.self, forKey: .delivered) ?? false
        message = try container.decode(Message.self, forKey: .message)
    }
}
