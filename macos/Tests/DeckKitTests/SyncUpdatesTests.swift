import XCTest
@testable import DeckKit

/// The view layer never polls the sync actor; it consumes a stream of updates.
/// What the tests pin is that the stream tells the same story the actor holds —
/// including the drop — and that it ends when the sync ends, so a thread view
/// that closes does not leak a task.
final class SyncUpdatesTests: XCTestCase {

    private let instantBackoff: @Sendable (Int) async throws -> Void = { _ in }

    private func clientAcrossADrop() -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        client.pages = [
            MessagePage(threadID: "t1", messages: (1...3).map { makeMessage("m\($0)", cursor: "000\($0)-m\($0)") },
                        isReadOnly: false, participants: ["Hemingway"]),
            MessagePage(threadID: "t1",
                        messages: [makeMessage("m4", cursor: "0004-m4"), makeMessage("m5", cursor: "0005-m5")],
                        isReadOnly: false, participants: ["Hemingway"]),
        ]
        client.feeds = [
            .emitThenDrop([.message(makeMessage("m4", cursor: "0004-m4"), readOnly: false)]),
            .emitThenFinish([.message(makeMessage("m6", cursor: "0006-m6"), readOnly: false)]),
        ]
        return client
    }

    func testTheFinalUpdateCarriesTheWholeTranscriptExactlyOnce() async throws {
        let sync = ConversationSync(
            client: clientAcrossADrop(), threadID: "t1", backoff: instantBackoff
        )
        let stream = await sync.updates()
        let running = Task { try await sync.run() }

        var seen: [SyncUpdate] = []
        for await update in stream { seen.append(update) }
        try await running.value

        let last = try XCTUnwrap(seen.last)
        XCTAssertEqual(last.messages.map(\.id), ["m1", "m2", "m3", "m4", "m5", "m6"])
        XCTAssertEqual(last.connection, .live)
    }

    func testTheDropIsVisibleToTheViewLayerAsItHappens() async throws {
        let sync = ConversationSync(
            client: clientAcrossADrop(), threadID: "t1", backoff: instantBackoff
        )
        let stream = await sync.updates()
        let running = Task { try await sync.run() }

        var states: [ConnectionState] = []
        for await update in stream { states.append(update.connection) }
        try await running.value

        XCTAssertTrue(
            states.contains(.reconnecting(attempt: 1)),
            "the indicator has to be able to say Reconnecting, got \(states)"
        )
        XCTAssertTrue(states.contains(.live), "and it has to come back")
    }

    func testTheUpdateCarriesTheReadOnlyFlagFromThePage() async throws {
        let client = ScriptedDeckClient()
        client.pages = [
            MessagePage(threadID: "t9", messages: [], isReadOnly: true,
                        participants: ["Chief", "Hemingway"])
        ]
        client.feeds = [.emitThenFinish([])]
        let sync = ConversationSync(client: client, threadID: "t9", backoff: instantBackoff)
        let stream = await sync.updates()
        let running = Task { try await sync.run() }

        var seen: [SyncUpdate] = []
        for await update in stream { seen.append(update) }
        try await running.value

        let last = try XCTUnwrap(seen.last)
        XCTAssertTrue(last.isReadOnly)
        XCTAssertEqual(last.participants, ["Chief", "Hemingway"])
    }

    func testTheStreamEndsWhenTheSyncEnds() async throws {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: "t1", messages: [], isReadOnly: false, participants: [])]
        client.feeds = [.emitThenFinish([])]
        let sync = ConversationSync(client: client, threadID: "t1", backoff: instantBackoff)
        let stream = await sync.updates()

        try await sync.run()

        var count = 0
        for await _ in stream { count += 1 }
        XCTAssertGreaterThan(count, 0, "the buffered updates are still delivered, then the stream closes")
    }
}
