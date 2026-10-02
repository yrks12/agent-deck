import Foundation

/// **A desk calling him** (`server/ringing.py`, `docs/calls.md`). Owner,
/// 2026-10-01: "settings to let Atlas call us by himself".
///
/// A desk rings with `mcp__deck__call_owner`. Every `GET /v1/owner/alerts`
/// page lists what is ringing (`OwnerAlertPage.ringing`) for its 30 seconds,
/// and both apps draw an incoming-call screen from it. Answer joins the same
/// live call as his own (`CallStart`, with the reason as `opening`); decline,
/// or no answer, and the desk's reason lands in his thread instead.
public struct IncomingRing: Codable, Hashable, Sendable, Identifiable {
    public let id: String
    public let agent: String
    public let threadID: String
    public let reason: String
    public let urgent: Bool
    public let createdAt: Double
    public let expiresAt: Double

    public init(id: String, agent: String, threadID: String? = nil, reason: String,
                urgent: Bool = false, createdAt: Double = 0, expiresAt: Double) {
        self.id = id; self.agent = agent; self.threadID = threadID ?? "direct:\(agent)"
        self.reason = reason; self.urgent = urgent
        self.createdAt = createdAt; self.expiresAt = expiresAt
    }

    enum CodingKeys: String, CodingKey {
        case id, agent, reason, urgent
        case threadID = "thread_id"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        agent = try c.decode(String.self, forKey: .agent)
        threadID = try c.decodeIfPresent(String.self, forKey: .threadID) ?? "direct:\(agent)"
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
        urgent = try c.decodeIfPresent(Bool.self, forKey: .urgent) ?? false
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        expiresAt = try c.decode(Double.self, forKey: .expiresAt)
    }

    /// "Atlas is calling" — what the screen and the notification say.
    public var callerLine: String { "\(Agent.displayName(forWireName: agent)) is calling" }

    public func secondsLeft(at now: Date) -> Int {
        max(0, Int((expiresAt - now.timeIntervalSince1970).rounded(.up)))
    }
}

/// **Which ring the screen shows.** PURE, so both apps decide it the same way:
/// the oldest ring still ringing that he has not already answered or turned
/// down on this device. A ring that left the feed, or ran out, is gone.
public struct IncomingCallPresenter: Equatable, Sendable {
    public private(set) var showing: IncomingRing?
    private var handled: [String] = []

    public init() {}

    @discardableResult
    public mutating func update(_ rings: [IncomingRing], now: Date) -> IncomingRing? {
        let at = now.timeIntervalSince1970
        showing = rings.first { $0.expiresAt > at && !handled.contains($0.id) }
        return showing
    }

    /// He tapped Answer or Decline here: never shown again, whatever a late
    /// page still says.
    public mutating func handle(_ id: String) {
        handled.append(id)
        if handled.count > 50 { handled.removeFirst(handled.count - 50) }
        if showing?.id == id { showing = nil }
    }
}

/// **Who may call him, and when** — `GET`/`PATCH /v1/calls/settings`.
public struct CallOwnerSettings: Codable, Equatable, Sendable {
    public enum Who: String, Codable, CaseIterable, Sendable {
        case chief, any
        public var label: String { self == .chief ? "Atlas only" : "Any desk" }
    }

    public enum When: String, Codable, CaseIterable, Sendable {
        case off, urgent, anytime
        public var label: String {
            switch self {
            case .off: return "Off"
            case .urgent: return "Urgent only"
            case .anytime: return "Anytime"
            }
        }
    }

    public var who: Who
    public var when: When
    /// "22:00-08:00" in `tz`; only urgent calls ring inside it.
    public var quietHours: String
    public var tz: String
    public var maxPerDay: Int
    /// For an hour (or one answered call): any allowed call rings, and the
    /// chief is told to call.
    public var callMeNow: Bool
    public var ringSeconds: Int

    /// The deck's public default: nobody rings him until he turns it on.
    public static let deckDefault = CallOwnerSettings(
        who: .chief, when: .off, quietHours: "22:00-08:00", tz: "America/New_York",
        maxPerDay: 3, callMeNow: false, ringSeconds: 30)

    public init(who: Who, when: When, quietHours: String, tz: String, maxPerDay: Int,
                callMeNow: Bool, ringSeconds: Int = 30) {
        self.who = who; self.when = when; self.quietHours = quietHours; self.tz = tz
        self.maxPerDay = maxPerDay; self.callMeNow = callMeNow; self.ringSeconds = ringSeconds
    }

    enum CodingKeys: String, CodingKey {
        case who, when, tz
        case quietHours = "quiet_hours"
        case maxPerDay = "max_per_day"
        case callMeNow = "call_me_now"
        case ringSeconds = "ring_seconds"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Self.deckDefault
        who = (try? c.decode(Who.self, forKey: .who)) ?? d.who
        when = (try? c.decode(When.self, forKey: .when)) ?? d.when
        quietHours = (try? c.decode(String.self, forKey: .quietHours)) ?? d.quietHours
        tz = (try? c.decode(String.self, forKey: .tz)) ?? d.tz
        maxPerDay = (try? c.decode(Int.self, forKey: .maxPerDay)) ?? d.maxPerDay
        callMeNow = (try? c.decode(Bool.self, forKey: .callMeNow)) ?? false
        ringSeconds = (try? c.decode(Int.self, forKey: .ringSeconds)) ?? d.ringSeconds
    }

    /// The fields that differ, as the PATCH body: only what he changed.
    public func changes(from old: CallOwnerSettings) -> [String: CallSettingValue] {
        var out: [String: CallSettingValue] = [:]
        if who != old.who { out["who"] = .text(who.rawValue) }
        if when != old.when { out["when"] = .text(when.rawValue) }
        if quietHours != old.quietHours { out["quiet_hours"] = .text(quietHours) }
        if tz != old.tz { out["tz"] = .text(tz) }
        if maxPerDay != old.maxPerDay { out["max_per_day"] = .number(maxPerDay) }
        if callMeNow != old.callMeNow { out["call_me_now"] = .flag(callMeNow) }
        return out
    }
}

/// One PATCH value. Sendable, unlike `[String: Any]`.
public enum CallSettingValue: Equatable, Sendable {
    case text(String), number(Int), flag(Bool)
    var json: Any {
        switch self {
        case .text(let s): return s
        case .number(let n): return n
        case .flag(let b): return b
        }
    }
}

/// **Answering, declining, and the settings.** A separate protocol, like
/// `DecisionClient`, so a transport that predates the routes does not conform
/// and the app says so instead of pretending.
public protocol IncomingCallClient: Sendable {
    /// `POST /v1/calls/incoming/{id}/answer` — the same 201 as `startCall`,
    /// plus `ringID` and `opening`.
    func answerRing(id: String) async throws -> CallStart
    /// `POST /v1/calls/incoming/{id}/decline` — the reason goes to his thread.
    func declineRing(id: String) async throws
    func callSettings() async throws -> CallOwnerSettings
    func updateCallSettings(_ changes: [String: CallSettingValue]) async throws -> CallOwnerSettings
}
