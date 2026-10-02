import Foundation

/// Holds the secure handoffs waiting on this deck and answers them.
///
/// A near-twin of `ApprovalsModel`, on purpose and not by accident: the two
/// routes are shaped the same, the tray draws them together, and the rules
/// about a question somebody else settled are identical. What is *not* shared
/// is the meaning of an answer — an approval writes a rule, a handoff tells a
/// desk what a human did — so they stay two types rather than one with a flag.
///
/// **Nothing here is optimistic.** A card is never cleared until the deck has
/// accepted the answer. A tray that emptied on a tap the deck rejected reads
/// exactly like one that worked, and the desk on the other end is still stopped.
public actor HandoffsModel {

    private let client: DeckClient
    /// Answered or expired blocks, keyed by id, so the next poll cannot raise
    /// one that has just been settled. The deck sweeps them off the route
    /// within a tick, but a tick is long enough to draw the card again and
    /// invite a second answer.
    private var settled: Set<String> = []

    public private(set) var page = HandoffsPage(handoffs: [], unreadable: 0)
    /// What went wrong, in the user's words. `nil` means the last exchange was
    /// clean — which is not the same as "nothing is stuck".
    public private(set) var problem: String?

    public init(client: DeckClient) {
        self.client = client
    }

    public var notice: String? { page.unreadableNotice }

    public func load() async {
        do {
            page = try await client.handoffs()
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    /// Every block still waiting on a human, oldest first — the deck's own
    /// order, because the oldest block is the one that has been costing the
    /// most and none of them can be made cheaper by a rule.
    public func waiting() -> [Handoff] {
        page.handoffs
            .filter { settled.contains($0.id) == false }
            .sorted { ($0.requestedAt ?? .distantPast) < ($1.requestedAt ?? .distantPast) }
    }

    public func handoff(id: String) -> Handoff? {
        page.handoffs.first { $0.id == id }
    }

    /// Tells the desk what he did. `done` orders a re-check, `skipped` orders
    /// abandonment; the deck owns both texts, so this cannot drift from the
    /// WhatsApp reply that means the same thing.
    public func resolve(_ handoff: Handoff, as outcome: HandoffOutcome) async {
        do {
            _ = try await client.resolveHandoff(id: handoff.id, outcome: outcome)
            settle(handoff)
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
            switch error {
            // Settled or gone, and this tap is not what settled it. The card
            // goes, because it is genuinely no longer answerable — but the
            // deck's own sentence stays on screen saying this tap changed
            // nothing, which is the opposite of a silent success.
            case .alreadyAnswered, .approvalExpired, .unknownAsk:
                settle(handoff)
            default:
                break
            }
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    private func settle(_ handoff: Handoff) {
        settled.insert(handoff.id)
        page.handoffs.removeAll { $0.id == handoff.id }
    }
}
