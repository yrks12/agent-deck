import XCTest
@testable import DeckKit

/// Before this: an agent hired through the interview door that stalled on
/// Claude Code's "trust this folder?" dialog read `OFFLINE`, identical to a
/// desk nobody had ever started. This is the detector for the fix — the deck
/// now says why, and every slug it can send must turn into a sentence a person
/// reading it once can act on, never a raw slug and never nothing.
///
/// 2026-09-01: the contract changed again. `blocked` used to be non-nil only
/// while `state == "OFFLINE"`. It is now also carried while the desk is
/// **seated and frozen** on an unrelayed permission dialog (`dialog_unrelayed`,
/// §3.1). The tests below sweep every state, not just `OFFLINE`, so a gate
/// added anywhere in the decode path fails loudly here.
final class BlockedTests: XCTestCase {

    private func agentJSON(blocked: String?, state: String = "OFFLINE") -> Data {
        let base = """
        {"name": "hemingway", "label": "Researcher", "state": "\(state)"
        """
        let json = blocked == nil
            ? base + "}"
            : base + ", \"blocked\": \(blocked!)}"
        return Data(json.utf8)
    }

    private func decode(_ blocked: String?, state: String = "OFFLINE") throws -> Agent {
        try DeckCoding.decoder.decode(Agent.self, from: agentJSON(blocked: blocked, state: state))
    }

    // MARK: the field itself

    func testAnAgentWithNoBlockedKeyAtAllDecodesToNilNotAnEmptyValue() throws {
        let agent = try decode(nil)

        XCTAssertNil(agent.blocked, "a deck that has not shipped this field yet must not fabricate one")
    }

    func testAnExplicitNullBlockedDecodesToNilTheSameAsAnAbsentKey() throws {
        let agent = try decode("null")

        XCTAssertNil(agent.blocked)
    }

    func testABlockedAgentCarriesTheReasonAndDetailVerbatim() throws {
        let agent = try decode("""
        {"what": "workspace trust was not pre-accepted", "reason": "lock_busy",
         "detail": "another writer holds the lock", "at": 1788222702.0}
        """)

        XCTAssertEqual(agent.blocked?.reason, "lock_busy")
        XCTAssertEqual(agent.blocked?.detail, "another writer holds the lock")
        XCTAssertEqual(agent.blocked?.at, Date(timeIntervalSince1970: 1788222702.0))
    }

    // MARK: every slug the deck can send — swept, not spot-checked

    /// The sentence for a recognised reason is fixed English, never the slug
    /// and never the deck's raw `detail` — so it reads the same whatever the
    /// underlying exception string on the server happened to say.
    func testEveryKnownReasonMapsToItsOwnActionableSentence() throws {
        var seen: Set<String> = []
        for reason in BlockedReason.allCases {
            let agent = try decode("""
            {"what": "x", "reason": "\(reason.rawValue)", "detail": "some raw exception text"}
            """)
            let sentence = try XCTUnwrap(agent.blocked?.sentence)

            XCTAssertFalse(sentence.isEmpty, "\(reason.rawValue) produced no sentence")
            XCTAssertNotEqual(sentence, reason.rawValue,
                               "a slug shown as itself is not what an owner reads and acts on")
            XCTAssertNotEqual(sentence, "some raw exception text",
                               "a known reason must not fall through to the raw detail")
            XCTAssertTrue(seen.insert(sentence).inserted,
                          "\(reason.rawValue) reused another reason's sentence — each needs its own")
        }
        XCTAssertEqual(BlockedReason.allCases.count, 6, "sweep every slug the contract names; update this test if the deck adds one")
    }

    // MARK: dialog_unrelayed — a desk that is seated, not offline, and stuck anyway

    /// The one slug that never renders the slug: it is written the way its
    /// siblings are — what he does about it, not what went wrong — and it
    /// must not read like the five `OFFLINE`-only reasons, which all talk
    /// about a trust dialog on a desk that never started.
    func testDialogUnrelayedHasItsOwnActionableSentence() throws {
        let sentence = BlockedReason.dialogUnrelayed.sentence

        XCTAssertFalse(sentence.isEmpty)
        XCTAssertFalse(sentence.contains("dialog_unrelayed"), "never render the slug")
        // It is not one of the OFFLINE start-up gates — it must not send the
        // reader to "accept the trust prompt", which is the wrong dialog.
        XCTAssertFalse(sentence.lowercased().contains("trust"),
                        "this desk already started; the trust prompt is a different failure")
        // The one thing the app cannot do is answer it for him.
        XCTAssertTrue(sentence.lowercased().contains("window"),
                       "the actionable instruction is: go to that session's window")
    }

    // MARK: `blocked` is carried on a seated desk too — the contract change itself

    /// This is the regression the contract note calls out by name: a client
    /// that assumed `blocked` only travels with `state == "OFFLINE"` drops
    /// `dialog_unrelayed` — the one case that most needs to reach the badge,
    /// because the desk looks like it is working. Nothing in the decode path
    /// may key off `state` to decide whether to carry `blocked`.
    func testBlockedSurvivesDecodeForEverySeatedStateNotJustOffline() throws {
        let states = ["OFFLINE", "WORKING", "NEEDS_YOU", "DONE", "SHELL", "IDLE", "DEAD"]

        for state in states {
            let agent = try decode("""
            {"what": "its window has a permission dialog the deck could not relay",
             "reason": "dialog_unrelayed",
             "detail": "needs your permission to use Bash", "at": 1788222702.0}
            """, state: state)

            XCTAssertEqual(agent.state.rawValue, state)
            XCTAssertNotNil(agent.blocked, "blocked must decode for state \(state), not just OFFLINE")
            XCTAssertEqual(agent.blocked?.sentence, BlockedReason.dialogUnrelayed.sentence,
                            "the sentence for state \(state) must be the one actionable sentence for this slug")
        }
    }

    // MARK: the reason this build does not recognise

    func testAnUnknownReasonWithDetailFallsBackToTheDecksOwnDetail() throws {
        let agent = try decode("""
        {"what": "x", "reason": "some_future_reason", "detail": "the deck's own prose about it"}
        """)

        XCTAssertEqual(agent.blocked?.sentence, "the deck's own prose about it",
                       "never the raw slug — fall back to detail")
    }

    func testAnUnknownReasonWithNoDetailFallsBackToThePlainSentence() throws {
        let agent = try decode("""
        {"what": "x", "reason": "some_future_reason", "detail": ""}
        """)

        XCTAssertEqual(agent.blocked?.sentence, Blocked.fallbackSentence)
        XCTAssertFalse(agent.blocked!.sentence.contains("some_future_reason"),
                        "still never the slug, even as a last resort")
    }

    func testAnUnknownReasonWithDetailMissingEntirelyAlsoFallsBackToThePlainSentence() throws {
        let agent = try decode("""
        {"what": "x", "reason": "some_future_reason"}
        """)

        XCTAssertEqual(agent.blocked?.sentence, Blocked.fallbackSentence)
    }

    /// The fallback is the protection against the *next* contract change, so
    /// it has to hold on a seated desk, not only on an `OFFLINE` one — this is
    /// the case that shipped before `dialog_unrelayed` existed, carrying
    /// Claude Code's own notification text (e.g. "needs your permission to
    /// use Bash") as `detail`.
    func testAnUnknownReasonFallsBackToDetailOnASeatedDeskToo() throws {
        let agent = try decode("""
        {"what": "x", "reason": "some_future_reason",
         "detail": "needs your permission to use Bash"}
        """, state: "WORKING")

        XCTAssertEqual(agent.state, .working)
        XCTAssertNotNil(agent.blocked, "a seated desk must still carry an unrecognised blocked reason")
        XCTAssertEqual(agent.blocked?.sentence, "needs your permission to use Bash",
                       "the fallback that shipped before dialog_unrelayed must still work on a live desk")
    }
}
