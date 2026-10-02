import Foundation

public extension AttentionItem {
    /// **Everything waiting on him, in one list**: approvals and handoffs
    /// together, oldest first across both (the oldest block has cost the most).
    /// Shared by the Mac's attention strip and the phone's attention tab, so
    /// the two can never order the same asks differently.
    static func queue(approvals: [Approval], handoffs: [Handoff], agents: [String: Agent]) -> [AttentionItem] {
        let asks = approvals.map { AttentionItem.make(approval: $0, agents: agents) }
        let blocks = handoffs.map { AttentionItem.make(handoff: $0, agents: agents) }
        return (asks + blocks).sorted {
            ($0.at ?? .distantPast, $0.askID) < ($1.at ?? .distantPast, $1.askID)
        }
    }
}
