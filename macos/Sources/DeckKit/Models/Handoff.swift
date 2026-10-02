import Foundation

/// **The two answers a secure handoff offers, and they are different
/// instructions rather than different words.**
///
/// `done` means he says he did it, so the desk goes back and *re-checks that
/// the step actually worked* — a human saying "done" is a claim, and the code
/// may have expired between the message and the tap. `skipped` means it will
/// never happen, so the desk *abandons that path for good* and reports what it
/// can no longer finish. Collapse the two and a skipped handoff becomes a login
/// screen hammered until something locks.
///
/// The deck's third outcome, `taken_over`, is deliberately not here. It is the
/// WhatsApp verb for "I have the keyboard right now", which leaves the work
/// unfinished and the card on the board — a button that changes nothing he can
/// see is a button that gets tapped twice.
///
/// `always` is the browser card's third answer ("Allow always for this site"):
/// the deck offers it as `always_option`, beside `options`, and writes a
/// durable grant for that desk + site when it arrives.
public enum HandoffOutcome: String, Hashable, Sendable, Decodable {
    case done, skipped, always
}

/// One of the two buttons on a handoff card.
///
/// `summary` is required and non-empty by construction, for the same reason
/// `ApprovalOption`'s is: a button that does not say what it will do is the
/// most dangerous control this app can draw. Here it is not a permission being
/// granted but a **desk being told what happened**, and telling it the wrong
/// one of these two is what makes it retry for ever or give up on work that is
/// finished.
public struct HandoffOption: Identifiable, Hashable, Sendable, Decodable {
    public let outcome: HandoffOutcome
    /// The deck offers both, always. Kept because the shape is an approval's
    /// and a future floor — a payment nobody may skip — would arrive here.
    public let isAvailable: Bool
    /// The deck's own sentence for what this reply tells the desk.
    public let summary: String

    public var id: String { outcome.rawValue }

    /// The reference product's own words, and they are the right ones: they
    /// say what happens next rather than naming a state.
    public var label: String {
        switch outcome {
        case .done: return "I'm done, continue"
        case .skipped: return "Skip this step"
        case .always: return "Allow always for this site"
        }
    }

    public init(outcome: HandoffOutcome, isAvailable: Bool = true, summary: String) {
        self.outcome = outcome
        self.isAvailable = isAvailable
        self.summary = summary
    }

    enum CodingKeys: String, CodingKey { case reply, available, summary }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        outcome = try container.decode(HandoffOutcome.self, forKey: .reply)
        isAvailable = try container.decodeIfPresent(Bool.self, forKey: .available) ?? true
        summary = try container.decode(String.self, forKey: .summary)
        guard !summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .summary, in: container,
                debugDescription: "an option with no sentence cannot be drawn as a button"
            )
        }
    }
}

/// **A step only a human can take, and the desk is stopped until one is.**
///
/// This is not an approval. An approval is a *permission* question — the desk
/// could do the thing and needs a yes, and the yes becomes a durable rule. A
/// handoff is the case where no yes helps because the desk **cannot act at
/// all**: a 2FA code arriving on a phone, a CAPTCHA, an SMS confirmation,
/// `gh auth login`, signing into Google. There is nothing to grant.
///
/// It arrives shaped like an approval on purpose — the same `agent` /
/// `asked_by` / `desk_known` join, the same `options` carrying their own words
/// — so one tray draws both and filters both on the same field.
public struct Handoff: Identifiable, Hashable, Sendable, Decodable {
    public let id: String
    /// The desk, when the deck could work out which one; otherwise exactly what
    /// was recorded. `deskKnown` is which of those two it is.
    public let agentName: String
    /// Always what was written down when the desk blocked — a session id, most
    /// often. The only handle on an ask nothing can attribute.
    public let askedBy: String
    public let deskKnown: Bool
    /// `2fa` | `captcha` | `login` | `payment` | `device_code` | `other`.
    public let kind: String
    /// What he must physically do, in one sentence.
    public let needs: String
    /// **The state of the work right now**, and the load-bearing field. Without
    /// it the card says "I need you" and nothing about whether money is already
    /// being spent — which is how somebody opens a laptop in a panic over a
    /// draft that never went live.
    public let state: String
    /// A url, a host, an app or a path: where he has to go.
    public let place: String
    /// A captured screen path, or an excerpt of the desk's own output.
    public let evidence: String
    public let status: String
    public let requestedAt: Date?
    public let options: [HandoffOption]
    /// "Allow always for this site", on a browser card only. Separate from
    /// `options` on the wire so an older app, which decodes those as exactly
    /// done|skipped, keeps working.
    public let alwaysOption: HandoffOption?

    /// Every button the card draws, in order.
    public var buttons: [HandoffOption] { options + (alwaysOption.map { [$0] } ?? []) }

    /// Where the desk's own conversation is, when the deck could name it.
    public var threadID: String { "direct:\(agentName)" }

    enum CodingKeys: String, CodingKey {
        case id, ts, agent, kind, needs, state, evidence, status, options
        case alwaysOption = "always_option"
        case askedBy = "asked_by"
        case deskKnown = "desk_known"
        case place = "where"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        agentName = try container.decode(String.self, forKey: .agent)
        askedBy = try container.decodeIfPresent(String.self, forKey: .askedBy) ?? agentName
        // Absent means an older deck that never did the join, and then the only
        // honest answer is "this client cannot tell" — which is `false`, so the
        // roster is asked instead of a raw id being drawn as a colleague.
        deskKnown = try container.decodeIfPresent(Bool.self, forKey: .deskKnown) ?? false
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? "other"
        needs = try container.decode(String.self, forKey: .needs)
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? ""
        place = try container.decodeIfPresent(String.self, forKey: .place) ?? ""
        evidence = try container.decodeIfPresent(String.self, forKey: .evidence) ?? ""
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "waiting"
        requestedAt = try container.decodeIfPresent(Double.self, forKey: .ts)
            .map(Date.init(timeIntervalSince1970:))
        let options = try container.decode([HandoffOption].self, forKey: .options)
        self.options = options
        // Lenient: a malformed extra must never cost him the whole card.
        alwaysOption = (try? container.decodeIfPresent(HandoffOption.self, forKey: .alwaysOption)) ?? nil

        guard !needs.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .needs, in: container,
                debugDescription: "handoff \(id) does not say what a human has to do"
            )
        }
        guard !options.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .options, in: container,
                debugDescription: "handoff \(id) offers nothing to answer with"
            )
        }
    }
}

/// `GET /v1/handoffs`. Unreadable entries are counted rather than dropped, for
/// the same reason as `ApprovalsPage`: a block nobody can see is still a desk
/// sitting stopped.
public struct HandoffsPage: Hashable, Sendable, Decodable {
    public var handoffs: [Handoff]
    public var unreadable: Int

    public init(handoffs: [Handoff], unreadable: Int = 0) {
        self.handoffs = handoffs
        self.unreadable = unreadable
    }

    enum CodingKeys: String, CodingKey { case handoffs }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        var list = try container.nestedUnkeyedContainer(forKey: .handoffs)
        var good: [Handoff] = []
        var bad = 0
        while !list.isAtEnd {
            if let handoff = try? list.decode(Handoff.self) {
                good.append(handoff)
            } else {
                _ = try? list.decode(AnyJSONValue.self)
                bad += 1
            }
        }
        handoffs = good
        unreadable = bad
    }

    public var unreadableNotice: String? {
        guard unreadable > 0 else { return nil }
        let noun = unreadable == 1 ? "handed-over step" : "handed-over steps"
        let verb = unreadable == 1 ? "could not be read and is" : "could not be read and are"
        return "\(unreadable) \(noun) \(verb) not shown here. The desk is still waiting on it."
    }
}

/// What `POST /v1/handoffs/{id}` answers with.
///
/// `resumed` is the point of the whole feature and the thing an approval cannot
/// promise: resuming after a handoff is an ordinary user message into the
/// session, not a permission prompt already drawn on a screen. So `false` here
/// means the desk was **not** told, and that is something he has to be shown
/// rather than a detail.
public struct HandoffResolution: Hashable, Sendable, Decodable {
    public let outcome: String
    public let resumed: Bool

    public init(outcome: String, resumed: Bool) {
        self.outcome = outcome
        self.resumed = resumed
    }

    enum CodingKeys: String, CodingKey { case handoff, resumed }
    private struct Settled: Decodable { let status: String? }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        outcome = try container.decodeIfPresent(Settled.self, forKey: .handoff)?.status ?? ""
        resumed = try container.decodeIfPresent(Bool.self, forKey: .resumed) ?? false
    }
}
