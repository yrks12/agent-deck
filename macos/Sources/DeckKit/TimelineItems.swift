import Foundation

/// **The conversation as a chain of command, decided as values.**
///
/// The owner recorded his screen running the reference app beside this app and
/// said *"we are long we from their experience"*. Four of the differences he
/// measured off those frames are one idea: the transcript has to say **who
/// talked to whom**, and it must never make him scroll past machine chatter to
/// find the answer he asked for.
///
/// | Reference draws | Ours drew |
/// |---|---|
/// | `Messaged 🟣 Initech` between the turns | bubbles run together |
/// | `41 messages with 🔵🟡🟢 3 Bots`, one grey line | all 41, or nothing |
/// | `Thu, Sep 3 7:10 AM` between days | an undated wall |
/// | a small face under the last bubble of a turn | nothing |
///
/// All four are decided **here**, over the entries the view already receives,
/// and nothing in the view layer decides any of them. That is not tidiness:
/// `TranscriptList` is a value-taking `Equatable` view because this pane cost
/// the owner a day of 100% CPU, and a row that had to work anything out for
/// itself would put that back.
public struct TimelineFormatting: Sendable {
    public var calendar: Calendar
    public var locale: Locale
    public var timeZone: TimeZone

    public init(
        calendar: Calendar = .current,
        locale: Locale = .autoupdatingCurrent,
        timeZone: TimeZone = .current
    ) {
        self.calendar = calendar
        self.locale = locale
        self.timeZone = timeZone
    }

    /// His Mac's own calendar, locale and zone. A date row in the wrong locale
    /// is worse than no date row.
    public static var current: TimelineFormatting { TimelineFormatting() }

    func dayKey(of date: Date) -> String {
        var zoned = calendar
        zoned.timeZone = timeZone
        let parts = zoned.dateComponents([.year, .month, .day], from: date)
        return "\(parts.year ?? 0)-\(parts.month ?? 0)-\(parts.day ?? 0)"
    }

    func dayText(of date: Date) -> String {
        date.formatted(
            Date.FormatStyle(locale: locale, calendar: calendar, timeZone: timeZone)
                .weekday(.abbreviated).month(.abbreviated).day().hour().minute())
    }
}

/// "Thu, Sep 3 at 7:10 AM", centred between two days of conversation.
public struct DayBreak: Identifiable, Hashable, Sendable {
    public var id: String
    public var text: String

    public init(id: String, text: String) {
        self.id = id
        self.text = text
    }
}

/// "Messaged Initech" / "Message from Initech UX", centred above the line
/// it is about.
///
/// It carries **both** names on purpose: `text` is what he reads, and `desk` is
/// the wire name the tiny face is derived from — so the face beside the
/// sentence is the same face the sidebar draws for that desk.
public struct AttributionLine: Identifiable, Hashable, Sendable {
    public var id: String
    public var text: String
    public var desk: String
    /// The `peer:` conversation this line came from, and so where a click goes.
    public var peerThreadID: String?

    public init(id: String, text: String, desk: String, peerThreadID: String?) {
        self.id = id
        self.text = text
        self.desk = desk
        self.peerThreadID = peerThreadID
    }
}

/// "41 messages with 3 Bots" — a run of agent-to-agent traffic, standing in for
/// itself until he asks to see it.
///
/// `hidden` is the run, kept whole. A rollup that dropped what it summarised
/// would solve the noise by deleting the evidence.
public struct TrafficRollup: Identifiable, Hashable, Sendable {
    public var id: String
    public var count: Int
    public var text: String
    /// Wire names, first-seen order, for the faces on the row.
    public var faces: [String]
    public var hidden: [ThreadEntry]

    public init(id: String, count: Int, text: String, faces: [String], hidden: [ThreadEntry]) {
        self.id = id
        self.count = count
        self.text = text
        self.faces = faces
        self.hidden = hidden
    }
}

/// The small round face under the last bubble of a desk's turn, so two
/// consecutive turns from different desks are tellable apart.
public struct TurnFace: Identifiable, Hashable, Sendable {
    public var id: String
    public var desk: String

    public init(id: String, desk: String) {
        self.id = id
        self.desk = desk
    }
}

/// "Routine · Morning Shorts report" — a K1 `system` line (a routine fire, the
/// deck's notice, the engineer) drawn as a centred grey caption. Never his
/// bubble: ours-03 drew "Hired: listing-closer…" as though he had typed it.
public struct SystemCaption: Identifiable, Hashable, Sendable {
    public var id: String
    /// "Routine", "Deck", "Engineer", or whoever else the deck named.
    public var source: String
    /// "<source> · <first line>".
    public var text: String
    /// Everything that was sent, for the tooltip and a screen reader.
    public var fullText: String

    public init(id: String, source: String, text: String, fullText: String) {
        self.id = id
        self.source = source
        self.text = text
        self.fullText = fullText
    }

    static let deckPrefix = "[Agent Deck]"

    public static func make(_ message: Message) -> SystemCaption {
        let source: String
        switch message.author {
        case "routine": source = "Routine"
        case "deck": source = "Deck"
        case "engineer": source = "Engineer"
        default: source = Agent.displayName(forWireName: message.author)
        }
        var body = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.hasPrefix(deckPrefix) {
            body = body.dropFirst(deckPrefix.count).trimmingCharacters(in: .whitespaces)
        }
        let first = body.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? ""
        return SystemCaption(
            id: "said:\(message.id)", source: source,
            text: first.isEmpty ? source : "\(source) · \(first)",
            fullText: message.text)
    }
}

/// One drawable row of the conversation. Closed and exhaustive, for the same
/// reason `ThreadEntry` is: a new kind of row is a compile error in the view
/// rather than a row that quietly never draws.
public enum TranscriptItem: Identifiable, Hashable, Sendable {
    case dayBreak(DayBreak)
    case attribution(AttributionLine)
    case rollup(TrafficRollup)
    case entry(ThreadEntry)
    case turnFace(TurnFace)
    case caption(SystemCaption)
    /// `NEW`, directly above the first line he has not read.
    case newDivider

    public static let newDividerID = "new-divider"

    public var id: String {
        switch self {
        case .dayBreak(let value): return value.id
        case .attribution(let value): return value.id
        case .rollup(let value): return value.id
        case .entry(let value): return value.id
        case .turnFace(let value): return value.id
        case .caption(let value): return value.id
        case .newDivider: return Self.newDividerID
        }
    }
}

extension ThreadTimeline {

    /// A run of this many or more relayed lines becomes one grey row.
    ///
    /// **Two, not one.** The reference's frames roll up from two upwards and draw a
    /// single relayed line as itself with its attribution above it — hiding one
    /// message behind a summary of one message is strictly worse than showing
    /// it.
    public static let rollupFrom = 2

    /// **The drawing order, with the chain of command in it.**
    ///
    /// `desk` is the agent this thread belongs to — `ThreadID.desk(ofDirect:)`
    /// of the page's own id. It is what turns a `peer:` id into the *other*
    /// desk's wire name, which is what the faces are derived from.
    ///
    /// ## What is rolled up, and what is never rolled up
    ///
    /// Only **relayed** lines — §6.2 traffic between this desk and another one.
    /// The desk's own answers to him are never collapsed, however many arrive
    /// in a row. The reference rolls up "messages"; ours cannot, because on a `direct:`
    /// thread most of the non-owner messages *are* the answer he is waiting
    /// for, and hiding that behind a line that reads like machine chatter is
    /// the one failure worse than the noise.
    public static func items(
        _ entries: [ThreadEntry],
        desk: String,
        newFrom: String? = nil,
        formatting: TimelineFormatting = .current
    ) -> [TranscriptItem] {
        var items: [TranscriptItem] = []
        // `NEW` is drawn once, above whatever row first carries that line —
        // the line itself, or the rollup it was folded into.
        var owesNew = newFrom.map { id in entries.contains { $0.id == "said:\(id)" } } ?? false
        func markNew(_ covered: [ThreadEntry]) {
            guard owesNew, covered.contains(where: { $0.id == "said:\(newFrom ?? "")" }) else { return }
            items.append(.newDivider)
            owesNew = false
        }
        var lastDayKey: String?
        var run: [ThreadEntry] = []
        var turn: (author: String, lastID: String)?

        /// The face goes under the *last* bubble of the turn, so it is emitted
        /// when the turn ends rather than when it starts.
        func closeTurn() {
            if let open = turn {
                items.append(.turnFace(TurnFace(id: "face:\(open.lastID)", desk: open.author)))
            }
            turn = nil
        }

        func closeRun() {
            defer { run = [] }
            guard !run.isEmpty else { return }
            markNew(run)
            if run.count < rollupFrom {
                for entry in run {
                    if let line = attribution(of: entry, desk: desk) { items.append(.attribution(line)) }
                    items.append(.entry(entry))
                }
            } else {
                items.append(.rollup(rollup(of: run, desk: desk)))
            }
        }

        for entry in entries {
            // A day break belongs before anything drawn for that day, including
            // the attribution row or the rollup that a relayed line opens.
            if let at = entry.at {
                let key = formatting.dayKey(of: at)
                if key != lastDayKey {
                    closeRun()
                    closeTurn()
                    lastDayKey = key
                    items.append(.dayBreak(DayBreak(
                        id: "day:\(key)", text: formatting.dayText(of: at))))
                }
            }

            if case .said(let row) = entry, row.attribution.isRelayed {
                closeTurn()
                run.append(entry)
                continue
            }
            closeRun()
            markNew([entry])

            // K1: a routine fire, a deck notice, the engineer. A caption ends
            // the desk's turn — it is not the desk speaking.
            if case .said(let row) = entry, row.message.isSystem {
                closeTurn()
                items.append(.caption(SystemCaption.make(row.message)))
                continue
            }

            if case .said(let row) = entry, !row.message.isFromUser {
                // A turn is a run of consecutive lines from the same desk.
                if turn?.author != row.message.author { closeTurn() }
                turn = (row.message.author, row.id)
            } else {
                closeTurn()
            }
            items.append(.entry(entry))
        }
        closeRun()
        closeTurn()
        return items
    }

    /// The id of the first line he has not read, given the deck's `unread`
    /// count: the desk's own lines, counted back from the newest. His lines
    /// and system captions never count (§6.4).
    public static func firstUnread(in messages: [Message], unread: Int) -> String? {
        guard unread > 0 else { return nil }
        let theirs = messages.filter { $0.role == .agent }
        return theirs.suffix(unread).first?.id
    }

    /// **A rollup he has opened**: everything it was standing in for, each line
    /// announced by its own attribution row, in the order it happened.
    ///
    /// It is not `items(rollup.hidden, desk:)`. That would roll the run straight
    /// back up — the run is two or more by definition — and it would open a
    /// date row and close a turn inside a list that is already sitting between
    /// two lines of somebody else's conversation. Opening a summary shows what
    /// the summary replaced and changes nothing around it.
    public static func expanded(
        _ rollup: TrafficRollup, desk: String
    ) -> [TranscriptItem] {
        var items: [TranscriptItem] = []
        for entry in rollup.hidden {
            if let line = attribution(of: entry, desk: desk) { items.append(.attribution(line)) }
            items.append(.entry(entry))
        }
        return items
    }

    // MARK: what a run of traffic says about itself

    private static func attribution(of entry: ThreadEntry, desk: String) -> AttributionLine? {
        guard case .said(let row) = entry else { return nil }
        switch row.attribution {
        case .ordinary:
            return nil
        case .dispatch(let name, let threadID):
            return AttributionLine(
                id: "to:\(row.id)", text: "Messaged \(name)",
                desk: wireName(of: row, desk: desk), peerThreadID: threadID)
        case .reply(let name, let threadID):
            return AttributionLine(
                id: "from:\(row.id)", text: "Message from \(name)",
                desk: wireName(of: row, desk: desk), peerThreadID: threadID)
        }
    }

    /// "The participant of the `peer:` id that is not this desk" — §6.2's own
    /// rule, applied again here because the *shown* name has already been
    /// resolved through the roster and a face may not be derived from a title.
    private static func wireName(of row: TranscriptRow, desk: String) -> String {
        let id = row.message.threadID
        return ThreadID.peer(in: id, otherThan: desk)
            ?? ThreadID.peer(in: id, otherThan: row.message.author)
            ?? row.message.author
    }

    private static func rollup(of run: [ThreadEntry], desk: String) -> TrafficRollup {
        var faces: [String] = []
        var shown: [String] = []
        for entry in run {
            guard case .said(let row) = entry else { continue }
            let wire = wireName(of: row, desk: desk)
            if !faces.contains(wire) { faces.append(wire) }
            let name: String? = {
                switch row.attribution {
                case .ordinary: return nil
                case .dispatch(let name, _), .reply(let name, _): return name
                }
            }()
            if let name, !shown.contains(name) { shown.append(name) }
        }
        // One desk is worth naming; several are a count, exactly as the reference's
        // "41 messages with 3 Bots" is. A list of five names is the wall of
        // detail this row exists to replace.
        let who = shown.count == 1 ? shown[0] : "\(faces.count) Bots"
        return TrafficRollup(
            id: "traffic:\(run.first?.id ?? "")",
            count: run.count,
            text: "\(run.count) messages with \(who)",
            faces: faces,
            hidden: run)
    }
}
