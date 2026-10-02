import Foundation

/// Where a "Move to account…" has got to, per desk. Success leaves nothing:
/// the roster's badge is the proof.
public enum AccountMoveStatus: Equatable, Sendable {
    case moving
    case failed(String)
}

/// **What the account surfaces say**, decided once for the Mac sidebar and the
/// iPhone roster (docs/plans/2026-10-01-two-accounts.md "API/UI").
///
/// The rule under all of it: a deck with fewer than two accounts, or one that
/// predates accounts, shows exactly what it showed before. Nothing is hidden
/// behind a disabled control and nothing says "0 accounts".
public enum AccountsPresentation {

    // MARK: meters

    public struct Meter: Equatable, Identifiable, Sendable {
        public var id: String
        public var label: String
        public var plan: String?
        /// The rolling 5-hour window first, then one more: what fits a card.
        public var windows: [ClaudeUsage.Window]
        /// Why there are no bars, when there are none. Never a silent 0%.
        public var note: String?
        public var isStale: Bool
    }

    /// One meter per account, or `nil` when the single top-level meter is the
    /// right thing to draw (no accounts, or only one).
    public static func meters(_ usage: ClaudeUsage) -> [Meter]? {
        guard usage.accounts.count >= 2 else { return nil }
        return usage.accounts.map { account in
            let readable = account.available && !account.windows.isEmpty
            let note: String? = readable ? nil : account.reason.map {
                "Unavailable (\($0.replacingOccurrences(of: "_", with: " ")))"
            } ?? "Unavailable"
            return Meter(id: account.id, label: account.label, plan: account.plan,
                         windows: readable ? Array(account.windows.prefix(2)) : [],
                         note: note, isStale: account.stale)
        }
    }

    // MARK: badge

    /// The account a desk is on, as the label to draw; `nil` when there is no
    /// choice to show (fewer than two accounts).
    public static func badge(for agent: Agent, accounts: [AccountUsage], defaultID: String? = nil) -> String? {
        guard accounts.count >= 2 else { return nil }
        let id = currentID(of: agent, accounts: accounts, defaultID: defaultID)
        return accounts.first { $0.id == id }?.label ?? id
    }

    /// Where it really runs, else where it belongs, else the default (the
    /// policy's, else the first account).
    static func currentID(of agent: Agent, accounts: [AccountUsage], defaultID: String?) -> String {
        agent.runningAccount ?? agent.account ?? defaultID ?? accounts.first?.id ?? ""
    }

    // MARK: Move to account…

    public struct MoveOption: Equatable, Identifiable, Sendable {
        public var id: String
        public var label: String
        public var isCurrent: Bool
        public var disabledReason: String?
        public var isEnabled: Bool { disabledReason == nil }
    }

    /// Why this desk cannot be moved right now, or `nil` when it can. The deck
    /// has the last word (409 `not_idle` also covers an open ask or a subagent
    /// still working); this is the part the client can know.
    public static func moveDisabledReason(for agent: Agent) -> String? {
        switch agent.state {
        case .idle, .done, .asleep: return nil
        case .working: return "Working right now. Wait until it is idle."
        case .needsYou: return "Waiting for you. Answer it first."
        case .shell: return "In a shell. Wait until it is idle."
        case .dead: return "Not running."
        case .offline: return "Never started."
        }
    }

    public static func moveOptions(for agent: Agent, accounts: [AccountUsage], defaultID: String? = nil) -> [MoveOption] {
        guard accounts.count >= 2 else { return [] }
        let current = currentID(of: agent, accounts: accounts, defaultID: defaultID)
        let busy = moveDisabledReason(for: agent)
        return accounts.map { account in
            let isCurrent = account.id == current
            return MoveOption(id: account.id, label: account.label, isCurrent: isCurrent,
                              disabledReason: isCurrent ? "Already on \(account.label)" : busy)
        }
    }

    // MARK: expiry banner

    public struct ExpiryWarning: Equatable, Identifiable, Sendable {
        public var accountID: String
        public var label: String
        public var expiresAt: Date
        public var text: String
        public var id: String { accountID }
    }

    /// An account whose login stops refreshing within three days (or already
    /// has), soonest first. Only he can sign in again.
    public static func expiryWarnings(_ accounts: [AccountUsage], now: Date = Date()) -> [ExpiryWarning] {
        let window: TimeInterval = 3 * 86_400
        return accounts
            .compactMap { account -> ExpiryWarning? in
                guard let at = account.refreshExpiresAt, at.timeIntervalSince(now) <= window else { return nil }
                return ExpiryWarning(accountID: account.id, label: account.label, expiresAt: at,
                                     text: expiryText(label: account.label, id: account.id,
                                                      remaining: at.timeIntervalSince(now)))
            }
            .sorted { $0.expiresAt < $1.expiresAt }
    }

    private static func expiryText(label: String, id: String, remaining: TimeInterval) -> String {
        let fix = "Sign in again: deckctl login --account \(id)"
        guard remaining > 0 else { return "\(label): sign-in expired. \(fix)" }
        let days = Int(remaining / 86_400)
        let hours = Int(remaining / 3_600)
        let span: String
        if days >= 1 { span = days == 1 ? "1 day" : "\(days) days" }
        else if hours >= 1 { span = hours == 1 ? "1 hour" : "\(hours) hours" }
        else { span = "under an hour" }
        return "\(label): sign-in expires in \(span). \(fix)"
    }

    // MARK: auto-switch

    public struct AutoSwitch: Equatable, Sendable {
        public var isOn: Bool
        public var caption: String
        /// The deck has no route to change `policy`, so this app only reads it.
        public var isEditable: Bool
        public var note: String
    }

    /// The Settings row, or `nil` on a deck that sent no policy.
    public static func autoSwitch(_ policy: AccountPolicy?) -> AutoSwitch? {
        guard let policy else { return nil }
        let on = policy.isAutoSwitch
        let caption = on
            ? "Switch accounts on its own" + (policy.thresholdPct.map { " at \($0)%" } ?? " when one is nearly full")
            : "Desks stay on the account they were given"
        return AutoSwitch(isOn: on, caption: caption, isEditable: false,
                          note: "Set on the deck. This app cannot change it yet.")
    }
}
