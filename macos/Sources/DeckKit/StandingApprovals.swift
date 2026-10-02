import Foundation

/// **Standing approvals — "always, up to a limit"** (`docs/client-api.md` §23).
///
/// The owner sets a policy once; a desk then acts within it without a card.
/// These are the wire shapes. What they say on screen is `StandingPresentation`'s.

/// The five kinds a policy can cover. An unknown kind from a newer deck is kept
/// as its raw slug rather than failing the row.
public enum StandingKind: Hashable, Sendable {
    case sendEmail, spendMoney, postComment, runCommand, callAPI
    case other(String)

    public static let all: [StandingKind] = [.sendEmail, .spendMoney, .postComment, .runCommand, .callAPI]

    public init(wire: String) {
        switch wire {
        case "send_email": self = .sendEmail
        case "spend_money": self = .spendMoney
        case "post_comment": self = .postComment
        case "run_command": self = .runCommand
        case "call_api": self = .callAPI
        default: self = .other(wire)
        }
    }

    public var wire: String {
        switch self {
        case .sendEmail: return "send_email"
        case .spendMoney: return "spend_money"
        case .postComment: return "post_comment"
        case .runCommand: return "run_command"
        case .callAPI: return "call_api"
        case .other(let raw): return raw
        }
    }
}

public enum StandingStatus: Hashable, Sendable {
    case proposed, active, revoked, other(String)

    public init(wire: String) {
        switch wire {
        case "proposed": self = .proposed
        case "active": self = .active
        case "revoked": self = .revoked
        default: self = .other(wire)
        }
    }
}

/// `limits`. `nil` is "no limit of this kind"; it is sent as an explicit
/// `null`, because on a PATCH a missing key keeps the old limit and a `null`
/// removes it.
public struct StandingLimits: Hashable, Sendable, Codable {
    public var countPerDay: Int?
    public var usdPerDay: Double?
    /// Addresses or domains; for `call_api`, URL hosts. Empty = any.
    public var recipients: [String]
    /// The sender/account it must use. Empty = any.
    public var account: String

    public init(countPerDay: Int? = nil, usdPerDay: Double? = nil, recipients: [String] = [], account: String = "") {
        self.countPerDay = countPerDay; self.usdPerDay = usdPerDay
        self.recipients = recipients; self.account = account
    }

    enum CodingKeys: String, CodingKey {
        case recipients, account
        case countPerDay = "count_per_day"
        case usdPerDay = "usd_per_day"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        countPerDay = (try? c.decodeIfPresent(Int.self, forKey: .countPerDay)) ?? nil
        usdPerDay = (try? c.decodeIfPresent(Double.self, forKey: .usdPerDay)) ?? nil
        recipients = ((try? c.decodeIfPresent([String].self, forKey: .recipients)) ?? nil) ?? []
        account = ((try? c.decodeIfPresent(String.self, forKey: .account)) ?? nil) ?? ""
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if let countPerDay { try c.encode(countPerDay, forKey: .countPerDay) } else { try c.encodeNil(forKey: .countPerDay) }
        if let usdPerDay { try c.encode(usdPerDay, forKey: .usdPerDay) } else { try c.encodeNil(forKey: .usdPerDay) }
        try c.encode(recipients, forKey: .recipients)
        try c.encode(account, forKey: .account)
    }
}

/// Today's use of one policy (the deck's day, America/New_York).
public struct StandingUsage: Hashable, Sendable, Decodable {
    public var day: String
    public var count: Int
    public var usd: Double

    public init(day: String = "", count: Int = 0, usd: Double = 0) { self.day = day; self.count = count; self.usd = usd }

    enum CodingKeys: String, CodingKey { case day, count, usd }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = ((try? c.decodeIfPresent(String.self, forKey: .day)) ?? nil) ?? ""
        count = ((try? c.decodeIfPresent(Int.self, forKey: .count)) ?? nil) ?? 0
        usd = ((try? c.decodeIfPresent(Double.self, forKey: .usd)) ?? nil) ?? 0
    }
}

/// One policy row, as `GET /v1/standing-approvals` draws it.
public struct StandingPolicy: Hashable, Sendable, Decodable, Identifiable {
    public var id: String
    /// A desk name, or `"*"` for every desk.
    public var desk: String
    public var kind: StandingKind
    public var tool: String
    public var pattern: String
    public var limits: StandingLimits
    public var expiresAt: Date?
    /// `"owner"`, or the desk that proposed it.
    public var createdBy: String
    public var createdAt: Date?
    public var approvedAt: Date?
    public var revokedAt: Date?
    public var status: StandingStatus
    public var note: String
    /// Active and not expired: it is being honoured now.
    public var live: Bool
    public var expired: Bool
    public var usage: StandingUsage

    public init(id: String, desk: String, kind: StandingKind, tool: String = "*", pattern: String = "*",
                limits: StandingLimits, expiresAt: Date? = nil, createdBy: String = "owner",
                createdAt: Date? = nil, approvedAt: Date? = nil, revokedAt: Date? = nil,
                status: StandingStatus = .active, note: String = "", live: Bool = true, expired: Bool = false,
                usage: StandingUsage = StandingUsage()) {
        self.id = id; self.desk = desk; self.kind = kind; self.tool = tool; self.pattern = pattern
        self.limits = limits; self.expiresAt = expiresAt; self.createdBy = createdBy; self.createdAt = createdAt
        self.approvedAt = approvedAt; self.revokedAt = revokedAt; self.status = status; self.note = note
        self.live = live; self.expired = expired; self.usage = usage
    }

    enum CodingKeys: String, CodingKey {
        case id, desk, kind, tool, pattern, limits, status, note, live, expired, usage
        case expiresAt = "expires_at", createdBy = "created_by", createdAt = "created_at"
        case approvedAt = "approved_at", revokedAt = "revoked_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        desk = ((try? c.decodeIfPresent(String.self, forKey: .desk)) ?? nil) ?? "*"
        kind = StandingKind(wire: ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "")
        tool = ((try? c.decodeIfPresent(String.self, forKey: .tool)) ?? nil) ?? "*"
        pattern = ((try? c.decodeIfPresent(String.self, forKey: .pattern)) ?? nil) ?? "*"
        limits = ((try? c.decodeIfPresent(StandingLimits.self, forKey: .limits)) ?? nil) ?? StandingLimits()
        expiresAt = WireDate.read(c, .expiresAt)
        createdBy = ((try? c.decodeIfPresent(String.self, forKey: .createdBy)) ?? nil) ?? "owner"
        createdAt = WireDate.read(c, .createdAt)
        approvedAt = WireDate.read(c, .approvedAt)
        revokedAt = WireDate.read(c, .revokedAt)
        status = StandingStatus(wire: ((try? c.decodeIfPresent(String.self, forKey: .status)) ?? nil) ?? "")
        note = ((try? c.decodeIfPresent(String.self, forKey: .note)) ?? nil) ?? ""
        live = ((try? c.decodeIfPresent(Bool.self, forKey: .live)) ?? nil) ?? false
        expired = ((try? c.decodeIfPresent(Bool.self, forKey: .expired)) ?? nil) ?? false
        usage = ((try? c.decodeIfPresent(StandingUsage.self, forKey: .usage)) ?? nil) ?? StandingUsage()
    }
}

/// One action a policy covered, newest first.
public struct StandingAuditLine: Hashable, Sendable, Decodable, Identifiable {
    public var at: Date?
    public var desk: String
    public var tool: String
    /// Already redacted by the deck.
    public var action: String
    public var kind: StandingKind
    public var policyID: String
    public var countAfter: Int?
    public var usdAfter: Double?
    public var costUSD: Double?

    public var id: String { "\(policyID)|\(at?.timeIntervalSince1970 ?? 0)|\(action)" }

    public init(at: Date?, desk: String, tool: String, action: String, kind: StandingKind, policyID: String,
                countAfter: Int? = nil, usdAfter: Double? = nil, costUSD: Double? = nil) {
        self.at = at; self.desk = desk; self.tool = tool; self.action = action; self.kind = kind
        self.policyID = policyID; self.countAfter = countAfter; self.usdAfter = usdAfter; self.costUSD = costUSD
    }

    enum CodingKeys: String, CodingKey {
        case ts, desk, tool, action, kind
        case policyID = "policy_id", countAfter = "count_after", usdAfter = "usd_after", costUSD = "cost_usd"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        at = WireDate.read(c, .ts)
        desk = ((try? c.decodeIfPresent(String.self, forKey: .desk)) ?? nil) ?? ""
        tool = ((try? c.decodeIfPresent(String.self, forKey: .tool)) ?? nil) ?? ""
        action = ((try? c.decodeIfPresent(String.self, forKey: .action)) ?? nil) ?? ""
        kind = StandingKind(wire: ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "")
        policyID = ((try? c.decodeIfPresent(String.self, forKey: .policyID)) ?? nil) ?? ""
        countAfter = (try? c.decodeIfPresent(Int.self, forKey: .countAfter)) ?? nil
        usdAfter = (try? c.decodeIfPresent(Double.self, forKey: .usdAfter)) ?? nil
        costUSD = (try? c.decodeIfPresent(Double.self, forKey: .costUSD)) ?? nil
    }
}

/// The body of `POST /v1/standing-approvals`, and (as a whole) of a PATCH: an
/// edit sends every editable field, so what he sees on the form is what is stored.
public struct StandingDraft: Hashable, Sendable {
    public var desk: String
    public var kind: StandingKind
    public var tool: String
    public var pattern: String
    public var limits: StandingLimits
    public var expiresAt: Date?
    public var note: String

    public init(desk: String = "", kind: StandingKind = .sendEmail, tool: String = "*", pattern: String = "*",
                limits: StandingLimits = StandingLimits(), expiresAt: Date? = nil, note: String = "") {
        self.desk = desk; self.kind = kind; self.tool = tool; self.pattern = pattern
        self.limits = limits; self.expiresAt = expiresAt; self.note = note
    }

    /// The form, filled from a row he is editing.
    public init(_ policy: StandingPolicy) {
        self.init(desk: policy.desk, kind: policy.kind, tool: policy.tool, pattern: policy.pattern,
                  limits: policy.limits, expiresAt: policy.expiresAt, note: policy.note)
    }
}

/// Every route of §23. The owner's credential, the same bearer as every `/v1` route.
public protocol StandingApprovalsClient: Sendable {
    func standingApprovals(status: String?, desk: String?) async throws -> [StandingPolicy]
    func createStanding(_ draft: StandingDraft) async throws -> StandingPolicy
    func updateStanding(id: String, _ draft: StandingDraft) async throws -> StandingPolicy
    func approveStanding(id: String) async throws -> StandingPolicy
    /// `DELETE`: the row is kept, `status: "revoked"`, for the audit.
    func revokeStanding(id: String) async throws -> StandingPolicy
    func standingAudit(desk: String?, policyID: String?, limit: Int) async throws -> [StandingAuditLine]
}
