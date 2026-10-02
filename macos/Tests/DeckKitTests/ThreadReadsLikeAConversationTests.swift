import XCTest
@testable import DeckKit

/// **What the thread draws, decided as values (D2).**
///
/// ours-03: routine fires and "Hired: listing-closer…" drawn as his own blue
/// bubbles, and a footer that said "Idle — Its session is up and nothing is
/// running" under every quiet conversation. Grok draws the first as a centred
/// grey caption, marks where unread starts with `NEW`, and says nothing at all
/// about a desk that is simply quiet.
final class ThreadReadsLikeAConversationTests: XCTestCase {

    private let day = Date(timeIntervalSince1970: 1_790_000_000)

    private func said(
        _ id: String, _ author: String, _ role: MessageRole, _ text: String, minute: Double
    ) -> ThreadEntry {
        .said(TranscriptRow(
            message: Message(id: id, cursor: id, threadID: "direct:atlas", author: author,
                             role: role, sentAt: day.addingTimeInterval(minute * 60), text: text),
            attribution: .ordinary))
    }

    private var conversation: [ThreadEntry] {
        [
            said("r1", "routine", .system, "Morning Shorts report\nfive lines, what moved", minute: 0),
            said("a1", "atlas", .agent, "Shorts: 3 posted, 1 flagged.", minute: 1),
            said("d1", "deck", .system, "[Agent Deck] Hired: listing-closer now reports to you.", minute: 2),
            said("y1", DeckOwner.name, .owner, "who flagged it?", minute: 3),
            said("e1", "engineer", .system, "Deck check after today's deploy", minute: 4),
            said("a2", "atlas", .agent, "YouTube did.", minute: 5),
        ]
    }

    func testSystemLinesAreCentredCaptionsNamedByTheirSourceAndNeverBubbles() {
        let items = ThreadTimeline.items(conversation, desk: "atlas")
        let captions = items.compactMap { item -> String? in
            if case .caption(let caption) = item { return caption.text }
            return nil
        }
        XCTAssertEqual(captions, [
            "Routine · Morning Shorts report",
            "Deck · Hired: listing-closer now reports to you.",
            "Engineer · Deck check after today's deploy",
        ])
        let bubbles = items.compactMap { item -> String? in
            if case .entry(.said(let row)) = item { return row.id }
            return nil
        }
        XCTAssertEqual(bubbles, ["a1", "y1", "a2"], "a system line was drawn as somebody's bubble")
    }

    /// The whole text stays reachable: the caption shows one line of it.
    func testACaptionKeepsTheFullTextItStandsFor() throws {
        let items = ThreadTimeline.items(conversation, desk: "atlas")
        guard case .caption(let first)? = items.first(where: {
            if case .caption = $0 { return true } else { return false }
        }) else { return XCTFail("no caption") }
        XCTAssertEqual(first.fullText, "Morning Shorts report\nfive lines, what moved")
        XCTAssertEqual(first.source, "Routine")
    }

    /// `NEW` goes immediately above the first unread line, and nowhere when
    /// nothing is unread.
    func testTheNewDividerSitsAboveTheFirstUnreadLine() {
        let items = ThreadTimeline.items(conversation, desk: "atlas", newFrom: "a2")
        let ids = items.map(\.id)
        let divider = ids.firstIndex(of: TranscriptItem.newDividerID)
        let firstUnread = ids.firstIndex(of: "said:a2")
        XCTAssertNotNil(divider)
        XCTAssertEqual(divider.map { $0 + 1 }, firstUnread)

        XCTAssertFalse(ThreadTimeline.items(conversation, desk: "atlas").map(\.id)
            .contains(TranscriptItem.newDividerID))
        XCTAssertFalse(ThreadTimeline.items(conversation, desk: "atlas", newFrom: "gone").map(\.id)
            .contains(TranscriptItem.newDividerID), "a marker for a line not on screen draws nothing")
    }

    /// Unread counts the desk's own lines — not his, not a routine's.
    func testTheFirstUnreadIsCountedOnTheDesksOwnLines() {
        let messages = conversation.compactMap { entry -> Message? in
            if case .said(let row) = entry { return row.message }
            return nil
        }
        XCTAssertEqual(ThreadTimeline.firstUnread(in: messages, unread: 1), "a2")
        XCTAssertEqual(ThreadTimeline.firstUnread(in: messages, unread: 2), "a1")
        XCTAssertNil(ThreadTimeline.firstUnread(in: messages, unread: 0))
        XCTAssertEqual(ThreadTimeline.firstUnread(in: messages, unread: 9), "a1")
    }

    /// A quiet desk says nothing on the conversation; the things he has to act
    /// on, and a desk that is resting, still do.
    func testOnlyStatusesWorthReadingStayOnTheConversation() throws {
        func status(_ state: AgentState) -> DeskStatus? {
            var desk = Agent(name: "atlas", title: "COS")
            desk.state = state
            return DeskStatus.make(agent: desk, connection: .live, approvals: [], isSending: false)
        }
        XCTAssertEqual(status(.idle)?.showsOnConversation, false, "the Idle footer is back")
        XCTAssertEqual(status(.done)?.showsOnConversation, false)
        XCTAssertEqual(status(.working)?.showsOnConversation, false,
                       "working is the character under the last message, not a strip")
        XCTAssertEqual(status(.asleep)?.showsOnConversation, true)
        XCTAssertEqual(status(.needsYou)?.showsOnConversation, true)
        XCTAssertEqual(status(.offline)?.showsOnConversation, true)
        let dropped = try XCTUnwrap(DeskStatus.make(
            agent: nil, connection: .reconnecting(attempt: 1), approvals: [], isSending: false))
        XCTAssertTrue(dropped.showsOnConversation)
    }
}
