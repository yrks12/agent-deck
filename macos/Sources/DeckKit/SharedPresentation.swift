import Foundation

/// **One line of status, in words, with the one colour it may carry.**
///
/// The roster rows, the chief's card and the conversation's header on both
/// apps draw this. The tone picks the colour; the text is always there, so the
/// colour is never the only carrier of the meaning.
public struct StatusLine: Equatable, Sendable {
    public enum Tone: Equatable, Sendable {
        /// Orange: it needs him.
        case waiting
        /// Green: it is doing something.
        case working
        /// Grey: a plain fact.
        case neutral
    }

    public let text: String
    public let tone: Tone

    public init(_ text: String, tone: Tone) {
        self.text = text
        self.tone = tone
    }
}

extension RowAttention {
    /// "Waiting for you" / "Working", or nothing for a desk that is fine.
    public var statusLine: StatusLine? {
        switch self {
        case .waitingForYou: return StatusLine("Waiting for you", tone: .waiting)
        case .working: return StatusLine("Working", tone: .working)
        case .quiet: return nil
        }
    }
}

/// **The line under the desk's name at the top of a conversation.**
///
/// What needs him wins; then a stream that is not up (only once the thread has
/// loaded — "Connecting" on a pane that is still opening says nothing); then
/// what the desk is doing.
public enum ThreadHeaderStatus {
    public static func line(
        attention: RowAttention?, connection: ConnectionState?, loaded: Bool, agentState: String
    ) -> StatusLine {
        if attention == .waitingForYou { return StatusLine("Waiting for you", tone: .waiting) }
        if let connection, connection != .live, loaded {
            return StatusLine(connection.label, tone: .neutral)
        }
        if attention == .working { return StatusLine("Working", tone: .working) }
        return StatusLine(agentState, tone: .neutral)
    }
}

/// **What the button at the end of the composer is.**
///
/// The iPhone's rule, now the Mac's: an empty box offers his voice (record a
/// voice message); as soon as there are words it becomes the send arrow. A
/// thread he can only read has no box at all.
public enum ComposerMode: Equatable, Sendable {
    case viewOnly
    case record
    case send(enabled: Bool)

    public static func make(draft: String, isReadOnly: Bool, canRecord: Bool, isSending: Bool) -> ComposerMode {
        if isReadOnly { return .viewOnly }
        let empty = draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if empty && canRecord { return .record }
        return .send(enabled: !empty && !isSending)
    }
}

/// **How the deck is doing, in one line** — "2 working · 1 waiting on you".
///
/// Counted once per desk even when a desk is pinned and in a section too;
/// zero buckets are left out, and a deck with nothing going on says so. The
/// faces beside it are the busy desks, waiting ones first.
public struct RosterPulseSummary: Equatable, Sendable {
    public private(set) var working = 0
    public private(set) var waiting = 0
    /// The desks whose faces lead the line, at most three.
    public let faces: [SidebarRow]

    public init(rows: [SidebarRow]) {
        var seen = Set<String>()
        let unique = rows.filter { seen.insert($0.agent.name).inserted }
        for row in unique {
            switch row.attention {
            case .working: working += 1
            case .waitingForYou: waiting += 1
            case .quiet: break
            }
        }
        let busy = unique.filter { $0.attention != .quiet }
            .enumerated()
            .sorted { lhs, rhs in
                let l = lhs.element.attention == .waitingForYou ? 0 : 1
                let r = rhs.element.attention == .waitingForYou ? 0 : 1
                return l == r ? lhs.offset < rhs.offset : l < r
            }
            .map(\.element)
        faces = Array((busy.isEmpty ? unique : busy).prefix(3))
    }

    public var workingText: String? { working > 0 ? "\(working) working" : nil }
    public var waitingText: String? { waiting > 0 ? "\(waiting) waiting on you" : nil }

    public var line: String {
        let parts = [workingText, waitingText].compactMap { $0 }
        return parts.isEmpty ? "All quiet. Nothing is running right now." : parts.joined(separator: " · ")
    }
}

extension SidebarSnapshot {
    /// **The roster list as both apps draw it: every desk once.** Pinned desks
    /// first, under "Pinned"; then each section without the chief (it is the
    /// card above) and without the pinned desks. A section left empty is
    /// dropped; one that had unread below the fold keeps its overflow count.
    public var listedSections: [SidebarSection] {
        var shownAbove = Set(pinned.map(\.agent.name))
        if let chief { shownAbove.insert(chief.agent.name) }
        var listed: [SidebarSection] = []
        if !pinned.isEmpty {
            listed.append(SidebarSection(name: "Pinned", rows: pinned, overflowUnreadCount: 0))
        }
        for section in sections {
            let rows = section.rows.filter { !shownAbove.contains($0.agent.name) }
            guard !rows.isEmpty || section.hasOverflow else { continue }
            listed.append(SidebarSection(name: section.name, rows: rows,
                                         overflowUnreadCount: section.overflowUnreadCount))
        }
        return listed
    }
}
