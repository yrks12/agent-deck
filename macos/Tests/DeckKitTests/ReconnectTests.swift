import XCTest
@testable import DeckKit

/// Failure mode being pinned: the SSE stream drops mid-conversation, the client
/// reopens it and refetches, and the transcript ends up with a message twice —
/// or, worse, silently missing the two that arrived while it was down.
///
/// The deck's `since=` is *exclusive*, but its own docs are blunt that a slow
/// subscriber silently loses stream frames and that the stream is never the
/// source of truth. So a reopened stream replaying its last frame, and a
/// refetch overlapping what is already held, are both normal. Both are scripted
/// here on purpose.
final class ReconnectTests: XCTestCase {

    /// Never sleep in a test. The backoff is injected and does nothing.
    private let instantBackoff: @Sendable (Int) async throws -> Void = { _ in }

    private func syncAcrossADrop() -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        // First history fetch: the conversation so far.
        client.pages = [
            MessagePage(
                threadID: "t1",
                messages: (1...5).map { makeMessage("m\($0)", cursor: "000\($0)-m\($0)") },
                isReadOnly: false,
                participants: ["owner", "hemingway"]
            ),
            // Refetch after the drop. Overlaps m7 (already held) and carries the
            // two that were missed while the socket was down.
            MessagePage(
                threadID: "t1",
                messages: [
                    makeMessage("m7", cursor: "0007-m7"),
                    makeMessage("m8", cursor: "0008-m8"),
                    makeMessage("m9", cursor: "0009-m9"),
                ],
                isReadOnly: false,
                participants: ["owner", "hemingway"]
            ),
        ]
        client.feeds = [
            .emitThenDrop([
                .message(makeMessage("m6", cursor: "0006-m6"), readOnly: false),
                .message(makeMessage("m7", cursor: "0007-m7"), readOnly: false),
            ]),
            // The reopened stream replays m8, then delivers m10 and finishes.
            .emitThenFinish([
                .message(makeMessage("m8", cursor: "0008-m8"), readOnly: false),
                .message(makeMessage("m10", cursor: "0010-m10"), readOnly: false),
            ]),
        ]
        return client
    }

    func testEveryMessageSurvivesAReconnectExactlyOnceAndInOrder() async throws {
        let client = syncAcrossADrop()
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)

        try await sync.run()

        let ids = await sync.messages().map(\.id)
        XCTAssertEqual(
            ids,
            ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10"],
            "every message must appear exactly once, in cursor order, across the drop"
        )
        let cursors = await sync.messages().map(\.cursor)
        XCTAssertEqual(cursors, cursors.sorted(), "cursors sort lexically, and that is the order")
    }

    func testTheMessagesThatArrivedWhileTheStreamWasDownAreRecovered() async throws {
        let client = syncAcrossADrop()
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)

        try await sync.run()

        let ids = Set(await sync.messages().map(\.id))
        XCTAssertTrue(ids.contains("m8"), "m8 landed during the outage and must be refetched")
        XCTAssertTrue(ids.contains("m9"), "m9 landed during the outage and must be refetched")
    }

    func testTheRefetchEchoesBackTheCursorTheClientAlreadyHolds() async throws {
        let client = syncAcrossADrop()
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)

        try await sync.run()

        XCTAssertEqual(
            client.historyCursors,
            [nil, "0007-m7"],
            "first fetch is the newest page; the refetch echoes back the newest cursor held"
        )
    }

    func testTheConnectionIndicatorReportsReconnectingThenRecovers() async throws {
        let client = syncAcrossADrop()
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)

        try await sync.run()

        let history = await sync.connectionHistory
        XCTAssertTrue(
            history.contains(.reconnecting(attempt: 1)),
            "a dropped stream must surface as Reconnecting, got \(history)"
        )
        XCTAssertEqual(history.last, .live, "the client recovers on its own")
        XCTAssertEqual(ConnectionState.reconnecting(attempt: 1).label, "Reconnecting")
    }

    func testOutOfOrderStreamDeliveryIsSortedIntoPlace() async throws {
        let client = ScriptedDeckClient()
        client.pages = [
            MessagePage(threadID: "t1", messages: [makeMessage("m1", cursor: "0001-m1")],
                        isReadOnly: false, participants: ["owner", "hemingway"])
        ]
        client.feeds = [
            .emitThenFinish([
                .message(makeMessage("m3", cursor: "0003-m3"), readOnly: false),
                .message(makeMessage("m2", cursor: "0002-m2"), readOnly: false),
            ])
        ]
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)

        try await sync.run()

        let ids = await sync.messages().map(\.id)
        XCTAssertEqual(ids, ["m1", "m2", "m3"], "late arrivals belong at their cursor, not at the end")
    }

    func testIngestReportsWhatItActuallyAdded() {
        var store = ConversationStore()
        let first = store.ingest([
            makeMessage("m1", cursor: "0001-m1"), makeMessage("m2", cursor: "0002-m2"),
        ])
        XCTAssertEqual(first.inserted, 2)
        XCTAssertEqual(first.duplicatesDropped, 0)

        let second = store.ingest([
            makeMessage("m2", cursor: "0002-m2"), makeMessage("m3", cursor: "0003-m3"),
        ])
        XCTAssertEqual(second.inserted, 1, "only m3 is new")
        XCTAssertEqual(second.duplicatesDropped, 1, "m2 was already held")
        XCTAssertEqual(store.cursor, "0003-m3", "the cursor tracks the newest held")
    }

    /// The deck reissues ids stably, so identity is the id. A cursor that moves
    /// under a stable id must not produce a second bubble.
    func testTheSameIdArrivingWithAFreshCursorStaysOneMessage() {
        var store = ConversationStore()
        store.ingest([makeMessage("m1", cursor: "0001-m1", text: "draft")])
        store.ingest([makeMessage("m1", cursor: "0009-m1", text: "draft, edited")])

        XCTAssertEqual(store.messages.count, 1)
        XCTAssertEqual(store.messages.first?.text, "draft, edited")
    }
}
