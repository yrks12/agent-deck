import Foundation

/// **`GET /v1/usage` — the Claude subscription meter.**
///
/// Built on the deck but not mounted everywhere yet, so the route's absence is
/// a state, not an error: `HTTPDeckClient.usage()` answers `nil` on a 404 and
/// the meter is simply not drawn. `available == false` is different — the deck
/// has the route and cannot read the plan — and is shown as such, never as 0%.
/// A client that can answer `GET /v1/usage`. The HTTP client does; the
/// fixture answers with a plausible meter; a client that cannot is simply not
/// one, and the app draws no meter.
public protocol ClaudeUsageSource: Sendable {
    func usage() async throws -> ClaudeUsage?
}

public struct ClaudeUsage: Equatable, Sendable, Decodable {
    /// What every meter is titled. The numbers come from an undocumented
    /// Anthropic endpoint (the API sends `unofficial: true`), so the label says so.
    public static let heading = "Claude usage · unofficial"

    public struct Window: Equatable, Sendable, Decodable, Identifiable {
        public var key: String
        public var label: String
        /// 0…100 as sent; may exceed 100 when over the limit.
        public var percent: Double?
        public var severity: String
        public var resetsAt: Date?

        public var id: String { key }

        /// 0…1 for a bar: over the limit is a full bar, unknown is empty.
        public var fraction: Double { min(max((percent ?? 0) / 100, 0), 1) }

        enum CodingKeys: String, CodingKey {
            case key, label, percent, severity
            case resetsAt = "resets_at"
        }

        public init(key: String, label: String, percent: Double?, severity: String = "normal", resetsAt: Date? = nil) {
            self.key = key
            self.label = label
            self.percent = percent
            self.severity = severity
            self.resetsAt = resetsAt
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            label = try c.decodeIfPresent(String.self, forKey: .label) ?? key
            percent = try? c.decodeIfPresent(Double.self, forKey: .percent)
            severity = (try? c.decodeIfPresent(String.self, forKey: .severity)) ?? "normal"
            resetsAt = ClaudeUsage.date(c, .resetsAt)
        }
    }

    public var available: Bool
    public var stale: Bool
    public var reason: String?
    public var plan: String?
    /// The rolling session window first, then the rest in the deck's order.
    public var windows: [Window]
    /// One meter per Claude account. Empty on a deck that has one account or
    /// has not shipped accounts at all: then the top-level fields are the meter.
    public var accounts: [AccountUsage]
    /// The auto-switch policy; `nil` on a deck without accounts.
    public var policy: AccountPolicy?

    enum CodingKeys: String, CodingKey { case available, stale, reason, plan, windows, accounts, policy }

    public init(available: Bool, stale: Bool = false, reason: String? = nil, plan: String? = nil, windows: [Window],
                accounts: [AccountUsage] = [], policy: AccountPolicy? = nil) {
        self.accounts = accounts
        self.policy = policy
        self.available = available
        self.stale = stale
        self.reason = reason
        self.plan = plan
        self.windows = Self.ordered(windows)
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = try c.decodeIfPresent(Bool.self, forKey: .available) ?? false
        stale = try c.decodeIfPresent(Bool.self, forKey: .stale) ?? false
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
        plan = Self.planName(c)
        windows = Self.ordered((try? c.decodeIfPresent([Window].self, forKey: .windows)) ?? [])
        accounts = (try? c.decodeIfPresent(Lossy<AccountUsage>.self, forKey: .accounts))?.elements ?? []
        policy = try? c.decodeIfPresent(AccountPolicy.self, forKey: .policy)
    }

    /// The deck sends `plan` as `{"name", "subscription", "tier"}`; an older
    /// deck sent a bare string. Decoding only the string dropped the object
    /// silently, so "Max 20×" never showed. The name when there is one, else
    /// the subscription, capitalised.
    private struct PlanObject: Decodable {
        var name: String?
        var subscription: String?
    }

    private static func planName(_ c: KeyedDecodingContainer<CodingKeys>) -> String? {
        if let text = try? c.decodeIfPresent(String.self, forKey: .plan) { return text }
        guard let object = try? c.decodeIfPresent(PlanObject.self, forKey: .plan) else { return nil }
        if let name = object.name, !name.isEmpty { return name }
        return object.subscription.flatMap { $0.isEmpty ? nil : $0.capitalized }
    }

    private static func ordered(_ windows: [Window]) -> [Window] {
        windows.filter { $0.key == "session" } + windows.filter { $0.key != "session" }
    }

    /// Epoch seconds or ISO-8601 text; anything else is "unknown".
    fileprivate static func date<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Date? {
        WireDate.read(c, key)
    }
}
