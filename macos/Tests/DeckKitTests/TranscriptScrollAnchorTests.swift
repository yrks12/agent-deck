import Combine
import XCTest
@testable import DeckKit
@testable import DeckUI

/// **Suspect two, checked: does `proxy.scrollTo` fire per frame while a desk
/// streams a reply?**
///
/// `TranscriptList` scrolls the conversation with
///
/// ```swift
/// .onChange(of: entries.last?.id) { _, last in proxy.scrollTo(last, anchor: .bottom) }
/// ```
///
/// and the 98.8% CPU sample is `LazySubviewPlacements.placeSubviews` ->
/// `LazyStack.place` -> `ForEachList.applyNodes`, never converging. A
/// `scrollTo` into a `LazyVStack` re-places the list, so **if that anchor moved
/// on every publish rather than on every line, this one modifier would drive
/// the whole spin** — and a desk publishes constantly while it works, which is
/// exactly when the owner said *"when its working its stuck on my ui"*.
///
/// Nobody had checked. This file is the check, and the answer is written into
/// its assertions: the anchor is `Message.id` — the deck's own id, off the wire —
/// so it moves **once per line the desk actually said** and is untouched by the
/// hundreds of state frames, heartbeats and approval polls that arrive between
/// lines.
///
/// **So suspect two is ruled out**, and ruling it out is the point: it means the
/// remaining fault is in the container itself and not in what drives it. The
/// value of this file from here on is that it stays true — the day someone
/// derives a row id from anything that is not the deck's id (a UUID per build,
/// an index, a `hashValue` of the text), this goes red instead of the app going
/// to 100% CPU on his Mac a week later.
///
/// Driven through `LiveDeckClient`, whose stream **stays open**, because a
/// client that finishes its feed measures a conversation that has already
/// stopped — see `LiveWhileWatchingTests` for why that distinction cost a
/// defect.
@MainActor
final class TranscriptScrollAnchorTests: XCTestCase {

    /// What `onChange(of:)` in `TranscriptList` is watching, read the same way.
    private func anchor(_ store: DeckStore) -> String? {
        guard case .loaded(let screen) = store.thread else { return nil }
        return screen.entries.last?.id
    }

    // MARK: the good signal — one move per line

    /// A desk streaming a reply: five lines, and the anchor is those five
    /// lines, in order. Not four, not six, and never a value that is not a
    /// message the deck sent.
    func testTheScrollAnchorMovesOncePerLineTheDeskSaid() async {
        let client = LiveDeckClient.withChief(saying: "where are we?")
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript.count == 1 }

        var moves: [String] = []
        for index in 2...6 {
            client.push(.message(
                LiveDeckClient.reply("m\(index)", "line \(index)", at: TimeInterval(index)),
                readOnly: false))
            await waitUntil("line \(index) lands") { store.transcript.count == index }
            if let now = anchor(store), moves.last != now { moves.append(now) }
        }

        XCTAssertEqual(
            moves, ["said:m2", "said:m3", "said:m4", "said:m5", "said:m6"],
            "the transcript's scroll anchor moved to \(moves) for five lines. It "
            + "has to be one move per line: every move is a proxy.scrollTo into "
            + "the LazyVStack, and every scrollTo re-places the list — which is "
            + "LazySubviewPlacements.placeSubviews, the top of the 98.8% CPU "
            + "sample.")
    }

    // MARK: THE check — a working desk publishes constantly and must not move it

    /// **This is the one that answers the question.**
    ///
    /// A desk at work sends `agent_state` on every transition and the deck
    /// heartbeats underneath it. Every one of those is a publish, and every
    /// publish re-runs `ThreadView.body`. If the anchor were derived from
    /// anything but the deck's own message id, each of them would move it and
    /// fire a `scrollTo` — a re-placement of the whole lazy list, per frame,
    /// for as long as the desk is working.
    func testAWorkingDeskPublishesConstantlyAndTheAnchorDoesNotMove() async {
        let client = LiveDeckClient.withChief(saying: "where are we?")
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript.count == 1 }

        let settled = anchor(store)
        var publishes = 0
        let watching = store.objectWillChange.sink { _ in publishes += 1 }

        // A desk going round the loop it goes round all day.
        let states: [AgentState] = [.working, .needsYou, .working, .done, .idle]
        for round in 0..<8 {
            client.push(.agentState(
                name: "chief", state: states[round % states.count], blocked: .unspecified))
            client.push(.heartbeat)
        }
        await waitUntil("the state frames are through") { publishes >= 8 }
        watching.cancel()

        // GOOD SIGNAL FIRST: something really did happen. An anchor that never
        // moves because nothing ever arrived proves nothing at all.
        XCTAssertGreaterThanOrEqual(
            publishes, 8,
            "only \(publishes) publishes came out of 8 state frames and 8 "
            + "heartbeats, so the quiet below is the quiet of a dead stream")
        XCTAssertEqual(
            anchor(store), settled,
            "a desk changing state \(publishes) times moved the transcript's "
            + "scroll anchor from \(settled ?? "nothing") to "
            + "\(anchor(store) ?? "nothing"). Every move fires proxy.scrollTo "
            + "into the LazyVStack and re-places every row — which is the spin "
            + "in the sample, driven by a desk simply doing its job.")
        XCTAssertEqual(
            store.transcript.count, 1,
            "state frames added lines to the conversation")
    }

    // MARK: why it is stable — the id is the deck's, not this app's

    /// The property the two tests above rest on, stated where it can be read.
    /// A row's identity is the id the deck minted for that message, so two
    /// builds of the same conversation carry the same identities and `ForEach`
    /// has nothing to rebuild.
    func testARowIsIdentifiedByTheDecksOwnMessageIdAndNothingElse() {
        let message = Message(
            id: "01J8ZQ", cursor: "0007", threadID: "direct:chief", author: "chief",
            role: .agent, sentAt: Date(timeIntervalSince1970: 1_756_000_000),
            text: "the same words twice")

        let once = ThreadEntry.said(TranscriptRow(message: message, attribution: .ordinary))
        let twice = ThreadEntry.said(TranscriptRow(message: message, attribution: .ordinary))

        XCTAssertEqual(
            once.id, "said:01J8ZQ",
            "a row is identified as '\(once.id)' rather than by the deck's own "
            + "message id. Anything else — an index, a UUID, a hashValue of the "
            + "text — changes between two builds of the same conversation, and "
            + "then ForEach rebuilds every row and the scroll anchor fires on "
            + "every publish.")
        XCTAssertEqual(
            once.id, twice.id,
            "the same message built twice produced two identities")
    }

    // MARK: waiting without sleeping on a guess

    private func waitUntil(
        _ what: String, within timeout: TimeInterval = 3,
        file: StaticString = #filePath, line: UInt = #line,
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
