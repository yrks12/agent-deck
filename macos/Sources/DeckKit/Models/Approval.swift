import Foundation

/// The rule an option would write, as the deck describes it: same tool, same
/// folder, the argument widened only to its leading verb.
///
/// `pattern` is never redacted on the wire — it is what would actually be
/// stored, and showing a doctored version of it would be a lie.
public struct ApprovalRule: Hashable, Sendable, Decodable {
    public let id: String?
    /// `always_allow` or `deny`.
    public let kind: String
    public let tool: String
    public let pattern: String
    public let cwd: String
    /// Provenance for the *agent*, not for the device.
    public let note: String?

    enum CodingKeys: String, CodingKey { case id, kind, tool, pattern, cwd, note }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id)
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? ""
        tool = try container.decodeIfPresent(String.self, forKey: .tool) ?? ""
        pattern = try container.decode(String.self, forKey: .pattern)
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        note = try container.decodeIfPresent(String.self, forKey: .note)
        guard !pattern.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .pattern, in: container,
                debugDescription: "a rule with no pattern matches everything; refusing to model one"
            )
        }
    }
}

/// One of the three buttons on the card.
///
/// `summary` is required and non-empty *by construction*. There is no
/// initialiser that takes an option without it, and decoding one without it
/// throws — so there is no path in this app to a button that grants something
/// without saying what.
public struct ApprovalOption: Identifiable, Hashable, Sendable, Decodable {

    public enum Reply: String, Hashable, Sendable, Decodable {
        case once, always, never
    }

    public let reply: Reply
    /// False for the option the deck will not offer here — credentials,
    /// payments and irreversible actions ask every time. Drawn disabled *with
    /// its summary*, never greyed out silently.
    public let isAvailable: Bool
    /// The deck's own sentence for what this reply does. Shown, not the word.
    public let summary: String
    /// The rule that would be written. `nil` for `once` (which writes none) and
    /// for an option that is not available.
    public let rule: ApprovalRule?

    public var id: String { reply.rawValue }

    public var label: String {
        switch reply {
        case .once: return "Allow once"
        case .always: return "Always allow"
        case .never: return "Deny"
        }
    }

    /// What is drawn under the label. Never empty.
    public var ruleText: String { summary }

    /// `always` and `never` outlive this one request; `once` writes nothing.
    public var isStanding: Bool { reply != .once }

    enum CodingKeys: String, CodingKey { case reply, available, summary, rule }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        reply = try container.decode(Reply.self, forKey: .reply)
        isAvailable = try container.decodeIfPresent(Bool.self, forKey: .available) ?? true
        summary = try container.decode(String.self, forKey: .summary)
        rule = try container.decodeIfPresent(ApprovalRule.self, forKey: .rule)

        guard !summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .summary, in: container,
                debugDescription: "an option with no sentence cannot be drawn as a button"
            )
        }
        // A standing grant on offer must arrive with the rule it would write.
        // The sentence alone is not enough: it would describe something this
        // client never saw.
        if isAvailable, reply != .once, rule == nil {
            throw DecodingError.dataCorruptedError(
                forKey: .rule, in: container,
                debugDescription: "\(reply.rawValue) is on offer with no rule attached"
            )
        }
    }
}

/// A tool call an agent stopped on. §12 of the contract, verbatim.
public struct Approval: Identifiable, Hashable, Sendable, Decodable {
    public let id: String
    /// The desk, when the deck could work out which one; otherwise exactly what
    /// the permission hook recorded. `deskKnown` says which of the two it is.
    public let agentName: String
    /// **Always what the hook wrote down** — a raw session id, most often. The
    /// only handle on an ask that matches no desk on the roster.
    public let askedBy: String
    /// Whether `agentName` is a desk on the board. A client that cannot tell a
    /// resolved desk from a fallback files the card under a name that is not
    /// one.
    public let deskKnown: Bool
    public let tool: String
    /// The command or argument, already redacted by the deck.
    public let subject: String
    public let cwd: String
    /// The same folder, tilde-shortened, for prose.
    public let cwdShort: String
    public let status: String
    public let requestedAt: Date?
    public let options: [ApprovalOption]
    /// §23: whether this card can become a standing approval with a limit.
    /// `nil` from a deck too old to say.
    public let standingOption: StandingOption?

    /// Where the card is drawn: inline in that agent's own conversation.
    public var threadID: String { "direct:\(agentName)" }

    enum CodingKeys: String, CodingKey {
        case id, ts, agent, tool, subject, cwd, status, options
        case cwdShort = "cwd_short"
        case askedBy = "asked_by"
        case deskKnown = "desk_known"
        case standingOption = "standing_option"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        agentName = try container.decode(String.self, forKey: .agent)
        askedBy = try container.decodeIfPresent(String.self, forKey: .askedBy) ?? agentName
        // Absent means a deck too old to have done the join, and then the only
        // honest answer is "this client cannot tell" — so the roster is asked
        // rather than a session id being drawn as a colleague.
        deskKnown = try container.decodeIfPresent(Bool.self, forKey: .deskKnown) ?? false
        tool = try container.decodeIfPresent(String.self, forKey: .tool) ?? ""
        subject = try container.decodeIfPresent(String.self, forKey: .subject) ?? ""
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        let short = try container.decodeIfPresent(String.self, forKey: .cwdShort)
        cwdShort = (short?.isEmpty == false) ? short! : cwd
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "pending"
        requestedAt = try container.decodeIfPresent(Double.self, forKey: .ts)
            .map(Date.init(timeIntervalSince1970:))
        // Not `decodeIfPresent`: a question with no answerable options is not a
        // card, it is a thing to go and look at in the terminal.
        let options = try container.decode([ApprovalOption].self, forKey: .options)
        self.options = options
        standingOption = (try? container.decodeIfPresent(StandingOption.self, forKey: .standingOption)) ?? nil
        guard !options.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .options, in: container,
                debugDescription: "approval \(id) offers nothing to answer with"
            )
        }
    }
}

/// `GET /v1/approvals`. Unreadable entries are counted rather than thrown away
/// silently — a request nobody can see is still a session sitting blocked.
public struct ApprovalsPage: Hashable, Sendable, Decodable {
    public var approvals: [Approval]
    public var unreadable: Int

    public init(approvals: [Approval], unreadable: Int = 0) {
        self.approvals = approvals
        self.unreadable = unreadable
    }

    enum CodingKeys: String, CodingKey { case approvals }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        var list = try container.nestedUnkeyedContainer(forKey: .approvals)
        var good: [Approval] = []
        var bad = 0
        // Decoded one at a time: one malformed entry must not cost the deck
        // every other approval on the page.
        while !list.isAtEnd {
            if let approval = try? list.decode(Approval.self) {
                good.append(approval)
            } else {
                _ = try? list.decode(AnyJSONValue.self)
                bad += 1
            }
        }
        approvals = good
        unreadable = bad
    }

    public var unreadableNotice: String? {
        guard unreadable > 0 else { return nil }
        let noun = unreadable == 1 ? "approval request" : "approval requests"
        let verb = unreadable == 1 ? "could not be read and is" : "could not be read and are"
        return "\(unreadable) \(noun) \(verb) not shown here. Answer it in the agent's own terminal."
    }
}

/// Skips one element of an unkeyed container whatever shape it is. `Decodable`
/// has no "step over this" primitive, so it is spelled out here once.
struct AnyJSONValue: Decodable {
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { return }
        if (try? container.decode(Bool.self)) != nil { return }
        if (try? container.decode(Double.self)) != nil { return }
        if (try? container.decode(String.self)) != nil { return }
        if (try? container.decode([String: AnyJSONValue].self)) != nil { return }
        if (try? container.decode([AnyJSONValue].self)) != nil { return }
    }
}

/// What `POST /v1/approvals/{id}` answers with. §12, and every field of it is
/// something he has to be told.
///
/// This client used to throw the whole response away — `_ = try await raw(…)`
/// — so a tap that wrote a **standing permission on his Mac** left nothing on
/// screen but a card that had disappeared. `rule` is what was actually written,
/// and `resumed` is the contract's own point: *"a card that says restarted
/// rather than just clearing is the difference between him knowing the work
/// moved and him wondering whether it did."*
public struct ApprovalDecision: Hashable, Sendable, Decodable {
    /// `once` | `always` | `never`, off the returned `ask.answered`.
    public let answered: String
    /// The rule the deck just wrote. `nil` for `once`, which writes none.
    public let rule: ApprovalRule?
    /// Whether the desk was told, in words, that it may carry on with this
    /// exact action. `false` is correct for `never`, and also happens when the
    /// ask had already been resumed once.
    public let resumed: Bool

    public init(answered: String, rule: ApprovalRule?, resumed: Bool) {
        self.answered = answered
        self.rule = rule
        self.resumed = resumed
    }

    enum CodingKeys: String, CodingKey { case ask, rule, resumed }
    private struct Ask: Decodable { let answered: String? }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        answered = try container.decodeIfPresent(Ask.self, forKey: .ask)?.answered ?? ""
        rule = try container.decodeIfPresent(ApprovalRule.self, forKey: .rule)
        resumed = try container.decodeIfPresent(Bool.self, forKey: .resumed) ?? false
    }
}

/// Everything the inline card draws, decided here so the view holds no rules.
public struct ApprovalCard: Hashable, Sendable, Identifiable {
    public static let disclosureTitle = "Show the details"

    /// **What this card is, right now.** Every one of these is a different
    /// thing for him to do, which is why they are five values and not a flag:
    /// one needs an answer, three are a record of what he decided, and the last
    /// is a question somebody else settled while he was looking at it.
    public enum Status: Hashable, Sendable {
        case waitingOnYou
        case allowedOnce
        case alwaysAllowed
        case denied
        /// Answered in the agent's own terminal, or swept after four hours.
        /// Carries the deck's own sentence for which of those it was.
        case settledElsewhere(String)

        /// The pill, in the reference product's own register.
        public var pill: String {
            switch self {
            case .waitingOnYou: return "Waiting on you"
            case .allowedOnce: return "Allowed once"
            case .alwaysAllowed: return "Always allowed"
            case .denied: return "Denied"
            case .settledElsewhere: return "Settled elsewhere"
            }
        }
    }

    public var approvalID: String
    public var title: String
    /// The tool the desk stopped on — `Bash`, `Write`, `WebFetch`. Drawn on the
    /// card and used as the line the transcript reads out.
    public var tool: String
    /// When the desk asked. This is what puts the card in the conversation at
    /// the moment it happened rather than under everything ever said.
    public var at: Date?
    /// "Runs on the reference app's computer" — the reference's own line, and a true
    /// one: this executes on the machine that agent is sitting on.
    public var runsOn: String
    public var why: String
    public var disclosureTitle: String
    public var details: String
    public var options: [ApprovalOption]
    public var status: Status
    /// One sentence saying what the answer actually did — the rule that was
    /// written, and whether the desk was restarted. `nil` while nobody has
    /// answered: there is no outcome to state yet.
    public var outcome: String?
    /// §23 "Always, up to a limit…", when the deck offers it for this card.
    public var standingOption: StandingOption? = nil

    public var id: String { approvalID }

    /// Offered beside "Always" only while the question is live.
    public var offersStanding: Bool { isAnswerable && standingOption?.isAvailable == true }

    public var statusPill: String { status.pill }

    /// Only a live question draws buttons. A settled card that still offered to
    /// grant something would be an invitation to double-grant it.
    public var isAnswerable: Bool { status == .waitingOnYou }

    public static func make(
        approval: Approval,
        agents: [String: Agent],
        decision: ApprovalDecision? = nil,
        settledElsewhere: String? = nil,
        standing: [StandingPolicy] = []
    ) -> ApprovalCard {
        let shown = agents[approval.agentName]?.displayName
            ?? Agent(name: approval.agentName, title: "").displayName
        // §12 sends no prose for "why", so it is composed from what it does
        // send — the tool, the folder and the fact that the agent stopped —
        // rather than invented.
        let why = "\(shown) stopped here: this \(approval.tool) call is not covered by its rules. "
            + "It would run in \(approval.cwdShort)."
        let status = standing.isEmpty || settledElsewhere != nil
            ? Self.status(decision: decision, settledElsewhere: settledElsewhere) : .alwaysAllowed
        return ApprovalCard(
            approvalID: approval.id,
            title: approval.subject,
            tool: approval.tool,
            at: approval.requestedAt,
            runsOn: "Runs on \(shown)'s computer",
            why: why,
            disclosureTitle: disclosureTitle,
            details: "\(approval.tool)\n\(approval.subject)\nin \(approval.cwd)",
            options: approval.options,
            status: status,
            outcome: Self.outcome(
                status: status, decision: decision, approval: approval, desk: shown,
                settledElsewhere: settledElsewhere, standing: standing
            ),
            standingOption: approval.standingOption
        )
    }

    private static func status(
        decision: ApprovalDecision?, settledElsewhere: String?
    ) -> Status {
        if let settledElsewhere { return .settledElsewhere(settledElsewhere) }
        switch decision?.answered {
        case "once": return .allowedOnce
        case "always": return .alwaysAllowed
        case "never": return .denied
        default: return .waitingOnYou
        }
    }

    /// **What his tap did**, in one sentence, built only out of what the deck
    /// sent back. The scope comes off the rule the deck says it wrote — never
    /// off the button he pressed, because the button is what he *asked* for and
    /// the rule is what he *got*.
    private static func outcome(
        status: Status,
        decision: ApprovalDecision?,
        approval: Approval,
        desk: String,
        settledElsewhere: String?,
        standing: [StandingPolicy] = []
    ) -> String? {
        if let settledElsewhere { return settledElsewhere }
        if let policy = standing.first {
            // §23: a limited standing approval, and this one let through.
            let limits = StandingPresentation.limits(policy.limits, kind: policy.kind)
            let moved = decision?.resumed == true ? " \(desk) was told to carry on with it." : ""
            return "Allowed from now on within a limit: \(limits)." + moved
        }
        guard let decision else { return nil }

        let scope: String
        if let rule = decision.rule {
            let verb = rule.kind == "deny" ? "Refused" : "Allowed"
            let tool = rule.tool.isEmpty ? approval.tool : rule.tool
            scope = "\(verb) `\(rule.pattern)` for \(tool) in \(approval.cwdShort) from now on."
        } else if status == .denied {
            scope = "Refused this one time; no rule was written."
        } else {
            scope = "Allowed this one time; no rule was written."
        }

        // §12.1: answering does not click the button in the agent's terminal.
        // What it buys is that the desk is *told* to do the thing — and only
        // when `resumed` says it was.
        let moved = decision.resumed
            ? "\(desk) was told to carry on with it."
            : "Nothing was restarted."
        return "\(scope) \(moved)"
    }
}

/// **One button in the tray, whatever kind of thing is waiting behind it.**
///
/// The two kinds of block are answered with different verbs down different
/// routes — `once` / `always` / `never` to `POST /v1/approvals/{id}`, `done` /
/// `skipped` to `POST /v1/handoffs/{id}` — and they must never be confused,
/// because "always allow" writes a standing permission on his Mac and "skipped"
/// tells a desk to abandon a job for good.
///
/// So the verb is carried, not the word. The view draws a label and a sentence
/// and knows nothing about either route; the store looks the verb up against
/// the ask the deck really sent and answers that.
public struct AttentionAction: Identifiable, Hashable, Sendable {

    public enum Verb: Hashable, Sendable {
        /// A permission the desk is asking for.
        case permission(ApprovalOption.Reply)
        /// What a human did about a step the desk could not take at all.
        case handoff(HandoffOutcome)
    }

    public let verb: Verb
    public let label: String
    /// **The deck's own sentence for what this does.** Never empty: both
    /// option types refuse to decode without one.
    public let summary: String
    /// False for a reply the deck will not offer here — credentials, payments,
    /// anything irreversible. Drawn disabled *with its sentence*.
    public let isAvailable: Bool
    /// Whether it outlives this one question. `always` and `never` write a rule
    /// that applies from now on; `once`, `done` and `skipped` settle this ask
    /// and nothing else.
    public let isStanding: Bool

    public var id: String {
        switch verb {
        case .permission(let reply): return "permission.\(reply.rawValue)"
        case .handoff(let outcome): return "handoff.\(outcome.rawValue)"
        }
    }

    public init(
        verb: Verb, label: String, summary: String,
        isAvailable: Bool = true, isStanding: Bool = false
    ) {
        self.verb = verb
        self.label = label
        self.summary = summary
        self.isAvailable = isAvailable
        self.isStanding = isStanding
    }

    public init(_ option: ApprovalOption) {
        self.init(
            verb: .permission(option.reply), label: option.label,
            summary: option.summary, isAvailable: option.isAvailable,
            isStanding: option.isStanding)
    }

    public init(_ option: HandoffOption) {
        self.init(
            verb: .handoff(option.outcome), label: option.label,
            summary: option.summary, isAvailable: option.isAvailable,
            // Only `always` writes a rule (a grant for that desk + site).
            // `skipped` is permanent for *this* job and nothing beyond it, so
            // calling it standing would say the wrong thing out loud.
            isStanding: option.outcome == .always)
    }
}

/// **One thing on this deck that has stopped and is waiting on him — wherever
/// it is, and whichever kind of stopped it is.**
///
/// `ApprovalCard` is drawn *inside a conversation*, so it only exists on screen
/// while he is looking at the desk that raised it. An ask on `acme-growth`
/// raised while he is reading `atlas` therefore reaches him nowhere: the
/// session sits blocked, and the only thing that tells him is a WhatsApp
/// message from the agent — *"agents asking me on whatsup to confirm but not on
/// screen whats the point"*.
///
/// This is that ask, shaped for a tray pinned above the whole deck. It carries
/// the desk, so he knows where to look; what it wants, so he knows what he is
/// agreeing to; where it happens; and the deck's own options, each with the
/// sentence that ships with it.
///
/// **Two kinds of stopped, one strip.** A permission question is one the desk
/// could answer itself if he says yes. A secure handoff is one no permission
/// fixes, because the desk cannot act at all — a 2FA code, a CAPTCHA,
/// `gh auth login`. They arrive from two routes shaped the same way, and they
/// are the same thing to him: something of his is not moving until he does
/// something. So they share a tray and sort together, oldest first, because
/// the oldest block is the one that has been costing the most.
///
/// **The unattributed case is the point, not an edge.** The deck says whether
/// it could resolve the desk (`desk_known`) and keeps the raw session id it was
/// filed under (`asked_by`). When it could not, the ask is carried here with
/// what the deck *did* say and says plainly that the desk is unknown. An ask
/// nothing can attribute is exactly the ask that would otherwise be invisible.
public struct AttentionItem: Identifiable, Hashable, Sendable {

    /// Who is waiting. Two cases and no third: either a desk answers to the
    /// name the deck sent, or nothing does — and then this says so, rather than
    /// dressing a session id up as a colleague.
    public enum Desk: Hashable, Sendable {
        /// `name` is what the deck and every thread id key on; `shown` is how
        /// it is written on screen. Both, because the card has to *say* one and
        /// *go to* the other.
        case known(name: String, shown: String)
        case unattributed(String)
    }

    /// What kind of stopped this is, and what it needs. The two carry different
    /// facts because they *are* different — a tool call has a subject and a
    /// folder; a handoff has an instruction and the state of the work.
    public enum Need: Hashable, Sendable {
        /// A tool call the desk stopped on. It could run it, with a yes.
        case permission(tool: String, subject: String)
        /// A step the desk cannot take at all. `state` is the load-bearing
        /// half: what has already happened, which is what decides whether he
        /// opens a laptop now or after dinner.
        case handoff(kind: String, needs: String, state: String)
    }

    public let askID: String
    public let desk: Desk
    public let need: Need
    /// Where it happens: the folder a tool call would run in, tilde-shortened,
    /// or the site, host or app a handoff sends him to.
    public let place: String
    public let actions: [AttentionAction]
    public let at: Date?
    /// §23 "Always, up to a limit…" for a permission question, when offered.
    public let standingOption: StandingOption?

    public var offersStanding: Bool { standingOption?.isAvailable == true }

    public var id: String { askID }

    /// The line at the top of the item: which desk stopped.
    public var deskLine: String {
        switch desk {
        case .known(_, let shown): return shown
        case .unattributed: return "A desk this deck did not name"
        }
    }

    /// The desk's wire name, for going to it. `nil` when nothing on this deck
    /// answers to what the ask was filed under — and then there is nowhere to
    /// go, so nothing offers to take him there.
    public var deskName: String? {
        if case .known(let name, _) = desk { return name }
        return nil
    }

    /// **The way to the place where he can actually do it**, for a step no
    /// permission can lift. `nil` for a permission question, which he answers
    /// from the strip itself, and `nil` for a desk that cannot be found.
    ///
    /// A 2FA code or a sign-in happens somewhere, and that somewhere is the
    /// desk's own conversation and its screen. Without this the card tells him
    /// to go and do something and leaves him to find the desk himself, in a
    /// sidebar he is not currently looking at — which is most of the cost of
    /// the block in the first place.
    public var openDeskLabel: String? {
        guard isHandoff, case .known(_, let shown) = desk else { return nil }
        return "Open \(shown)"
    }

    public var isAttributed: Bool {
        if case .known = desk { return true }
        return false
    }

    /// **Whether a permission would even help.** The tray draws both, and this
    /// is the difference he has to be able to see: one of them he can answer
    /// from the chair he is in.
    public var isHandoff: Bool {
        if case .handoff = need { return true }
        return false
    }

    /// What it wants, in one line. For a tool call the tool is kept in front of
    /// the subject, because `rm -rf build` means one thing under `Bash` and
    /// another quoted inside a `Write`. For a handoff it is the deck's own
    /// instruction, which is already a sentence written for a phone.
    public var request: String {
        switch need {
        case .handoff(_, let needs, _):
            return needs
        case .permission(let tool, let subject):
            switch (tool.isEmpty, subject.isEmpty) {
            case (false, false): return "\(tool) · \(subject)"
            case (false, true): return "a \(tool) call"
            case (true, false): return subject
            case (true, true): return "a tool call it did not describe"
            }
        }
    }

    /// Where it would run, or where he has to go. Empty when the deck sent
    /// nothing: an invented one would be the most misleading string on the
    /// card. Suppressed for a handoff whose instruction already names it, so
    /// the card does not say the same thing twice.
    public var whereLine: String {
        guard !place.isEmpty else { return "" }
        switch need {
        case .permission: return "in \(place)"
        case .handoff(_, let needs, _):
            return needs.contains(place) ? "" : "at \(place)"
        }
    }

    /// **The state of the work right now.** Only a handoff has one, and for a
    /// handoff it is the line that decides what he does about it: a payment
    /// halfway through is not a login screen sitting idle.
    public var situation: String {
        if case .handoff(_, _, let state) = need { return state }
        return ""
    }

    /// What the deck knew instead of a desk. Empty for an attributed ask,
    /// because then there is nothing to explain.
    public var evidence: String {
        guard case .unattributed(let raw) = desk else { return "" }
        guard !raw.isEmpty else {
            return "The deck named nothing at all for this one."
        }
        return "The deck filed it under \(raw), and no desk on this roster answers to that."
    }

    /// The whole item as one sentence, for the screen reader — read before the
    /// buttons, so the answer is not the first thing announced.
    public var spoken: String {
        let opening = isHandoff
            ? "\(deskLine) cannot go on without you."
            : "\(deskLine) is waiting on you."
        let place = whereLine.isEmpty ? "" : ", \(whereLine)"
        let now = situation.isEmpty ? "" : " Right now: \(situation)"
        let extra = evidence.isEmpty ? "" : " \(evidence)"
        return spell("\(opening) \(request)\(place).\(now)\(extra)")
    }

    /// **What this button will do, said out loud, before it is pressed.** Never
    /// the bare label: "Always allow" writes a standing permission on his Mac,
    /// "Skip this step" abandons a job for good, and with two desks waiting
    /// each must also say *which* desk it answers.
    public func spokenAction(_ action: AttentionAction) -> String {
        let sentence = spell(action.summary)
        guard action.isAvailable else {
            return "\(action.label) for \(deskLine). Not available. \(sentence)"
        }
        let scope = action.isStanding ? "From now on" : "This time only"
        return "\(action.label) for \(deskLine). \(scope): \(sentence)"
    }

    /// Backticks are silence in VoiceOver — a pattern wrapped in them is read
    /// as nothing at all, which is how "never ask again" loses its object.
    private func spell(_ text: String) -> String {
        text.replacingOccurrences(of: "`", with: "")
    }

    public init(
        askID: String, desk: Desk, need: Need, place: String,
        actions: [AttentionAction], at: Date?, standingOption: StandingOption? = nil
    ) {
        self.askID = askID
        self.desk = desk
        self.need = need
        self.place = place
        self.actions = actions
        self.at = at
        self.standingOption = standingOption
    }

    /// **Who this belongs to, off the deck's own join first.**
    ///
    /// `desk_known` is the deck saying whether it resolved the name; the roster
    /// is the fallback for a deck too old to say. A name the deck resolved is
    /// used even when the roster has not caught up with it yet, because the
    /// deck is the authority on its own board.
    static func desk(named name: String, askedBy: String, deckSaysKnown: Bool,
                     agents: [String: Agent]) -> Desk {
        if let onRoster = agents[name] {
            return .known(name: name, shown: onRoster.displayName)
        }
        if deckSaysKnown {
            return .known(name: name, shown: Agent.displayName(forWireName: name))
        }
        return .unattributed(askedBy)
    }

    /// Built off the roster the app is already holding. A desk it cannot find
    /// is not an error, and not a reason to drop the ask.
    public static func make(approval: Approval, agents: [String: Agent]) -> AttentionItem {
        AttentionItem(
            askID: approval.id,
            desk: desk(named: approval.agentName, askedBy: approval.askedBy,
                       deckSaysKnown: approval.deskKnown, agents: agents),
            need: .permission(tool: approval.tool, subject: approval.subject),
            place: approval.cwdShort,
            actions: approval.options.map(AttentionAction.init),
            at: approval.requestedAt,
            standingOption: approval.standingOption
        )
    }

    /// The same, for the block no permission can lift.
    public static func make(handoff: Handoff, agents: [String: Agent]) -> AttentionItem {
        AttentionItem(
            askID: handoff.id,
            desk: desk(named: handoff.agentName, askedBy: handoff.askedBy,
                       deckSaysKnown: handoff.deskKnown, agents: agents),
            need: .handoff(kind: handoff.kind, needs: handoff.needs, state: handoff.state),
            place: handoff.place,
            actions: handoff.buttons.map(AttentionAction.init),
            at: handoff.requestedAt
        )
    }
}
