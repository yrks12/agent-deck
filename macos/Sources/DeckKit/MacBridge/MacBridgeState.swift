import Foundation

/// The last gesture made on this Mac: who, what, when.
public struct MacControlAction: Equatable, Sendable {
    public var desk: String
    public var summary: String
    public var at: Date

    public init(desk: String, summary: String, at: Date) {
        self.desk = desk
        self.summary = summary
        self.at = at
    }
}

/// How the bridge stands with the deck.
public enum MacConnection: Equatable, Sendable {
    case off, connecting, online, paused
    /// Not allowed to connect; the sentence says why (e.g. the transport guard).
    case refused(String)
}

public struct MacRunningJob: Equatable, Identifiable, Sendable {
    public var id: String
    public var desk: String
    public var summary: String
    public var startedAt: Date

    public init(id: String, desk: String, summary: String, startedAt: Date) {
        self.id = id
        self.desk = desk
        self.summary = summary
        self.startedAt = startedAt
    }
}

/// **Everything DeckUI draws about the Mac bridge, and nothing else.** A
/// plain value: the settings tab, the grant card, the in-use bar, the menu bar
/// and the activity view all render this, so each of their rules is testable
/// without a window.
public struct MacBridgeState: Equatable, Sendable {
    public var mode: MacMode
    public var connection: MacConnection
    public var running: [MacRunningJob]
    public var pendingGrants: [MacGrantRequest]
    public var recent: [MacActivityRow]
    /// Mac control: the live grant (nil = off), desks waiting on his Allow,
    /// and the last gesture an agent made (the banner says who is acting).
    public var control: MacControlGrant?
    public var controlAsks: [String] = []
    public var lastControl: MacControlAction?
    /// His Mac's terminal, open from his phone or another Mac (the banner).
    public var terminal: MacTerminalOpen?

    public static let recentMax = 200

    public init(mode: MacMode = .ask, connection: MacConnection = .off, running: [MacRunningJob] = [],
                pendingGrants: [MacGrantRequest] = [], recent: [MacActivityRow] = []) {
        self.mode = mode
        self.connection = connection
        self.running = running
        self.pendingGrants = pendingGrants
        self.recent = recent
    }

    /// The menu-bar icon fills while this is true.
    public var isInUse: Bool { !running.isEmpty }

    /// What the connection should read before the loop has said anything: the
    /// transport guard and the mode decide it.
    public static func initialConnection(mode: MacMode, deck: URL?) -> MacConnection {
        if let deck, !MacTransport.allowed(deck) { return .refused(MacTransport.refusal) }
        switch mode {
        case .off: return .off
        case .paused: return .paused
        case .ask, .full: return deck == nil ? .off : .connecting
        }
    }

    public mutating func started(_ job: MacRunningJob) {
        running.removeAll { $0.id == job.id }
        running.append(job)
    }

    public mutating func finished(jobId: String, row: MacActivityRow? = nil) {
        running.removeAll { $0.id == jobId }
        if let row { logged(row) }
    }

    public mutating func logged(_ row: MacActivityRow) {
        recent.insert(row, at: 0)
        if recent.count > Self.recentMax { recent.removeLast(recent.count - Self.recentMax) }
    }

    /// One card per desk: a second request from a desk already waiting
    /// replaces nothing and adds nothing — he answers the desk, not the job.
    public mutating func asked(_ request: MacGrantRequest) {
        guard !pendingGrants.contains(where: { $0.desk == request.desk }) else { return }
        pendingGrants.append(request)
    }

    public mutating func answered(desk: String) {
        pendingGrants.removeAll { $0.desk == desk }
    }

    /// "Atlas is using your Mac — `npm test` · 0:14", or nil when idle.
    public func inUseLine(now: Date) -> String? {
        guard let first = running.min(by: { $0.startedAt < $1.startedAt }) else { return nil }
        let desks = running.map(\.desk).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        if running.count == 1 {
            let what = first.summary.count > 60 ? String(first.summary.prefix(59)) + "…" : first.summary
            return "\(Self.display(first.desk)) is using your Mac — `\(what)` · \(Self.elapsed(now.timeIntervalSince(first.startedAt)))"
        }
        let who = desks.count == 1 ? "\(Self.display(desks[0])) is"
            : desks.count == 2 ? "\(Self.display(desks[0])) and \(Self.display(desks[1])) are"
            : "\(desks.count) desks are"
        return "\(who) using your Mac — \(running.count) jobs · \(Self.elapsed(now.timeIntervalSince(first.startedAt)))"
    }

    /// `0:14`, `12:03`, `1:02:03`.
    public static func elapsed(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        let (h, m, sec) = (s / 3600, s / 60 % 60, s % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }

    static func display(_ desk: String) -> String { desk.prefix(1).uppercased() + desk.dropFirst() }
}
