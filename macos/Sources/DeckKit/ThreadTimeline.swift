import Foundation

/// **One thing that happened in a conversation, whatever kind of thing it was.**
///
/// He held this app next to a product that draws a card per tool call *inline*,
/// between the messages, and asked why ours showed him nothing. Ours had the
/// cards — it drew them in a block welded under the entire transcript, so a
/// question asked at 09:00 sat below a message sent at 17:00 and read as the
/// newest thing on the screen. A tool call happens *at a time*. It belongs
/// where that time is, exactly like every other line.
///
/// This is an enum rather than a protocol so the set is closed and countable:
/// adding a kind of activity is a compile error in every view that draws one,
/// which is the only way a new card cannot quietly fail to be rendered.
public enum ThreadEntry: Identifiable, Hashable, Sendable {
    /// Something was said — his own line, the desk's answer, or (§6.2) traffic
    /// with another desk that he is overhearing. The row already carries which.
    case said(TranscriptRow)
    /// The desk stopped on a tool call. Live or settled; the card says which.
    case toolCall(ApprovalCard)

    public var id: String {
        switch self {
        case .said(let row): return "said:\(row.id)"
        case .toolCall(let card): return "tool:\(card.id)"
        }
    }

    /// When it happened. This is the only ordering key, and it is the deck's
    /// own timestamp in both cases — never the time the app noticed.
    public var at: Date? {
        switch self {
        case .said(let row): return row.message.sentAt
        case .toolCall(let card): return card.at
        }
    }
}

public enum ThreadTimeline {

    /// Messages and tool calls, one list, in the order they happened.
    ///
    /// **A card with no `ts` goes last, not first.** §12's `ts` is required in
    /// practice but optional in this client's decoder, and an ask this app
    /// cannot place in time is a *live* question — putting it at the top of a
    /// day's conversation would bury the thing he most needs to answer.
    ///
    /// The tie-break is the entry id, so two things the deck stamped in the
    /// same microsecond do not swap places between two draws of the same
    /// screen. An order that is not total is an order that flickers.
    public static func entries(
        rows: [TranscriptRow],
        toolCalls: [ApprovalCard]
    ) -> [ThreadEntry] {
        let all = rows.map(ThreadEntry.said) + toolCalls.map(ThreadEntry.toolCall)
        return all.sorted { left, right in
            let leftAt = left.at ?? .distantFuture
            let rightAt = right.at ?? .distantFuture
            if leftAt != rightAt { return leftAt < rightAt }
            return left.id < right.id
        }
    }

    /// **K4: what each decision card says now.**
    ///
    /// `overrides` are newer words from the deck — an SSE `decision` frame, or
    /// his own tap once the deck took it — and win over the copy the page
    /// carried, keeping the options the card was drawn with. An open card
    /// followed by a line **he** typed is `skipped` (the reference's `widgetSkipped`):
    /// he answered in words, and the buttons are no longer the question. A
    /// routine or a deck notice after it skips nothing.
    public static func settleDecisions(
        _ messages: [Message], overrides: [String: Decision]
    ) -> [Message] {
        guard messages.contains(where: { $0.decision != nil }) else { return messages }
        var settled = messages
        var ownerSpokeAfter = false
        for index in settled.indices.reversed() {
            if settled[index].isFromUser { ownerSpokeAfter = true }
            guard var decision = settled[index].decision else { continue }
            if let newer = overrides[decision.id] {
                decision.state = newer.state
                decision.answer = newer.answer
            }
            if decision.state == .open, ownerSpokeAfter { decision.state = .skipped }
            settled[index].decision = decision
        }
        return settled
    }
}
