import XCTest
import Combine
@testable import DeckKit
@testable import DeckUI

/// **The sidebar must not rebuild itself over a frame that said nothing new.**
///
/// The app sits at 0.0% CPU untouched and pegs a core once it is used, and a
/// live `sample` taken while it was stuck put `_swift_getGenericMetadata` and
/// `swift::RefCounts` at the top with `ForEachState.item` under them. That is
/// the view *tree being reconstructed*, not a stable tree being laid out — a
/// list being handed what SwiftUI has to treat as new rows, over and over.
///
/// One roster tick costs a full pass over the payload: every thread bucketed,
/// every row allocated, every section sorted, and then a fresh `SidebarSnapshot`
/// pushed at the sidebar. That is correct **once**, when something moved. The
/// deck's stream repeats what it already said — `agent_state` frames restating
/// the state a desk is already in, unread frames restating a count, a `hello`
/// after a reconnect carrying the roster we are already holding — and every one
/// of those was rebuilding the whole sidebar.
///
/// ## Why this one can be honest headlessly
///
/// Seven probes in this repo have been blind to the runtime loop, because an
/// off-screen window has no display cycle (see `LayoutSettlesTests` and
/// `BaselineFallbackTests`, both of which say so on their face). This defect is
/// **not** about the display cycle. It is about counts: how many times a no-op
/// update provokes work. A count is the same number in a window, in a test, and
/// on his Mac.
///
/// The shape is copied from `LiveWhileWatchingTests`'
/// `testAPollThatFindsNothingNewPublishesNothingAtAll`, which asserts **0**
/// `objectWillChange` emissions across four polls. Same assertion, one layer
/// down: **0** rebuilds across five frames that found nothing new.
///
/// ## What this file does NOT claim
///
/// Green here does not mean the app is quiet. It means the roster model stopped
/// manufacturing work. `DeckStore.publishRoster` still assigns `roster` and
/// `agents` unconditionally, so the *publish* is not guarded by anything in
/// this file — that lives in a file this branch does not own.
final class SidebarChurnTests: XCTestCase {

    override func setUp() {
        super.setUp()
        Diagnostics.isEnabled = true
        Diagnostics.reset()
    }

    override func tearDown() {
        Diagnostics.isEnabled = false
        Diagnostics.reset()
        super.tearDown()
    }

    /// Rebuilds since the last time this was called.
    private func rebuildsSinceLastCheck() -> Int {
        Diagnostics.drain()["sidebar.rebuild"] ?? 0
    }

    private func roster(_ count: Int = 40) -> RosterPayload {
        var agents: [Agent] = []
        var threads: [ThreadSummary] = []
        for index in 0..<count {
            let name = "agent\(index)"
            var agent = makeAgent(name, section: index.isMultiple(of: 2) ? "Work" : "Personal")
            agent.state = .idle
            agent.unread = index.isMultiple(of: 3) ? 2 : 0
            agents.append(agent)
            threads.append(
                makeThread(
                    "direct:\(name)",
                    agent: name,
                    unread: agent.unread,
                    at: Double(index),
                    preview: ThreadPreview(text: "line \(index)"),
                    // The shape a direct thread really has on the wire. A
                    // one-participant thread is not one, and `rename` would
                    // rewrite it into one — a real change, dressed up as a
                    // no-op by the fixture rather than by the code.
                    participants: [DeckOwner.name, name]
                )
            )
        }
        return RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work", "Personal"])
    }

    private func loaded(_ payload: RosterPayload) async throws -> RosterModel {
        let client = ScriptedDeckClient()
        client.rosterPayload = payload
        let model = RosterModel(client: client)
        try await model.load()
        return model
    }

    private func rowIDs(_ snapshot: SidebarSnapshot) -> [String] {
        snapshot.sections.flatMap(\.rows).map(\.id)
    }

    // MARK: the good signal — assert the work still happens when it should

    /// **First, because a model that rebuilt nothing ever would sail through
    /// every assertion below while showing a stale sidebar.** This file has to
    /// be able to tell "quiet" from "broken", and the only thing that separates
    /// them is that a real change still costs exactly one rebuild and still
    /// reaches the snapshot.
    func testARealChangeStillRebuildsTheSidebarExactlyOnceAndIsVisibleInIt() async throws {
        let model = try await loaded(roster())
        _ = rebuildsSinceLastCheck()

        await model.applyAgentState(name: "agent7", state: .needsYou)

        XCTAssertEqual(
            rebuildsSinceLastCheck(), 1,
            "a desk that really did change state must still rebuild the sidebar once")
        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.id == "agent7" }
        XCTAssertEqual(
            row?.agent.state, .needsYou,
            "the change never reached the sidebar — every silence below would be "
            + "the silence of a model that has stopped working, not a quiet one")
    }

    /// The other half of the good signal: an unread count that really moved,
    /// and a roster that really is different, both still land.
    func testARealUnreadAndARealRosterStillLand() async throws {
        let model = try await loaded(roster())
        _ = rebuildsSinceLastCheck()

        await model.applyUnread(agent: "agent1", count: 9)
        XCTAssertEqual(rebuildsSinceLastCheck(), 1, "a real unread count must rebuild once")
        let afterUnread = await model.snapshot
        XCTAssertEqual(
            afterUnread.sections.flatMap(\.rows).first { $0.id == "agent1" }?.unreadCount, 9,
            "the badge never moved")

        var changed = roster()
        changed.agents[2].title = "Something else entirely"
        await model.replace(with: changed)
        XCTAssertEqual(rebuildsSinceLastCheck(), 1, "a roster that really differs must rebuild once")
        let afterReplace = await model.snapshot
        XCTAssertEqual(
            afterReplace.sections.flatMap(\.rows).first { $0.id == "agent2" }?.agent.title,
            "Something else entirely",
            "the new title never reached the row")
    }

    // MARK: the rule — five frames that found nothing new cost nothing

    /// `hello` after every reconnect, and any refetch, hands the model the
    /// roster it is already holding. Five of them rebuilt the sidebar five
    /// times.
    func testFiveRostersThatFindNothingNewRebuildTheSidebarNoTimes() async throws {
        let model = try await loaded(roster())
        let before = await model.snapshot
        _ = rebuildsSinceLastCheck()

        for _ in 0..<5 { await model.replace(with: roster()) }

        let count = rebuildsSinceLastCheck()
        XCTAssertEqual(
            count, 0,
            "\(count) full rebuilds of the sidebar for 5 rosters identical to the one "
            + "already held. Each one re-buckets every thread, re-allocates every row "
            + "and re-sorts every section, then pushes a new snapshot at a List.")
        let after = await model.snapshot
        XCTAssertEqual(rowIDs(after), rowIDs(before), "the rows are not even the same rows")
        XCTAssertEqual(after, before, "the snapshot changed over a roster that did not")
    }

    /// The frame the deck sends most: `agent_state`. A desk that has been
    /// WORKING for ten minutes is announced as WORKING on every tick.
    func testFiveAgentStateFramesRepeatingWhatWeKnowRebuildTheSidebarNoTimes() async throws {
        let model = try await loaded(roster())
        let before = await model.snapshot
        _ = rebuildsSinceLastCheck()

        for _ in 0..<5 { await model.applyAgentState(name: "agent4", state: .idle) }

        let count = rebuildsSinceLastCheck()
        XCTAssertEqual(
            count, 0,
            "\(count) rebuilds for 5 frames restating the state agent4 was already in")
        let after = await model.snapshot
        XCTAssertEqual(rowIDs(after), rowIDs(before))
        XCTAssertEqual(after, before)
    }

    /// `applyUnread` already looked guarded — it has a `touched` flag. The flag
    /// was set by the *assignment*, not by the assignment changing anything, so
    /// it never once suppressed a rebuild. A guard that cannot say no is not a
    /// guard.
    func testFiveUnreadFramesRepeatingTheSameCountRebuildTheSidebarNoTimes() async throws {
        let model = try await loaded(roster())
        let before = await model.snapshot
        _ = rebuildsSinceLastCheck()

        // agent3 is already on 2 — that is what the roster above gave it.
        for _ in 0..<5 { await model.applyUnread(agent: "agent3", count: 2) }

        let count = rebuildsSinceLastCheck()
        XCTAssertEqual(
            count, 0,
            "\(count) rebuilds for 5 unread frames restating a count already held")
        let after = await model.snapshot
        XCTAssertEqual(after, before)
    }

    /// Sweeping the class rather than the three instances above: **every**
    /// mutator on this model has to be silent when it finds nothing new. One
    /// that is not is a 1 Hz rebuild of the whole sidebar for as long as the
    /// app is open.
    func testEveryMutatorIsSilentWhenItFindsNothingNew() async throws {
        let payload = roster()
        let model = try await loaded(payload)
        let unchangedAgent = payload.agents[5]

        var noisy: [String] = []
        func check(_ what: String, _ act: () async -> Void) async {
            _ = rebuildsSinceLastCheck()
            await act()
            let count = rebuildsSinceLastCheck()
            if count != 0 { noisy.append("\(what) rebuilt \(count)x") }
        }

        await check("replace(with:) with the roster already held") {
            await model.replace(with: roster())
        }
        await check("applyAgentState with the state already held") {
            await model.applyAgentState(name: "agent5", state: .idle)
        }
        await check("applyUnread with the count already held") {
            await model.applyUnread(agent: "agent3", count: 2)
        }
        await check("applyAgentUpdate with the agent already held") {
            await model.applyAgentUpdate(unchangedAgent)
        }
        await check("insert of a desk already on the roster") {
            await model.insert(unchangedAgent)
        }
        await check("rename of a desk to what it is already called") {
            await model.rename(unchangedAgent.name, to: unchangedAgent)
        }
        await check("load() finding the same roster the deck served before") {
            try? await model.load()
        }

        XCTAssertEqual(
            noisy, [],
            "these updates found nothing new and rebuilt the whole sidebar anyway: "
            + noisy.joined(separator: "; "))
    }

    /// The identity claim on its own, because it is the one the profile named.
    /// Whatever the rebuild count, a row for a desk that did not change must
    /// come back as the same row — same id, same everything — or SwiftUI has to
    /// treat the list as new.
    func testARowSurvivesTwentyNoOpUpdatesUnchanged() async throws {
        let model = try await loaded(roster())
        let before = await model.snapshot

        // agent8 is idle and on zero unread in the roster above, so all three
        // of these restate what the model already holds.
        for _ in 0..<20 {
            await model.replace(with: roster())
            await model.applyAgentState(name: "agent8", state: .idle)
            await model.applyUnread(agent: "agent8", count: 0)
        }

        let after = await model.snapshot
        XCTAssertEqual(rowIDs(after), rowIDs(before), "the row identities moved")
        XCTAssertEqual(
            after, before,
            "twenty updates that changed nothing produced a sidebar that is not equal "
            + "to the one before them — a store that guards its publish on equality "
            + "would still publish, and the List would still be rebuilt")
    }
}

// MARK: - the other half: the publish above the model

/// **The model going quiet buys nothing if the store publishes anyway.**
///
/// The file above stops `RosterModel` manufacturing work, and says on its face
/// that `DeckStore`'s publish "lives in a file this branch does not own". This
/// is that layer: `publishRoster` assigns `roster` and `refreshAgents` assigns
/// `agents`, on **every frame the deck sends about any desk**. `@Published`
/// fires `objectWillChange` on every `set`, equal or not — so a `WORKING` frame
/// restating the state a desk is already in was two full rebuilds of every view
/// holding the store, several times a second, for nothing.
///
/// Driven through the live stream rather than through a method call, because
/// that is how these frames really arrive: `LiveDeckClient.push` yields on the
/// open `/v1/stream` exactly as the deck does.
///
/// The good signal is asserted **first**. A store that published nothing ever
/// would pass every silence below while showing a sidebar frozen on the state
/// the deck sent an hour ago.
@MainActor
final class RosterPublishChurnTests: XCTestCase {

    private func openDeck() async -> (LiveDeckClient, DeckStore) {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await waitUntil("the deck opens a conversation") { store.selectedAgentName == "chief" }
        // The deck runs **one** stream for everything, and a frame pushed
        // before the store has opened it goes nowhere. `.live` is the store
        // having seen the heartbeat this client yields on connect, so it is the
        // point after which a push is really delivered.
        await waitUntil("the stream is open") { store.connection == .live }
        return (client, store)
    }

    /// One frame that really moved something: it costs a bounded number of
    /// publishes, and the new state is on screen afterwards.
    func testAFrameThatReallyChangesSomethingStillPublishesAndIsVisible() async {
        let (client, store) = await openDeck()
        XCTAssertEqual(store.agents["chief"]?.state, .idle, "the fixture starts idle")

        var rebuilds = 0
        let watching = store.objectWillChange.sink { _ in rebuilds += 1 }
        client.push(.agentState(name: "chief", state: .working, blocked: .unspecified))
        await waitUntil("the desk reads as working") { store.agents["chief"]?.state == .working }
        watching.cancel()

        XCTAssertEqual(
            store.agents["chief"]?.state, .working,
            "the frame never reached the conversation's copy of the desk, so "
            + "every silence below would be the silence of a store that has "
            + "stopped listening rather than a quiet one")
        XCTAssertGreaterThan(rebuilds, 0, "nothing published at all, so nothing was redrawn")
        XCTAssertLessThanOrEqual(
            rebuilds, 2,
            "one state frame cost \(rebuilds) publishes. There are two facts to "
            + "move — the sidebar snapshot and the desk dictionary — and each is "
            + "worth one; anything above that is a value being assigned twice.")
    }

    /// The rule: ten frames restating what the deck has already said cost
    /// nothing at all above the store.
    func testTenFramesRepeatingWhatTheDeckAlreadySaidPublishNothingAtAll() async {
        let (client, store) = await openDeck()
        client.push(.agentState(name: "chief", state: .working, blocked: .unspecified))
        await waitUntil("the first frame lands") { store.agents["chief"]?.state == .working }
        await settle()

        var rebuilds = 0
        let watching = store.objectWillChange.sink { _ in rebuilds += 1 }
        for _ in 0..<10 {
            client.push(.agentState(name: "chief", state: .working, blocked: .unspecified))
        }
        await settle()
        watching.cancel()

        XCTAssertEqual(
            rebuilds, 0,
            "\(rebuilds) rebuilds of every view holding the store for ten frames "
            + "that said nothing new. A working desk sends these continuously, and "
            + "one of the views is a LazyVStack that re-measures every row on each "
            + "one of them.")
        XCTAssertEqual(store.agents["chief"]?.state, .working, "and it is still current")
    }

    /// Lets the store's own tasks run to quiescence, without sleeping on a
    /// guess about how many hops a frame takes to get there.
    private func settle() async {
        for _ in 0..<40 { await Task.yield() }
        try? await Task.sleep(nanoseconds: 80_000_000)
        for _ in 0..<40 { await Task.yield() }
    }

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
