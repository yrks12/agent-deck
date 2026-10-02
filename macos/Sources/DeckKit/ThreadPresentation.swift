import Foundation

/// What sits at the bottom of the thread. There is no third case: a thread
/// either takes input or explains why it does not — so a view that switches on
/// this cannot accidentally draw a text field on a read-only conversation.
public enum ComposerState: Equatable, Sendable {
    case enabled(placeholder: String)
    case viewOnly(footer: String, closeButtonTitle: String)
}

/// Everything the thread header and footer render, decided once, off the
/// payload.
public struct ThreadPresentation: Equatable, Sendable {
    public static let viewOnlyFooter = "This chat is view-only"
    public static let closeButtonTitle = "Close Chat"

    public var threadID: String
    public var headerTitle: String
    public var headerSubtitle: String?
    public var composer: ComposerState
    public var isReadOnly: Bool

    public init(
        threadID: String,
        headerTitle: String,
        headerSubtitle: String?,
        composer: ComposerState,
        isReadOnly: Bool
    ) {
        self.threadID = threadID
        self.headerTitle = headerTitle
        self.headerSubtitle = headerSubtitle
        self.composer = composer
        self.isReadOnly = isReadOnly
    }

    /// `isReadOnly` is the thread payload's flag and the sole authority on the
    /// composer. Participant count decides the *title* — "Chief ⇄ Hemingway" —
    /// and nothing else. Guessing from it in either direction is wrong: a
    /// two-party thread can be writable, and a one-party thread can be frozen.
    public static func make(
        threadID: String,
        participants: [String],
        isReadOnly: Bool,
        title serverTitle: String? = nil,
        agents: [String: Agent]
    ) -> ThreadPresentation {
        // The owner is a participant on a direct thread, but he is not who the
        // header is about.
        let others = participants.filter { $0 != DeckOwner.name }
        let primary = others.first ?? participants.first ?? "Agent"
        let isPeer = others.count > 1
        // Names are lowercase on the wire; capitalising them is a client
        // decision the contract leaves open.
        func shown(_ name: String) -> String { agents[name]?.displayName ?? name }

        // The deck already assembles "chief ⇄ hemingway"; prefer it over
        // reassembling one here.
        let title = serverTitle.flatMap { $0.isEmpty ? nil : $0 }
            ?? (isPeer ? others.map(shown).joined(separator: " ⇄ ") : shown(primary))
        let subtitle = isPeer ? "Agent-to-agent relay" : agents[primary]?.title

        return ThreadPresentation(
            threadID: threadID,
            headerTitle: title,
            headerSubtitle: subtitle,
            composer: isReadOnly
                ? .viewOnly(footer: viewOnlyFooter, closeButtonTitle: closeButtonTitle)
                : .enabled(placeholder: "Message \(shown(primary))"),
            isReadOnly: isReadOnly
        )
    }
}

public enum SendOutcome: Equatable, Sendable {
    case sent(Message)
    case refusedViewOnly
    case refusedEmpty
    case failed(DeckError)
}

/// The send path behind the composer.
///
/// It refuses a view-only thread itself rather than letting the server do it.
/// The UI already hides the box; this is the second lock, for the day someone
/// wires a keyboard shortcut straight to `submit`.
public actor ThreadComposer {
    private let client: DeckClient
    private let threadID: String
    private let isReadOnly: Bool

    public init(client: DeckClient, threadID: String, isReadOnly: Bool) {
        self.client = client
        self.threadID = threadID
        self.isReadOnly = isReadOnly
    }

    /// `replyTo` is the message this one answers (`ReplyDraft.replyToID`).
    public func submit(_ text: String, replyTo: String? = nil) async -> SendOutcome {
        guard !isReadOnly else { return .refusedViewOnly }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .refusedEmpty }

        do {
            return .sent(try await client.send(threadID: threadID, text: trimmed, replyTo: replyTo))
        } catch let error as DeckError {
            return .failed(error)
        } catch {
            return .failed(.transport(error.localizedDescription))
        }
    }
}

/// The per-agent settings panel: avatar, name, title, description, notifications.
public actor AgentSettingsModel {
    public static let notificationsCaption = "Get notified when this Bot finishes or needs input"
    // The line **under** that caption is deliberately not here.
    // `NotificationDelivery` owns what this build can send a notification on
    // and derives the sentence from it. A frozen copy of that sentence used to
    // sit here: read by nothing, wrong from the day the pushes shipped, and
    // still the constant a future edit would have reached for. See
    // `OneSentenceAboutDeliveryTests`.

    private let client: DeckClient
    public private(set) var agent: Agent
    public private(set) var lastError: DeckError?

    public init(agent: Agent, client: DeckClient) {
        self.agent = agent
        self.client = client
    }

    public func setNotifications(_ enabled: Bool) async {
        var next = agent
        next.notificationsEnabled = enabled
        await push(next)
    }

    public func save(name: String, title: String, detail: String, summary: String? = nil) async {
        var next = agent
        next.name = name
        next.title = title
        next.detail = detail
        if let summary { next.summary = summary }
        await push(next)
    }

    /// One PATCH, and the server's answer wins — the panel shows what was
    /// actually stored, not what was typed.
    private func push(_ next: Agent) async {
        do {
            agent = try await client.updateAgent(next)
            lastError = nil
        } catch let error as DeckError {
            lastError = error
        } catch {
            lastError = .transport(error.localizedDescription)
        }
    }
}
