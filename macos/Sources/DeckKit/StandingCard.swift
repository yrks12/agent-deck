import Foundation

// "Always, up to a limit…" on an approval card (§23, "From a card").

/// `standing_option` on an approval row: can this card become a limited standing approval?
public struct StandingOption: Hashable, Sendable, Decodable {
    public var isAvailable: Bool
    public var kind: StandingKind
    public var desk: String
    /// Where to POST the limits — the deck's own route, used as given.
    public var route: String
    public var summary: String

    public init(isAvailable: Bool, kind: StandingKind, desk: String, route: String, summary: String) {
        self.isAvailable = isAvailable; self.kind = kind; self.desk = desk; self.route = route; self.summary = summary
    }

    enum CodingKeys: String, CodingKey { case available, kind, desk, route, summary }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        route = ((try? c.decodeIfPresent(String.self, forKey: .route)) ?? nil) ?? ""
        // No route, nothing to post to: not offered, whatever `available` says.
        isAvailable = (((try? c.decodeIfPresent(Bool.self, forKey: .available)) ?? nil) ?? false) && !route.isEmpty
        kind = StandingKind(wire: ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "")
        desk = ((try? c.decodeIfPresent(String.self, forKey: .desk)) ?? nil) ?? ""
        summary = ((try? c.decodeIfPresent(String.self, forKey: .summary)) ?? nil) ?? ""
    }
}

/// The body of `POST /v1/approvals/{ask_id}/standing`.
public struct StandingFromCard: Hashable, Sendable {
    public var limits: StandingLimits
    public var expiresAt: Date?
    public var note: String

    public init(limits: StandingLimits, expiresAt: Date? = nil, note: String = "") {
        self.limits = limits; self.expiresAt = expiresAt; self.note = note
    }
}

/// What the from-a-card route answers: the policies it made, and the card's
/// own answer (`nil` when the card was already settled).
public struct StandingFromCardResult: Hashable, Sendable, Decodable {
    public var policies: [StandingPolicy]
    public var answered: ApprovalDecision?

    public init(policies: [StandingPolicy], answered: ApprovalDecision? = nil) {
        self.policies = policies; self.answered = answered
    }

    enum CodingKeys: String, CodingKey { case policies, answered }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        policies = (try? c.decodeIfPresent(Lossy<StandingPolicy>.self, forKey: .policies))?.elements ?? []
        answered = (try? c.decodeIfPresent(ApprovalDecision.self, forKey: .answered)) ?? nil
    }
}

/// `POST /v1/approvals/{ask_id}/standing`, at the card's own `standing_option.route`.
public protocol StandingCardClient: Sendable {
    func standFromCard(route: String, _ body: StandingFromCard) async throws -> StandingFromCardResult
}
