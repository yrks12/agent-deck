import XCTest
@testable import DeckKit

/// The application step: `RosterModel.applyAgentState` folding a live
/// `agent_state` frame's `blocked` into the roster without a refetch. This is
/// the half of the fix that is easy to ship silently wrong in the other
/// direction — a badge that only ever appears and never clears is the same
/// lie as one that never appears at all, and it looks fine at the moment you
/// write it because nothing crashes.
final class RosterModelBlockedTests: XCTestCase {

    private func model(with agent: Agent) async -> RosterModel {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [agent],
            threads: [makeThread("t1", agent: agent.name)],
            sectionOrder: ["Work"]
        )
        let model = RosterModel(client: client)
        try? await model.load()
        return model
    }

    private func blockedRow(_ model: RosterModel, name: String) async -> Bool {
        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == name }
        return row?.agent.blocked != nil
    }

    // MARK: 1. a desk that freezes while connected shows the badge immediately

    func testAFreezeFrameAttachesBlockedToAnAlreadyConnectedDeskWithNoRefetch() async {
        let chief = makeAgent("chief")
        let model = await model(with: chief)

        var connected = await blockedRow(model, name: "chief")
        XCTAssertFalse(connected, "starts unblocked")

        let info = Blocked(what: "stuck", reason: "dialog_unrelayed", detail: "needs your permission to use Bash")
        await model.applyAgentState(name: "chief", state: .needsYou, blocked: .value(info))

        connected = await blockedRow(model, name: "chief")
        XCTAssertTrue(connected, "the badge must attach off the live frame alone, before any reconnect or GET /v1/agents")

        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == "chief" }
        XCTAssertEqual(row?.agent.blocked?.sentence, BlockedReason.dialogUnrelayed.sentence,
                       "the right sentence, not just a non-nil value")
    }

    // MARK: 2. a desk that recovers clears it immediately

    func testARetractionFrameClearsAnExistingBlockedImmediately() async {
        let chief = makeAgent("chief")
        var withBadge = chief
        withBadge.blocked = Blocked(what: "stuck", reason: "dialog_unrelayed", detail: "d")
        let model = await model(with: withBadge)

        var stillThere = await blockedRow(model, name: "chief")
        XCTAssertTrue(stillThere, "seeded with a badge")

        await model.applyAgentState(name: "chief", state: .working, blocked: .cleared)

        stillThere = await blockedRow(model, name: "chief")
        XCTAssertFalse(stillThere, "an explicit null retraction must clear the badge, not merely leave it stale")
    }

    // MARK: 3. blocked moving with state sitting still (NEEDS_YOU throughout)

    func testBlockedCanChangeValueWithoutTheStateFieldMoving() async {
        let chief = makeAgent("chief")
        var seated = chief
        seated.state = .needsYou
        seated.blocked = Blocked(what: "a", reason: "dialog_unrelayed", detail: "first dialog")
        let model = await model(with: seated)

        let second = Blocked(what: "b", reason: "dialog_unrelayed", detail: "second dialog")
        await model.applyAgentState(name: "chief", state: .needsYou, blocked: .value(second))

        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == "chief" }
        XCTAssertEqual(row?.agent.state, .needsYou)
        XCTAssertEqual(row?.agent.blocked?.detail, "second dialog",
                       "a frame with the same state must still update blocked, not be treated as a no-op")
    }

    // MARK: 4. the absent key — must not read as a retraction

    func testAnAgentStateFrameWithNoBlockedKeyLeavesTheExistingBlockedValueAlone() async {
        let chief = makeAgent("chief")
        var withBadge = chief
        withBadge.blocked = Blocked(what: "stuck", reason: "dialog_unrelayed", detail: "d")
        let model = await model(with: withBadge)

        // .unspecified is what an absent `blocked` key decodes to (see
        // AgentStateBlockedFrameTests) — simulating an older deck, or any
        // frame class that has not been taught to carry this field.
        await model.applyAgentState(name: "chief", state: .working, blocked: .unspecified)

        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == "chief" }
        XCTAssertEqual(row?.agent.state, .working, "state still applies")
        XCTAssertNotNil(row?.agent.blocked, "an absent key must not clear an existing blocked — 'not told' is not 'told: clear'")
        XCTAssertEqual(row?.agent.blocked?.reason, "dialog_unrelayed")
    }

    /// The default argument itself is the contract: a caller that does not
    /// pass `blocked` at all (every existing call site before this change)
    /// must behave exactly like `.unspecified`, never like `.cleared`.
    func testOmittingTheBlockedArgumentEntirelyAlsoLeavesTheExistingValueAlone() async {
        let chief = makeAgent("chief")
        var withBadge = chief
        withBadge.blocked = Blocked(what: "stuck", reason: "lock_busy", detail: "d")
        let model = await model(with: withBadge)

        await model.applyAgentState(name: "chief", state: .idle)

        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == "chief" }
        XCTAssertNotNil(row?.agent.blocked, "the default must be 'unspecified', not 'cleared'")
    }

    // MARK: 5. an unknown reason slug still applies as a value

    func testAnUnrecognisedReasonSlugStillReplacesTheExistingBlockedValue() async {
        let chief = makeAgent("chief")
        let model = await model(with: chief)

        let future = Blocked(what: "x", reason: "some_future_reason", detail: "raw text")
        await model.applyAgentState(name: "chief", state: .needsYou, blocked: .value(future))

        let snapshot = await model.snapshot
        let row = snapshot.sections.flatMap(\.rows).first { $0.agent.name == "chief" }
        XCTAssertEqual(row?.agent.blocked?.sentence, "raw text",
                       "an unrecognised slug must still reach the row and fall back to detail")
    }
}
