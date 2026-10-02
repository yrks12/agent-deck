import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The tray is deck-wide, so it has to keep watching when no conversation is
/// open.**
///
/// `AttentionTrayView`'s own comment says it out loud: *"the tray's whole
/// reason for existing is the ask raised somewhere he is not looking"*. The
/// strongest case of "somewhere he is not looking" is **nothing** open at all —
/// he closed the chat, or clicked back out of it, and the window is sitting on
/// the empty pane.
///
/// That is precisely where it stopped. `DeckStore.open(threadID:)` cancelled
/// the poll and then returned early on `nil`, and `closeThread()` calls
/// `open(nil)`. `loadApprovals()` has no other caller, so from that moment the
/// tray was frozen: resolved items never left it and new asks never arrived.
/// It only ever looked alive because `loadRoster()` auto-selects the first row,
/// which restarted the poll on launch.
///
/// Both directions are asserted, and the **thread-open case first** — a
/// detector that only measured the closed case could pass by measuring nothing
/// at all (a client that never serves an ask satisfies it).
///
/// Every wait here is bounded. A test in this suite hung for over an hour
/// holding the SwiftPM lock; nothing below pumps open-endedly.
@MainActor
final class TheTrayKeepsWatchingTests: XCTestCase {

    /// A deck whose stream stays open, one desk, one line already said.
    private func deck() -> (LiveDeckClient, DeckStore) {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        return (client, DeckStore(client: client, approvalPollInterval: 0.02))
    }

    // MARK: 1 — the control: it works while he is reading a conversation

    /// If this fails, the case below is measuring nothing.
    func testAnAskRaisedWhileAConversationIsOpenReachesTheTray() async throws {
        let (client, store) = deck()
        await store.loadRoster()
        await waitUntil("the conversation opens") { store.selectedThreadID == "direct:chief" }
        XCTAssertTrue(store.attention.isEmpty, "nothing is waiting yet")

        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")

        await waitUntil("the ask reaches the tray") { !store.attention.isEmpty }
        XCTAssertEqual(store.attention.map(\.askID), ["z47x8"])
    }

    // MARK: 2 — THE DETECTOR: he closed the conversation and the deck kept going

    /// He closes the chat. `chief` then stops on a tool call. Nothing on this
    /// Mac tells him — the tray is the only surface that was ever going to, and
    /// it stopped asking the moment the thread went.
    func testAnAskRaisedAfterHeClosesTheConversationStillReachesTheTray() async throws {
        let (client, store) = deck()
        await store.loadRoster()
        await waitUntil("the poll starts") { client.approvalFetches > 0 }

        store.closeThread()
        XCTAssertNil(store.selectedThreadID, "the case needs the conversation genuinely closed")
        XCTAssertNil(store.selectedAgentName)

        // The desk hits something its rules do not cover, and stops — with him
        // looking at an empty pane.
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")

        await waitUntil("the ask reaches the tray with nothing open") {
            !store.attention.isEmpty
        }
        XCTAssertEqual(
            store.attention.map(\.askID), ["z47x8"],
            "chief is stopped waiting for him and he has no conversation open, "
            + "which is the exact case the tray exists for. The poll that feeds "
            + "it was cancelled by closeThread() and never restarted.")
    }

    /// The other direction, and the one that makes a stale tray dangerous: an
    /// ask settled somewhere else has to leave the strip. A tray that keeps
    /// offering to grant something nobody is asking for any more is worse than
    /// an empty one.
    func testAnAskSettledElsewhereLeavesTheTrayWithNoConversationOpen() async throws {
        let (client, store) = deck()
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")
        await store.loadRoster()
        await waitUntil("the ask is on screen") { !store.attention.isEmpty }

        store.closeThread()
        client.approvalsPage = ApprovalsPage(approvals: [])

        await waitUntil("the settled ask leaves the tray") { store.attention.isEmpty }
        XCTAssertEqual(
            store.attention.map(\.askID), [],
            "the question was answered in the agent's own terminal and the tray "
            + "still offered to answer it")
    }

    // MARK: 3 — it is the deck it follows, not the selection

    /// A deck with **no desks at all** still has to be watched: `/v1/approvals`
    /// and `/v1/handoffs` are deck-wide routes and an ask can carry a raw
    /// session id no roster row answers to. An empty roster selects nothing, so
    /// under the old rule no thread was ever opened and the poll never started.
    func testTheTrayIsWatchedEvenWhenTheRosterOpensNothing() async throws {
        let client = LiveDeckClient()
        client.rosterPayload = RosterPayload(agents: [], threads: [], sectionOrder: [])
        let store = DeckStore(client: client, approvalPollInterval: 0.02)

        await store.loadRoster()
        XCTAssertNil(store.selectedThreadID, "an empty roster opens nothing")

        client.approvalsPage = try LiveDeckClient.oneApproval(from: "sid-9f2c")

        await waitUntil("the ask reaches the tray on an empty deck") {
            !store.attention.isEmpty
        }
        XCTAssertEqual(store.attention.map(\.askID), ["z47x8"])
    }

    // MARK: 4 — and it still stops when the window goes

    /// The reason the poll was tied to the thread in the first place was a real
    /// one: a request per interval, for ever, on a window he leaves open all
    /// day. That bound is kept — it is the **window** that ends it, not the
    /// selection.
    func testThePollStopsWhenTheWindowGoes() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        var store: DeckStore? = DeckStore(client: client, approvalPollInterval: 0.02)

        await store?.loadRoster()
        await waitUntil("the poll starts") { client.approvalFetches > 0 }

        store = nil
        // One tick may already be in flight when the last reference goes.
        try? await Task.sleep(nanoseconds: 100_000_000)
        let settled = client.approvalFetches
        try? await Task.sleep(nanoseconds: 300_000_000)

        XCTAssertEqual(
            client.approvalFetches, settled,
            "the window closed and something is still waking up to ask this "
            + "deck for approvals: \(settled) → \(client.approvalFetches)")
    }

    // MARK: waiting without sleeping on a guess

    private func waitUntil(
        _ what: String,
        within timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for: \(what)", file: file, line: line)
    }
}
