import Foundation

// Two Claude accounts on one deck (docs/plans/2026-10-01-two-accounts.md).
//
// Everything here is ADDITIVE on the wire: a deck that has not shipped it
// sends none of these keys, and every type below decodes to "nothing" rather
// than failing, so an older deck keeps drawing exactly what it drew.

/// Epoch seconds or ISO-8601 text; anything else is "unknown".
enum WireDate {
    static func read<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Date? {
        if let seconds = try? c.decodeIfPresent(Double.self, forKey: key) {
            return Date(timeIntervalSince1970: seconds)
        }
        guard let text = try? c.decodeIfPresent(String.self, forKey: key) else { return nil }
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        return full.date(from: text) ?? plain.date(from: text)
    }
}

/// `plan` is `{"name","subscription","tier"}` or a bare string.
enum WirePlan {
    private struct Object: Decodable { var name: String?; var subscription: String? }

    static func read<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String? {
        if let text = try? c.decodeIfPresent(String.self, forKey: key) { return text }
        guard let object = try? c.decodeIfPresent(Object.self, forKey: key) else { return nil }
        if let name = object.name, !name.isEmpty { return name }
        return object.subscription.flatMap { $0.isEmpty ? nil : $0.capitalized }
    }
}

/// One account's meter, from `GET /v1/usage` `accounts[]`.
public struct AccountUsage: Equatable, Sendable, Decodable, Identifiable {
    public var id: String
    public var label: String
    /// `subscription` or `api`.
    public var kind: String
    public var plan: String?
    public var available: Bool
    public var stale: Bool
    public var reason: String?
    public var windows: [ClaudeUsage.Window]
    public var fetchedAt: Date?
    /// When this account's login stops refreshing. Near it, he must sign in again.
    public var refreshExpiresAt: Date?
    /// Names of the desks running on this account.
    public var desks: [String]

    enum CodingKeys: String, CodingKey {
        case id, label, kind, plan, available, stale, reason, windows, desks
        case fetchedAt = "fetched_at"
        case refreshExpiresAt = "refresh_expires_at"
    }

    public init(id: String, label: String? = nil, kind: String = "subscription", plan: String? = nil,
                available: Bool = true, stale: Bool = false, reason: String? = nil,
                windows: [ClaudeUsage.Window] = [], fetchedAt: Date? = nil,
                refreshExpiresAt: Date? = nil, desks: [String] = []) {
        self.id = id
        self.label = label ?? id
        self.kind = kind
        self.plan = plan
        self.available = available
        self.stale = stale
        self.reason = reason
        self.windows = windows.filter { $0.key == "session" } + windows.filter { $0.key != "session" }
        self.fetchedAt = fetchedAt
        self.refreshExpiresAt = refreshExpiresAt
        self.desks = desks
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        let name = try? c.decodeIfPresent(String.self, forKey: .label)
        label = (name?.isEmpty == false) ? name! : id
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? "subscription"
        plan = WirePlan.read(c, .plan)
        available = (try? c.decodeIfPresent(Bool.self, forKey: .available)) ?? false
        stale = (try? c.decodeIfPresent(Bool.self, forKey: .stale)) ?? false
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
        let all = (try? c.decodeIfPresent([ClaudeUsage.Window].self, forKey: .windows)) ?? []
        windows = all.filter { $0.key == "session" } + all.filter { $0.key != "session" }
        fetchedAt = WireDate.read(c, .fetchedAt)
        refreshExpiresAt = WireDate.read(c, .refreshExpiresAt)
        desks = (try? c.decodeIfPresent([String].self, forKey: .desks)) ?? []
    }
}

/// `policy` on `GET /v1/usage`: `fixed | failover | failover_api`.
///
/// Read-only here: the plan has no write route for it (it lives in
/// `deck.toml [accounts]` on the box).
public struct AccountPolicy: Equatable, Sendable, Decodable {
    public var mode: String
    public var thresholdPct: Int? = nil
    public var failoverAllowed: Bool? = nil
    public var defaultAccount: String? = nil

    /// Any mode but `fixed` moves desks on its own at the threshold.
    public var isAutoSwitch: Bool { mode == "failover" || mode == "failover_api" }

    enum CodingKeys: String, CodingKey {
        case mode
        case thresholdPct = "threshold_pct"
        case failoverAllowed = "failover_allowed"
        case defaultAccount = "default"
    }

    public init(mode: String, thresholdPct: Int? = nil, failoverAllowed: Bool? = nil, defaultAccount: String? = nil) {
        self.mode = mode
        self.thresholdPct = thresholdPct
        self.failoverAllowed = failoverAllowed
        self.defaultAccount = defaultAccount
    }

    public init(from decoder: Decoder) throws {
        // A bare string is read as the mode.
        if let text = try? decoder.singleValueContainer().decode(String.self) {
            mode = text
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = try c.decode(String.self, forKey: .mode)
        thresholdPct = try? c.decodeIfPresent(Int.self, forKey: .thresholdPct)
        failoverAllowed = try? c.decodeIfPresent(Bool.self, forKey: .failoverAllowed)
        defaultAccount = try? c.decodeIfPresent(String.self, forKey: .defaultAccount)
    }
}

/// One entry of `GET /v1/accounts`: who he can move a desk to.
public struct DeckAccount: Equatable, Sendable, Decodable, Identifiable {
    public var id: String
    public var label: String
    public var kind: String
    public var plan: String?

    enum CodingKeys: String, CodingKey { case id, label, kind, plan }

    public init(id: String, label: String? = nil, kind: String = "subscription", plan: String? = nil) {
        self.id = id
        self.label = label ?? id
        self.kind = kind
        self.plan = plan
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        let name = try? c.decodeIfPresent(String.self, forKey: .label)
        label = (name?.isEmpty == false) ? name! : id
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? "subscription"
        plan = WirePlan.read(c, .plan)
    }
}

/// Reads an array where one bad element is skipped, not fatal.
struct Lossy<Element: Decodable>: Decodable {
    var elements: [Element]

    /// Accepts anything, so the container moves past an element that did not read.
    private struct Skip: Decodable { init(from decoder: Decoder) throws {} }

    init(from decoder: Decoder) throws {
        var list = try decoder.unkeyedContainer()
        var out: [Element] = []
        while !list.isAtEnd {
            if let element = try? list.decode(Element.self) { out.append(element) }
            else { _ = try? list.decode(Skip.self) }
        }
        elements = out
    }
}

/// `GET /v1/accounts` is either a bare array or `{"accounts": [...]}`.
struct AccountList: Decodable {
    var accounts: [DeckAccount]

    private enum CodingKeys: String, CodingKey { case accounts }

    init(from decoder: Decoder) throws {
        if let bare = try? decoder.singleValueContainer().decode(Lossy<DeckAccount>.self) {
            accounts = bare.elements
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        accounts = (try? c.decode(Lossy<DeckAccount>.self, forKey: .accounts))?.elements ?? []
    }
}

/// Why `POST /v1/agents/{name}/account` did not move the desk. The reason
/// decides, the status only breaks a tie when no reason came.
public enum AccountMoveError: Error, Equatable, Sendable {
    /// 409 `not_idle`: mid-turn, or an ask is open. Try again when it is idle.
    case notIdle(detail: String)
    /// 404 `no_account`: the deck does not know that account.
    case noAccount(detail: String)
    /// 502 `move_failed`: the resume failed; the deck put the desk back.
    case moveFailed(detail: String)
    /// A bare 404: this deck has no accounts route.
    case unsupported
    /// Anything else the deck says (`unknown_agent`, 401, transport…).
    case other(DeckError)

    init(status: Int, body: Data) {
        struct Envelope: Decodable { let reason: String?; let detail: String? }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body)
        let reason = envelope?.reason ?? ""
        let detail = envelope?.detail ?? ""
        switch reason {
        case "not_idle": self = .notIdle(detail: detail)
        case "no_account": self = .noAccount(detail: detail)
        case "move_failed": self = .moveFailed(detail: detail)
        case "":
            switch status {
            case 404: self = .unsupported
            case 409: self = .notIdle(detail: detail)
            case 502: self = .moveFailed(detail: detail)
            default: self = .other(DeckError(status: status, body: body))
            }
        default: self = .other(DeckError(status: status, body: body))
        }
    }

    public var userFacingText: String {
        switch self {
        case .notIdle(let detail):
            return detail.isEmpty ? "This desk is busy. Try again when it is idle." : "This desk is busy: \(detail)"
        case .noAccount(let detail):
            return detail.isEmpty ? "The deck does not know that account." : detail
        case .moveFailed(let detail):
            return "The move did not work, and the desk is still on its old account." + (detail.isEmpty ? "" : " \(detail)")
        case .unsupported:
            return "This deck cannot switch accounts yet."
        case .other(let error):
            return error.userFacingText
        }
    }
}

/// A client that can list accounts and move a desk between them. Separate
/// from `DeckClient` on purpose: a deck without the routes is a state, not an
/// error, and no conformer can silently inherit a not-wired default.
public protocol AccountMover: Sendable {
    /// `nil` when the deck does not serve `GET /v1/accounts`.
    func accounts() async throws -> [DeckAccount]?
    func move(agent: String, to account: String) async throws
}
