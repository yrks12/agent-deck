import Foundation

/// K1: a closed set. `system` is a routine fire, the deck's own notice or the
/// engineer — never the owner, whatever channel it arrived on.
public enum MessageRole: String, Codable, Sendable {
    case owner
    case agent
    case system

    /// An unknown value renders as `agent` — never guessed back into `owner`.
    public init(wire: String) {
        self = MessageRole(rawValue: wire) ?? .agent
    }
}

/// K1: said on a live call, or typed. Unknown values read as `text`.
public enum MessageChannel: String, Codable, Sendable {
    case text, voice
}

/// K1: an ordinary line, or a K4 decision card.
public enum MessageKind: String, Codable, Sendable {
    case text, decision
}

/// One button on a decision card (K4).
public struct DecisionOption: Hashable, Codable, Sendable {
    public enum Style: String, Codable, Sendable {
        case primary, `default`, danger
    }

    public var label: String
    /// What is posted back, and what his answer bubble says.
    public var value: String
    public var style: Style

    public init(label: String, value: String, style: Style = .default) {
        self.label = label
        self.value = value
        self.style = style
    }

    enum CodingKeys: String, CodingKey { case label, value, style }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        label = try container.decode(String.self, forKey: .label)
        value = try container.decodeIfPresent(String.self, forKey: .value) ?? label
        style = (try container.decodeIfPresent(String.self, forKey: .style))
            .flatMap(Style.init(rawValue:)) ?? .default
    }
}

/// **K4: a question the desk asked as buttons, not prose.** `answered` carries
/// what he picked; `skipped` means he typed something else instead.
public struct Decision: Identifiable, Hashable, Codable, Sendable {
    public enum State: String, Codable, Sendable {
        case open, answered, skipped
    }

    public var id: String
    public var prompt: String
    public var help: String?
    public var options: [DecisionOption]
    public var allowCustom: Bool
    public var state: State
    public var answer: String?

    public init(
        id: String, prompt: String, help: String? = nil, options: [DecisionOption],
        allowCustom: Bool = true, state: State = .open, answer: String? = nil
    ) {
        self.id = id
        self.prompt = prompt
        self.help = help
        self.options = options
        self.allowCustom = allowCustom
        self.state = state
        self.answer = answer
    }

    /// The line under the card once something has happened to it; `nil`
    /// while it is open. An answer is named by the option's label when it is
    /// one, and by his own words otherwise.
    public var statusLine: String? {
        switch state {
        case .open:
            return nil
        case .answered:
            let chosen = answer.map { value in options.first { $0.value == value }?.label ?? value }
            return "You chose: \(chosen ?? "—")"
        case .skipped:
            return "Skipped — you answered in the chat"
        }
    }

    /// §6.5: an answered card is done; an open **or skipped** one can still be
    /// answered — the tap is the newest thing he said about it.
    public var offersChoices: Bool { state != .answered }

    enum CodingKeys: String, CodingKey {
        case id, prompt, help, options, state, answer
        case allowCustom = "allow_custom"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        prompt = try container.decodeIfPresent(String.self, forKey: .prompt) ?? ""
        help = try container.decodeIfPresent(String.self, forKey: .help)
        options = try container.decodeIfPresent([DecisionOption].self, forKey: .options) ?? []
        allowCustom = try container.decodeIfPresent(Bool.self, forKey: .allowCustom) ?? false
        state = (try container.decodeIfPresent(String.self, forKey: .state))
            .flatMap(State.init(rawValue:)) ?? .open
        answer = try container.decodeIfPresent(String.self, forKey: .answer)
    }
}

/// Derived server-side from the message text by pattern match. The deck does
/// not store files and does not serve them — see the contract's §9.5 — so an
/// `image` is only drawable if this machine can already reach the path.
public struct Attachment: Hashable, Codable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case file, image, link
    }

    /// What to draw. Not `kind`: an app built before videos decodes `kind` as
    /// this closed enum, so the deck keeps it `image`/`file`/`link` and says
    /// the rest here (`docs/client-api.md`, the `attachments` row).
    public enum Media: String, Codable, Sendable {
        case image, video, audio, pdf, file
    }

    public var kind: Kind
    public var value: String
    /// Set by the deck when it holds the bytes (one he sent from the phone,
    /// or a file a desk sent him): `/v1/attachments/{id}/{name}`, fetched
    /// with his token, byte ranges answered.
    public var url: String?
    /// The deck's word for it; nil from an older deck (judged by the name).
    public var mediaName: String?
    public var mime: String?
    public var bytes: Int?
    public var name: String?
    /// A JPEG poster frame (video) or thumbnail (big image), once made.
    public var previewURL: String?

    enum CodingKeys: String, CodingKey {
        case kind, value, url, mime, bytes, name
        case mediaName = "media"
        case previewURL = "preview_url"
    }

    public init(kind: Kind, value: String, url: String? = nil, media: Media? = nil,
                mime: String? = nil, bytes: Int? = nil, name: String? = nil, previewURL: String? = nil) {
        self.kind = kind
        self.value = value
        self.url = url
        self.mediaName = media?.rawValue
        self.mime = mime
        self.bytes = bytes
        self.name = name
        self.previewURL = previewURL
    }

    /// A local image this app can actually draw. Anything else is shown as its
    /// path rather than as a broken picture.
    public var localImageURL: URL? {
        guard kind == .image else { return nil }
        let expanded = (value as NSString).expandingTildeInPath
        guard FileManager.default.fileExists(atPath: expanded) else { return nil }
        return URL(fileURLWithPath: expanded)
    }
}

/// A run of message content the thread view knows how to draw.
public enum MessageSpan: Hashable, Sendable {
    case text(String)
    case link(URL)
    /// A file path. Rendered monospaced, because a path that wraps like prose
    /// is a path you cannot copy.
    case path(String)
}

/// One line in a thread.
///
/// `cursor` is the deck's opaque, string-comparable total-order key. The client
/// never builds one and never parses one — it sorts on it, deduplicates on it,
/// and echoes it back as `since=`.
public struct Message: Identifiable, Hashable, Codable, Sendable {
    public var id: String
    public var cursor: String
    public var threadID: String
    /// `DeckOwner.name` or an agent name.
    public var author: String
    public var role: MessageRole
    public var sentAt: Date
    public var text: String
    public var attachments: [Attachment]
    public var channel: MessageChannel
    public var kind: MessageKind
    /// Present when `kind == .decision`.
    public var decision: Decision?
    /// His recorded voice message, when this line is its transcript.
    public var voiceNote: VoiceNoteRef?
    /// The message this one answers, when it is a quote-reply.
    public var replyTo: MessageQuote?

    public init(
        id: String,
        cursor: String,
        threadID: String,
        author: String,
        role: MessageRole,
        sentAt: Date,
        text: String,
        attachments: [Attachment] = [],
        channel: MessageChannel = .text,
        kind: MessageKind = .text,
        decision: Decision? = nil,
        voiceNote: VoiceNoteRef? = nil,
        replyTo: MessageQuote? = nil
    ) {
        self.id = id
        self.cursor = cursor
        self.threadID = threadID
        self.author = author
        self.role = role
        self.sentAt = sentAt
        self.text = text
        self.attachments = attachments
        self.channel = channel
        self.kind = kind
        self.decision = decision
        self.voiceNote = voiceNote
        self.replyTo = replyTo
    }

    public var isFromUser: Bool { role == .owner }

    /// A routine fire, the deck's own notice, or the engineer (K1).
    public var isSystem: Bool { role == .system }

    public var spans: [MessageSpan] { MessageBody.spans(text) }

    public var displayName: String { isFromUser ? "You" : author }

    /// The one sentence VoiceOver reads for this message.
    ///
    /// The body comes first because it is the thing worth hearing; who and
    /// when follow it. The bubble used to announce only who and when, which
    /// made every conversation in the app unreadable to anyone using a screen
    /// reader — a bubble is not a decoration, it is the content.
    ///
    /// `timestamp` is passed in rather than formatted here so this stays in
    /// DeckKit, where it can be decided without a screen.
    public func spokenLabel(timestamp: String) -> String {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let said = body.isEmpty ? "no text" : body
        var sentence = "\(displayName): \(said), \(timestamp)"
        let images = attachments.filter { $0.localImageURL != nil }.count
        if images > 0 {
            sentence += images == 1 ? ", 1 image attached" : ", \(images) images attached"
        }
        return sentence
    }

    enum CodingKeys: String, CodingKey {
        case id, cursor, author, role, text, attachments, channel, kind, decision
        case threadID = "thread_id"
        case voiceNote = "voice_note"
        case replyTo = "reply_to"
        case ts
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        cursor = try container.decodeIfPresent(String.self, forKey: .cursor) ?? id
        threadID = try container.decodeIfPresent(String.self, forKey: .threadID) ?? ""
        author = try container.decodeIfPresent(String.self, forKey: .author) ?? ""
        // Derived from the author only when the deck sent no role at all. An
        // unknown role is `agent` (K1): guessing `owner` from the author is how
        // a routine fire became his bubble.
        if let raw = try container.decodeIfPresent(String.self, forKey: .role) {
            role = MessageRole(wire: raw)
        } else {
            role = author == DeckOwner.name ? .owner : .agent
        }
        sentAt = Date(timeIntervalSince1970: try container.decodeIfPresent(Double.self, forKey: .ts) ?? 0)
        text = try container.decodeIfPresent(String.self, forKey: .text) ?? ""
        attachments = try container.decodeIfPresent([Attachment].self, forKey: .attachments) ?? []
        channel = (try container.decodeIfPresent(String.self, forKey: .channel))
            .flatMap(MessageChannel.init(rawValue:)) ?? .text
        kind = (try container.decodeIfPresent(String.self, forKey: .kind))
            .flatMap(MessageKind.init(rawValue:)) ?? .text
        decision = try container.decodeIfPresent(Decision.self, forKey: .decision)
        voiceNote = try? container.decodeIfPresent(VoiceNoteRef.self, forKey: .voiceNote)
        // `try?`: a malformed quote costs the quote, never the message.
        replyTo = try? container.decodeIfPresent(MessageQuote.self, forKey: .replyTo)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(cursor, forKey: .cursor)
        try container.encode(threadID, forKey: .threadID)
        try container.encode(author, forKey: .author)
        try container.encode(role.rawValue, forKey: .role)
        try container.encode(sentAt.timeIntervalSince1970, forKey: .ts)
        try container.encode(text, forKey: .text)
        try container.encode(attachments, forKey: .attachments)
        try container.encode(channel, forKey: .channel)
        try container.encode(kind, forKey: .kind)
        try container.encodeIfPresent(decision, forKey: .decision)
        try container.encodeIfPresent(voiceNote, forKey: .voiceNote)
        try container.encodeIfPresent(replyTo, forKey: .replyTo)
    }
}

/// There is one human on this deck. His wire id is the deck's, not the app's:
/// every `/v1` response names it in `X-Deck-Owner` ("owner" unless an older
/// deck kept another id), and the client learns it from the first answer.
public enum DeckOwner {
    public static let neutral = "owner"
    public static let header = "X-Deck-Owner"
    /// His display name, percent-encoded; absent on a deck with none set.
    public static let nameHeader = "X-Deck-Owner-Name"

    private final class Box: @unchecked Sendable {
        let lock = NSLock()
        var value = DeckOwner.neutral
        var display: String?
    }
    private static let box = Box()

    public static var name: String {
        box.lock.lock(); defer { box.lock.unlock() }
        return box.value
    }

    /// **His name, as the deck knows it** ("Sam"), or nil until a deck has
    /// said one. The footer draws "Owner" only while this is nil.
    public static var displayName: String? {
        box.lock.lock(); defer { box.lock.unlock() }
        return box.display
    }

    /// Adopt the name from `X-Deck-Owner-Name`. A response without the header,
    /// or with a blank one, never forgets a name already learned. `reset` is
    /// for tests.
    public static func learnDisplayName(_ encoded: String?, reset: Bool = false) {
        box.lock.lock(); defer { box.lock.unlock() }
        if reset { box.display = nil; return }
        guard let encoded, let decoded = encoded.removingPercentEncoding?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !decoded.isEmpty, decoded.count <= 60 else { return }
        box.display = decoded
    }

    /// Both headers, from any answer.
    public static func learn(from response: HTTPURLResponse) {
        learn(response.value(forHTTPHeaderField: header))
        learnDisplayName(response.value(forHTTPHeaderField: nameHeader))
    }

    /// Adopt the deck's id. Anything that is not a short lowercase slug is
    /// ignored, so a missing or odd header never renames him.
    public static func learn(_ handle: String?) {
        guard let handle = handle?.trimmingCharacters(in: .whitespaces),
              !handle.isEmpty, handle.count <= 32,
              handle.first.map({ $0.isLetter || $0.isNumber }) == true,
              handle.allSatisfy({ ($0.isLowercase && $0.isASCII) || ($0.isNumber && $0.isASCII) || $0 == "-" })
        else { return }
        box.lock.lock(); box.value = handle; box.lock.unlock()
    }
}

/// Splits plain message text into the runs the view draws differently.
/// Done once, here, so no view body is parsing strings during layout.
public enum MessageBody {

    public static func spans(_ raw: String) -> [MessageSpan] {
        var spans: [MessageSpan] = []
        var plain = ""

        func flush() {
            if !plain.isEmpty {
                spans.append(.text(plain))
                plain = ""
            }
        }

        // Split on spaces only: punctuation is handled per token so a full stop
        // after a URL stays in the sentence rather than inside the link.
        for (index, token) in raw.split(separator: " ", omittingEmptySubsequences: false).enumerated() {
            if index > 0 { plain += " " }
            let word = String(token)
            let (core, trailing) = splitTrailingPunctuation(word)

            if core.hasPrefix("https://") || core.hasPrefix("http://"),
               let url = URL(string: core) {
                flush()
                spans.append(.link(url))
                plain += trailing
            } else if isFilePath(core) {
                flush()
                spans.append(.path(core))
                plain += trailing
            } else {
                plain += word
            }
        }
        flush()
        return spans
    }

    /// A path needs a separator inside it. A lone "/" is prose.
    private static func isFilePath(_ token: String) -> Bool {
        guard token.hasPrefix("/") || token.hasPrefix("~/") else { return false }
        return token.dropFirst().contains("/") || (token.hasPrefix("/") && token.count > 1)
    }

    private static func splitTrailingPunctuation(_ token: String) -> (String, String) {
        let punctuation: Set<Character> = [".", ",", ")", "]", "}", ";", ":", "!", "?", "\""]
        var core = token
        var trailing = ""
        while let last = core.last, punctuation.contains(last) {
            trailing.insert(last, at: trailing.startIndex)
            core.removeLast()
        }
        return (core, trailing)
    }
}
