import Foundation

/// The row the sidebar draws for a thread that is not a desk yet.
///
/// It is deliberately not a `SidebarRow`: that type is built from a roster
/// payload and every one of its rows is a real desk. A provisional row has no
/// agent, no thread id and no unread count, and saying so in the type is what
/// stops it being sorted, searched or opened like the real ones.
public struct PendingRow: Equatable, Sendable {
    public var title: String
    /// What he has said so far, or the prompt to say something.
    public var detail: String
    public var isProvisional: Bool { true }
    /// Read out as one thing, because that is what it is on screen.
    public var accessibilityLabel: String { "\(title), not created yet. \(detail)" }

    public init(title: String, detail: String) {
        self.title = title
        self.detail = detail
    }
}

/// The conversation that exists before the agent does.
///
/// "+" used to open a sheet — a form with one question in it. This is the same
/// question asked the way everything else on this deck is asked: as the first
/// line of a thread, with a composer under it. The first thing typed into that
/// composer is the hire.
///
/// Nothing here talks to the deck. It is what the pending thread *looks like*
/// at each point — before, during and after a refused create — so the view can
/// stay dumb and the rule that matters ("what he typed survives a failure") is
/// decidable without a window.
public struct PendingThread: Equatable, Sendable {
    /// The agent's opening line. It asks the same thing the sheet asked and
    /// promises the rest is the agent's job.
    public static let openingLine =
        "What do you want this one for? Say it however you like — I'll work out what to call myself."
    public static let placeholder = "Say what you want this one for"
    /// Shown in the thread while the deck is being asked.
    public static let workingLine = "Setting it up…"
    public static let rowTitle = "New agent"
    public static let rowPrompt = "Say what it is for"
    /// The second door, and only the second: the manual form is reachable from
    /// here and from the "+" menu, never by accident.
    public static let manualDoorTitle = "Set it up myself"

    /// What is in the composer. Kept here rather than in the view precisely so
    /// a failed create cannot lose it.
    public var typedText: String
    public var isCreating: Bool
    /// The deck's refusal, or the client's, shown in the thread under his line.
    public var problem: String?

    public init(typedText: String = "", isCreating: Bool = false, problem: String? = nil) {
        self.typedText = typedText
        self.isCreating = isCreating
        self.problem = problem
    }

    public var openingLine: String { Self.openingLine }
    public var composerPlaceholder: String { Self.placeholder }

    /// Disabled only while the create is in flight — a refusal must leave it
    /// usable, because retrying is the whole point of keeping the text.
    public var composerIsEnabled: Bool { !isCreating }

    public var canSend: Bool { composerIsEnabled && !trimmed.isEmpty }

    /// Present only while it is working. A view that switches on this cannot
    /// draw "Setting it up…" over a thread that is sitting idle.
    public var statusLine: String? { isCreating ? Self.workingLine : nil }

    public var trimmed: String {
        typedText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// What would be sent. `role_hint` and nothing else — the deck allocates
    /// the workspace, so there is no folder to ask for.
    public var draft: InterviewDraft { InterviewDraft(roleHint: typedText) }

    public var row: PendingRow {
        PendingRow(title: Self.rowTitle, detail: trimmed.isEmpty ? Self.rowPrompt : trimmed)
    }
}

/// His own first line, held locally until the deck's transcript carries it.
///
/// The hire and the thread are two requests: the desk is created, then its
/// history is fetched. Between them the sentence he typed exists nowhere on
/// screen — and "he never sees a transition to a different window" means it
/// must not blink out. So it is drawn from here until the server's copy shows
/// up, and then dropped, because two copies of the same sentence is worse than
/// none.
public enum ThreadEcho {

    /// True once the server's transcript is carrying his line itself.
    public static func carried(_ echo: Message, by messages: [Message]) -> Bool {
        messages.contains { $0.role == .owner && $0.text == echo.text }
    }

    /// Where his line sits while it is standing in for the deck's copy.
    ///
    /// Two cases, and they are opposites. A hire's sentence *created* the
    /// thread, so everything the deck has in there is an answer to it and his
    /// line is `.first`. A message into a conversation that already exists is
    /// the newest thing in it and is `.last`. Ordering on timestamps instead
    /// would make what he sees depend on this Mac's clock against the deck's.
    public enum Position: Sendable {
        case first
        case last
    }

    public static func merge(
        _ echo: Message?,
        into messages: [Message],
        at position: Position = .first
    ) -> [Message] {
        guard let echo, !carried(echo, by: messages) else { return messages }
        switch position {
        case .first: return [echo] + messages
        case .last: return messages + [echo]
        }
    }
}
