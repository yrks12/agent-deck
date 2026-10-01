import XCTest
@testable import DeckKit

/// The gap this closes: `GET /v1/stream`'s `agent_state` frame now always
/// carries a `blocked` key — object or `null` — and fires whenever `state`
/// **or** `blocked` changes (`client-api.md` §8). Before this, the frame's
/// wire shape had no `blocked` field at all, so a desk that froze on a
/// permission dialog *while a client was already connected* changed `state`
/// on the wire with nothing attached — the badge only appeared on the next
/// `GET /v1/agents` or a reconnect, which is exactly the moment it exists to
/// cover.
///
/// This file is the decode step only: turning one wire frame into one
/// `DeckEvent.agentState` with the right tri-state `blocked`. `absent` (no
/// key at all — an older deck, or a frame that never carried one) is a
/// different fact from `cleared` (`blocked: null` — the deck telling you
/// "not blocked") and both differ from `value` (a live reason). Collapsing
/// `absent` and `cleared` to the same "nil" — as a plain `Blocked?` would —
/// is the bug this type exists to make impossible.
final class AgentStateBlockedFrameTests: XCTestCase {

    private func frame(_ json: String) -> SSEEvent {
        SSEEvent(name: nil, data: json, id: nil)
    }

    // MARK: a desk that freezes while connected

    func testAnAgentStateFrameCarryingABlockedObjectDecodesToItsValue() throws {
        let sse = frame("""
        {"type": "agent_state", "ts": 1788222780.1, "name": "chief", "state": "NEEDS_YOU",
         "blocked": {"what": "this desk is stopped on a permission dialog",
                     "reason": "dialog_unrelayed", "detail": "needs your permission to use Bash",
                     "at": 1788222779.6}}
        """)

        guard case .agentState(let name, let state, let blocked) = try XCTUnwrap(DeckEvent(sse: sse))
        else { return XCTFail("expected an agentState event") }

        XCTAssertEqual(name, "chief")
        XCTAssertEqual(state, .needsYou)
        guard case .value(let info) = blocked else {
            return XCTFail("a present blocked object must decode to .value, not .unspecified or .cleared")
        }
        XCTAssertEqual(info.reason, "dialog_unrelayed")
    }

    // MARK: a desk that recovers — the retraction

    func testAnAgentStateFrameCarryingExplicitNullBlockedDecodesToCleared() throws {
        let sse = frame("""
        {"type": "agent_state", "ts": 1788222784.7, "name": "chief", "state": "WORKING",
         "blocked": null}
        """)

        guard case .agentState(_, _, let blocked) = try XCTUnwrap(DeckEvent(sse: sse))
        else { return XCTFail("expected an agentState event") }

        XCTAssertEqual(blocked, .cleared,
                       "explicit null is the deck telling the client the badge must clear now")
    }

    // MARK: an older deck, or any frame that never sent the key

    func testAnAgentStateFrameWithNoBlockedKeyAtAllDecodesToUnspecifiedNotCleared() throws {
        let sse = frame(#"{"type":"agent_state","ts":1.0,"name":"chief","state":"OFFLINE"}"#)

        guard case .agentState(_, _, let blocked) = try XCTUnwrap(DeckEvent(sse: sse))
        else { return XCTFail("expected an agentState event") }

        XCTAssertEqual(blocked, .unspecified,
                       "an absent key is 'this frame does not tell me' — it must not read as a retraction")
        XCTAssertNotEqual(blocked, .cleared,
                          "absent and null are different facts on the wire and must decode differently")
    }

    // MARK: blocked moving with state sitting still

    func testStateUnchangedButBlockedMovingStillDecodesTheNewBlockedValue() throws {
        // A desk can flip between an answerable and an unanswerable dialog with
        // `state` sitting at NEEDS_YOU throughout (§8) — the frame still carries
        // the new `blocked`, and decode must not special-case "state didn't move".
        let sse = frame("""
        {"type": "agent_state", "ts": 2.0, "name": "chief", "state": "NEEDS_YOU",
         "blocked": {"what": "y", "reason": "dialog_unrelayed", "detail": "d"}}
        """)

        guard case .agentState(_, let state, let blocked) = try XCTUnwrap(DeckEvent(sse: sse))
        else { return XCTFail("expected an agentState event") }

        XCTAssertEqual(state, .needsYou)
        guard case .value(let info) = blocked else {
            return XCTFail("blocked must still decode even though state did not change")
        }
        XCTAssertEqual(info.reason, "dialog_unrelayed")
    }

    // MARK: a reason slug this build does not know yet

    func testAnAgentStateFrameWithAnUnknownReasonSlugStillDecodesAsAValue() throws {
        let sse = frame("""
        {"type": "agent_state", "ts": 3.0, "name": "chief", "state": "WORKING",
         "blocked": {"what": "x", "reason": "some_future_reason", "detail": "raw text"}}
        """)

        guard case .agentState(_, _, let blocked) = try XCTUnwrap(DeckEvent(sse: sse))
        else { return XCTFail("expected an agentState event") }

        guard case .value(let info) = blocked else {
            return XCTFail("an unrecognised reason is still a present value, not absent")
        }
        // `Blocked.sentence` (BlockedTests.swift) is what falls back for an
        // unrecognised slug; decode itself must not drop or reject it.
        XCTAssertEqual(info.sentence, "raw text")
    }
}
