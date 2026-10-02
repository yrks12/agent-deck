import Foundation

/// One tool call as the conversation should draw it: the ask, plus what became
/// of it. `decision` and `settledElsewhere` are both `nil` while it is still a
/// live question.
public struct ApprovalEntry: Hashable, Sendable {
    public let approval: Approval
    /// What the deck did when he answered — the rule, and whether the desk was
    /// resumed.
    public let decision: ApprovalDecision?
    /// The deck's own sentence for a question somebody else settled: answered
    /// in the agent's terminal, or swept after four hours (§12.1).
    public let settledElsewhere: String?
    /// §23: the standing approvals "Always, up to a limit…" made from this card.
    public let standing: [StandingPolicy]

    public init(
        approval: Approval,
        decision: ApprovalDecision? = nil,
        settledElsewhere: String? = nil,
        standing: [StandingPolicy] = []
    ) {
        self.approval = approval
        self.decision = decision
        self.settledElsewhere = settledElsewhere
        self.standing = standing
    }
}

/// Holds the approvals waiting on this deck and answers them.
///
/// Nothing here is optimistic. A card is never marked answered until the deck
/// has accepted the decision, because a permission that looked granted and was
/// not — or looked denied and was not — is worse than a slow button.
///
/// **An answered card is kept, not deleted.** It used to be removed outright:
/// he tapped Approve, the card vanished, and the screen went back to exactly
/// what it had been, with nothing saying what had been granted on his Mac or
/// whether the desk had carried on. The record of the work is the work he
/// cannot otherwise see.
public actor ApprovalsModel {
    /// How many settled tool calls one window keeps. The deck sweeps them off
    /// `/v1/approvals` the moment they are answered, so this list is the only
    /// copy — but it is a *conversation*, not an audit log, and an unbounded
    /// one would grow all day inside a `LazyVStack`.
    static let settledKept = 30

    private let client: DeckClient
    /// Answered or expired asks, oldest first, keyed by ask id.
    private var settled: [String: ApprovalEntry] = [:]
    private var settledOrder: [String] = []

    public private(set) var page = ApprovalsPage(approvals: [], unreadable: 0)
    /// What went wrong, in the user's words. `nil` means the last exchange was
    /// clean — which is not the same as "there are no approvals".
    public private(set) var problem: String?
    public private(set) var isBusy = false

    public init(client: DeckClient) {
        self.client = client
    }

    public var notice: String? { page.unreadableNotice }
    public var all: [Approval] { page.approvals }

    public func load() async {
        do {
            page = try await client.approvals()
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    /// **Every live question on the deck, oldest first, whoever raised it.**
    ///
    /// This is deliberately not filtered by anything. `pending(forAgent:)`
    /// below answers the conversation's question — *what belongs in this
    /// thread* — and by construction it can only ever return asks it can
    /// attribute to a desk. An ask carrying something no desk answers to falls
    /// out of every one of those filters and is drawn nowhere, which is the
    /// state the app shipped in: a blocked session with nothing on screen.
    ///
    /// So the tray reads this instead, and the attributing is done afterwards,
    /// where failing to attribute costs a label rather than the whole ask.
    public func waiting() -> [Approval] {
        page.approvals
            .filter { settled[$0.id] == nil }
            .sorted { ($0.requestedAt ?? .distantPast) < ($1.requestedAt ?? .distantPast) }
    }

    /// The live questions for one agent, oldest first — the order they were
    /// asked in.
    public func pending(forAgent name: String) -> [Approval] {
        waiting().filter { $0.agentName == name }
    }

    /// **Everything this agent's conversation should draw**: the questions
    /// still waiting, and the ones already decided, in the order they were
    /// asked.
    ///
    /// A settled entry wins over the deck's copy of the same ask. The deck's
    /// collector runs at about 1 Hz, so for a tick or so after an answer the
    /// question is still on `/v1/approvals` as `pending` — and without this
    /// join the next poll would flip the card back to a live question and
    /// offer to grant the same permission a second time.
    public func entries(forAgent name: String) -> [ApprovalEntry] {
        let live = pending(forAgent: name).map { ApprovalEntry(approval: $0) }
        let done = settledOrder.compactMap { settled[$0] }
            .filter { $0.approval.agentName == name }
        return (live + done).sorted {
            ($0.approval.requestedAt ?? .distantFuture) < ($1.approval.requestedAt ?? .distantFuture)
        }
    }

    public func answer(approval: Approval, with option: ApprovalOption) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let decision = try await client.decideApproval(id: approval.id, option: option)
            settle(approval, ApprovalEntry(approval: approval, decision: decision))
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
            switch error {
            // The question is settled or gone, and this tap is not what
            // settled it. The card stays, saying so in the deck's own words —
            // clearing it would read exactly like a tap that worked, which is
            // §12's "a reply that lands on a settled question must not look
            // like it decided something".
            case .alreadyAnswered, .approvalExpired, .unknownAsk:
                settle(approval, ApprovalEntry(
                    approval: approval, settledElsewhere: error.userFacingText
                ))
            default:
                break
            }
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    /// **"Always, up to a limit…"** (§23): the card's own route makes the
    /// policies and answers this one `once`. Settled only when the deck says
    /// so; a refusal keeps the question live with the deck's reason.
    @discardableResult
    public func stand(approval: Approval, with body: StandingFromCard, via standing: StandingCardClient) async -> Bool {
        guard let option = approval.standingOption, option.isAvailable else {
            problem = DeckError.standingRefused(.neverCoverable, detail: approval.standingOption?.summary ?? "").userFacingText
            return false
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let made = try await standing.standFromCard(route: option.route, body)
            settle(approval, ApprovalEntry(approval: approval, decision: made.answered, standing: made.policies))
            problem = nil
            return true
        } catch let error as DeckError {
            problem = error.userFacingText
            if error == .unknownAsk {
                settle(approval, ApprovalEntry(approval: approval, settledElsewhere: error.userFacingText))
            }
            return false
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
            return false
        }
    }

    private func settle(_ approval: Approval, _ entry: ApprovalEntry) {
        page.approvals.removeAll { $0.id == approval.id }
        if settled[approval.id] == nil { settledOrder.append(approval.id) }
        settled[approval.id] = entry
        while settledOrder.count > Self.settledKept {
            settled.removeValue(forKey: settledOrder.removeFirst())
        }
    }
}
