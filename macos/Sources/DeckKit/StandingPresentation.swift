import Foundation

/// **What the Standing approvals screen says, and in what order.** DeckUI only
/// draws this; both apps draw the same words.
public enum StandingPresentation {
    public static let title = "Standing approvals"
    public static let empty = "No standing approvals yet. Add one to let a desk act within a daily limit without asking."
    public static let floor = "Money to others, deleting data, credentials and account security always ask, whatever is set here."
    public static let cardOption = "Always, up to a limit…"
    public static let unavailable = "This deck does not offer standing approvals yet."
    public static let emptyLog = "Nothing has been done under a standing approval yet."

    /// The screen's three groups: proposals first (they wait on him), then
    /// what is honoured now, then what has ended (kept for the record).
    public struct Sections: Equatable, Sendable {
        public var proposed: [StandingPolicy]
        public var active: [StandingPolicy]
        public var ended: [StandingPolicy]
    }

    public static func sections(_ policies: [StandingPolicy]) -> Sections {
        let newest: (StandingPolicy, StandingPolicy) -> Bool = {
            ($0.createdAt ?? .distantPast) > ($1.createdAt ?? .distantPast)
        }
        return Sections(
            proposed: policies.filter { $0.status == .proposed }.sorted(by: newest),
            active: policies.filter { $0.status == .active && !$0.expired }.sorted(by: newest),
            ended: policies.filter { $0.status == .revoked || ($0.status == .active && $0.expired) }.sorted(by: newest))
    }

    /// The badge on the sidebar row / menu entry: proposals waiting on him.
    public static func proposedCount(_ policies: [StandingPolicy]) -> Int {
        policies.filter { $0.status == .proposed }.count
    }

    /// The phone's roster line: "2 standing approvals wait on you".
    public static func waitingLine(_ count: Int) -> String {
        count == 1 ? "1 standing approval waits on you" : "\(count) standing approvals wait on you"
    }

    /// The menu entry, with the count when there is one.
    public static func menuTitle(_ count: Int) -> String {
        count > 0 ? "\(title) (\(count))" : title
    }

    public static func badge(_ count: Int) -> String? {
        count <= 0 ? nil : (count > 99 ? "99+" : "\(count)")
    }

    public static func kindLabel(_ kind: StandingKind) -> String {
        switch kind {
        case .sendEmail: return "Send email"
        case .spendMoney: return "Spend money"
        case .postComment: return "Post comments"
        case .runCommand: return "Run commands"
        case .callAPI: return "Call APIs"
        case .other(let raw): return raw
        }
    }

    /// "email" / "emails" — the thing a count limit counts.
    public static func noun(_ kind: StandingKind, _ count: Int) -> String {
        let one: String
        switch kind {
        case .sendEmail: one = "email"
        case .spendMoney: one = "payment"
        case .postComment: one = "comment"
        case .runCommand: one = "command"
        case .callAPI: one = "API call"
        case .other: one = "action"
        }
        return count == 1 ? one : one + "s"
    }

    public static func desk(_ desk: String) -> String { desk == "*" ? "Every desk" : desk }

    /// "atlas · Send email".
    public static func heading(_ p: StandingPolicy) -> String { "\(desk(p.desk)) · \(kindLabel(p.kind))" }

    /// "$1.20". The limits are in dollars on the wire, whatever the deck's currency.
    public static func usd(_ value: Double) -> String { String(format: "$%.2f", value) }

    /// Today's use against each limit: "43/80 emails today", "$1.20/$5.00 today".
    public static func usage(_ p: StandingPolicy) -> [String] {
        var lines: [String] = []
        if let cap = p.limits.countPerDay {
            lines.append("\(p.usage.count)/\(cap) \(noun(p.kind, cap)) today")
        }
        if let cap = p.limits.usdPerDay {
            lines.append("\(usd(p.usage.usd))/\(usd(cap)) today")
        }
        return lines
    }

    /// How full the fuller limit is, 0…1, for the meter. `nil` with no limit.
    public static func fill(_ p: StandingPolicy) -> Double? {
        var parts: [Double] = []
        if let cap = p.limits.countPerDay, cap > 0 { parts.append(Double(p.usage.count) / Double(cap)) }
        if let cap = p.limits.usdPerDay, cap > 0 { parts.append(p.usage.usd / cap) }
        return parts.max().map { min(max($0, 0), 1) }
    }

    /// "Up to 80 emails a day · up to $5.00 a day".
    public static func limits(_ limits: StandingLimits, kind: StandingKind) -> String {
        var parts: [String] = []
        if let n = limits.countPerDay { parts.append("up to \(n) \(noun(kind, n)) a day") }
        if let d = limits.usdPerDay { parts.append("up to \(usd(d)) a day") }
        guard let first = parts.first else { return "No limit set" }
        return ([first.prefix(1).uppercased() + first.dropFirst()] + parts.dropFirst()).joined(separator: " · ")
    }

    /// What it covers beyond desk and kind: the command pattern, recipients, account.
    public static func scope(_ p: StandingPolicy) -> String? {
        var parts: [String] = []
        if p.pattern != "*" && !p.pattern.isEmpty { parts.append(p.pattern) }
        if p.tool != "*" && !p.tool.isEmpty && p.kind != .runCommand { parts.append("via \(p.tool)") }
        if !p.limits.recipients.isEmpty { parts.append("to " + p.limits.recipients.joined(separator: ", ")) }
        if !p.limits.account.isEmpty { parts.append("as \(p.limits.account)") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// "Proposed by atlas", "No expiry", "Expires in 3 days", "Expired", "Revoked".
    public static func state(_ p: StandingPolicy, now: Date = Date()) -> String {
        switch p.status {
        case .proposed: return p.createdBy == "owner" ? "Proposed" : "Proposed by \(p.createdBy)"
        case .revoked: return "Revoked"
        case .other(let raw): return raw
        case .active:
            if p.expired { return "Expired" }
            guard let end = p.expiresAt else { return "No expiry" }
            return "Expires in " + span(end.timeIntervalSince(now))
        }
    }

    static func span(_ seconds: TimeInterval) -> String {
        let s = max(seconds, 0)
        if s < 3_600 { let m = max(Int(s / 60), 1); return "\(m) minute" + (m == 1 ? "" : "s") }
        if s < 86_400 { let h = Int(s / 3_600); return "\(h) hour" + (h == 1 ? "" : "s") }
        let d = Int(s / 86_400); return "\(d) day" + (d == 1 ? "" : "s")
    }

    /// One audit line: "atlas · send to b***@acme.com" over "44 today".
    public static func auditTitle(_ line: StandingAuditLine) -> String {
        line.desk.isEmpty ? line.action : "\(line.desk) · \(line.action)"
    }

    public static func auditDetail(_ line: StandingAuditLine) -> String {
        var parts = [kindLabel(line.kind)]
        if let cost = line.costUSD { parts.append(usd(cost)) }
        if let n = line.countAfter { parts.append("\(n) today") }
        if let d = line.usdAfter, d > 0 { parts.append("\(usd(d)) today") }
        return parts.joined(separator: " · ")
    }

    /// The deck's own rules, checked before sending so the form can say what is
    /// missing. The deck still decides; its refusal reason is shown if it refuses.
    public static func problem(_ d: StandingDraft) -> String? {
        if d.desk.trimmingCharacters(in: .whitespaces).isEmpty { return "Pick a desk, or every desk." }
        if let n = d.limits.countPerDay, n <= 0 { return "A daily count must be above zero." }
        if let u = d.limits.usdPerDay, u <= 0 { return "A daily dollar limit must be above zero." }
        if d.kind == .spendMoney && d.limits.usdPerDay == nil { return "Spending money needs a daily dollar limit." }
        if d.limits.countPerDay == nil && d.limits.usdPerDay == nil { return "Set a daily limit: a number, or dollars." }
        let pattern = d.pattern.trimmingCharacters(in: .whitespaces)
        if d.kind == .runCommand && (pattern.isEmpty || pattern == "*") {
            return "Name the commands, such as gh pr*."
        }
        return nil
    }

    /// The card sheet's check. The card names the commands itself, so only
    /// the limits are the sheet's to get right.
    public static func cardProblem(_ body: StandingFromCard, kind: StandingKind) -> String? {
        problem(StandingDraft(desk: "card", kind: kind, pattern: "card", limits: body.limits))
    }

    /// A typed limit: "80" → 80, "" → no limit, "abc" → no limit.
    public static func count(_ text: String) -> Int? { Int(text.trimmingCharacters(in: .whitespaces)) }
    public static func dollars(_ text: String) -> Double? {
        Double(text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "$", with: ""))
    }
}
