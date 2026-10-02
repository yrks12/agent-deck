import Foundation

/// **The message a reply answers**, as the deck stored it: `reply_to` on the
/// wire. The excerpt is the deck's own one-line copy, never the client's.
public struct MessageQuote: Hashable, Codable, Sendable {
    public var id: String
    public var author: String
    public var excerpt: String

    public init(id: String, author: String, excerpt: String) {
        self.id = id
        self.author = author
        self.excerpt = excerpt
    }

    /// "You" for his own line, the desk's shown name otherwise — the label the
    /// quote in a bubble and the strip above the composer both draw.
    public func authorLabel(displayName: (String) -> String = { $0 }) -> String {
        author == DeckOwner.name ? "You" : displayName(author)
    }
}

/// What the strip above the composer shows while he is replying.
public struct ReplyTarget: Equatable, Sendable {
    public var messageID: String
    public var author: String
    public var authorLabel: String
    /// The first line of the quoted message that has any words on it.
    public var firstLine: String

    /// The quote his bubble carries before the deck's copy comes back.
    public var quote: MessageQuote {
        MessageQuote(id: messageID, author: author, excerpt: firstLine)
    }
}

/// **The composer's reply state.** Set by a swipe, a long-press, a hover
/// button, the menu or ⌘R; cleared by the ×, or by a send the deck accepted.
/// A refused send keeps it, exactly as it keeps his words.
public struct ReplyDraft: Equatable, Sendable {
    public private(set) var target: ReplyTarget?

    public init() {}

    /// The longest first line the strip carries; the strip truncates anyway,
    /// this only keeps a wall of text out of the value.
    public static let firstLineMax = 140

    /// `false`, and nothing changes, for a line there is nothing on the deck
    /// to point at: his own line before the deck has given it an id, or a
    /// deck caption.
    @discardableResult
    public mutating func begin(_ message: Message, displayName: (String) -> String = { $0 }) -> Bool {
        guard Self.canQuote(message) else { return false }
        target = ReplyTarget(
            messageID: message.id,
            author: message.author,
            authorLabel: message.isFromUser ? "You" : displayName(message.author),
            firstLine: Self.firstLine(of: message))
        return true
    }

    public mutating func cancel() { target = nil }

    /// What `POST /v1/threads/{id}/messages` carries as `reply_to`.
    public var replyToID: String? { target?.messageID }

    public static func canQuote(_ message: Message) -> Bool {
        !message.isSystem && !message.id.isEmpty && !message.id.hasPrefix("pending:")
    }

    static func firstLine(of message: Message) -> String {
        let source = message.text.isEmpty ? (message.decision?.prompt ?? "") : message.text
        let line = source.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty } ?? ""
        if line.isEmpty { return message.attachments.isEmpty ? "" : "Attachment" }
        return line.count > firstLineMax ? String(line.prefix(firstLineMax - 1)) + "…" : line
    }
}

/// Where a tap on a quote goes.
public enum QuoteJumpDestination: Equatable, Sendable {
    /// The original is drawn: scroll to this row id.
    case row(String)
    /// The original is folded into an agent-to-agent rollup: open it, then
    /// scroll to `row`.
    case insideRollup(rollupID: String, row: String)
    /// Not in what is loaded — older than the page, or gone.
    case notLoaded
}

public enum QuoteJump {
    /// Read off the drawn items, so both apps answer the same way.
    public static func destination(of messageID: String, in items: [TranscriptItem]) -> QuoteJumpDestination {
        let rowID = "said:\(messageID)"
        for item in items {
            switch item {
            case .entry(let entry) where entry.id == rowID:
                return .row(rowID)
            case .rollup(let rollup) where rollup.hidden.contains(where: { $0.id == rowID }):
                return .insideRollup(rollupID: rollup.id, row: rowID)
            default:
                continue
            }
        }
        return .notLoaded
    }
}

/// One tap on a quote. A new value per tap, so tapping the same quote twice
/// scrolls twice.
public struct QuoteJumpRequest: Equatable, Sendable {
    public var messageID: String
    public var nonce: UUID

    public init(messageID: String, nonce: UUID = UUID()) {
        self.messageID = messageID
        self.nonce = nonce
    }
}
