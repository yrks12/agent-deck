import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"Idle" on its own is misleading, and `client-api.md` §3.0 is a whole
/// section saying so.**
///
/// `state` is the *session's* disposition. It is computed from what the process
/// is doing and knows nothing about the conversation, so `IDLE` covers three
/// situations a person would never call the same thing:
///
/// | `state` | `last_activity_by` | what it actually is |
/// |---|---|---|
/// | `IDLE` | `"agent"` | **It answered you and is waiting.** Say that, not "nothing is running". |
/// | `IDLE` | `"owner"` | You said something and it has not come back yet. |
/// | `IDLE` | `""` | Genuinely idle since it started — nobody has ever spoken to it. |
///
/// The deck serves the field on every `/v1/agents` row. This app had **no
/// coding key for it** — `grep -rn "last_activity_by\|lastActivityBy" Sources/`
/// returned nothing — and printed one sentence for all three.
///
/// That is the difference between *finished, go and read it*, *still working on
/// what you asked*, and *never started*. Three of his six desks read `IDLE`.
/// Managing by exception is the whole product, and a word that means three
/// things is not an exception you can manage by.
///
/// §3.0 also says the field is **not on the `agent_state` SSE frame**: "A
/// `state` change that should also change this line needs the row — re-read
/// `GET /v1/agents`, or take it from the newest message in the thread you
/// already have open." Both halves are pinned below, because the failure there
/// is the worst one available: a desk that has just answered him being
/// described as still working on his question.
final class IdleIsThreeDifferentThingsTests: XCTestCase {

    // MARK: 1 — it is decoded at all

    /// The row §3 serves, verbatim, with the field on it.
    private func row(_ lastActivityBy: String?) throws -> Agent {
        let field = lastActivityBy.map { "\"last_activity_by\": \"\($0)\"," } ?? ""
        return try DeckCoding.decoder.decode(Agent.self, from: Data("""
        {"name": "hemingway", "label": "Designer", "section": "Work",
         "avatar": null, "state": "IDLE", "unread": 0,
         "last_activity_at": 1756000050.0, \(field)
         "preview": "second draft: out. last line: cut.",
         "thread_id": "direct:hemingway", "pinned": false, "notifications": true,
         "desk": true, "project": "", "session_id": null,
         "boss": "chief", "reports": []}
        """.utf8))
    }

    func testTheFieldTheDeckServesOnEveryRowIsRead() throws {
        XCTAssertEqual(
            try row("agent").lastSpeaker, .agent,
            "the deck says this desk spoke last and the app decodes nothing "
            + "from the field, so it cannot tell an answer from a silence")
        XCTAssertEqual(try row("owner").lastSpeaker, .owner)
        XCTAssertEqual(
            try row("").lastSpeaker, .nobody,
            "\"\" is the deck's word for nobody has ever spoken here — a real "
            + "answer, not a missing one")
        XCTAssertEqual(
            try row(nil).lastSpeaker, .unknown,
            "an older deck that never did the join has told this client "
            + "nothing, and that is not the same fact as \"\"")
    }

    // MARK: 2 — THE DETECTOR: the three read differently on the conversation

    private func idle(_ speaker: LastSpeaker) throws -> DeskStatus {
        var desk = makeAgent("hemingway")
        desk.state = .idle
        desk.lastSpeaker = speaker
        return try XCTUnwrap(
            DeskStatus.make(agent: desk, connection: .live, approvals: [], isSending: false))
    }

    /// The case §3.0 calls out by name: **say that it answered you**, not that
    /// nothing is running.
    func testADeskThatAnsweredAndIsWaitingSaysSoRatherThanReadingAsNothingRunning() throws {
        let answered = try idle(.agent)

        XCTAssertTrue(
            answered.spokenLabel.localizedCaseInsensitiveContains("answered"),
            "the desk came back to him and the pane says: \(answered.spokenLabel)")
        XCTAssertFalse(
            answered.spokenLabel.localizedCaseInsensitiveContains("nothing is running"),
            "§3.0: \"Say that, not 'nothing is running'\" — \(answered.spokenLabel)")
    }

    func testADeskHeIsStillWaitingOnDoesNotReadAsOneThatCameBack() throws {
        let waitingOnIt = try idle(.owner)

        XCTAssertNotEqual(
            waitingOnIt.spokenLabel, try idle(.agent).spokenLabel,
            "he spoke last and it has not come back — drawn identically to a "
            + "desk that has already answered him")
        XCTAssertTrue(
            waitingOnIt.spokenLabel.localizedCaseInsensitiveContains("not come back")
                || waitingOnIt.spokenLabel.localizedCaseInsensitiveContains("has not"),
            "it has to say the ball is still with the desk: \(waitingOnIt.spokenLabel)")
    }

    func testADeskNobodyHasEverSpokenToSaysThatRatherThanLookingFinished() throws {
        let fresh = try idle(.nobody)

        XCTAssertTrue(
            fresh.spokenLabel.localizedCaseInsensitiveContains("nobody")
                || fresh.spokenLabel.localizedCaseInsensitiveContains("never"),
            "a desk that has never been spoken to reads as one that finished "
            + "and went quiet: \(fresh.spokenLabel)")
    }

    /// The sweep, and the point of the section: **three meanings, three
    /// sentences.** Two that read the same are two he cannot act on
    /// differently.
    func testEachOfTheThreeMeaningsOfIdleGetsItsOwnSentence() throws {
        let sentences = try [LastSpeaker.agent, .owner, .nobody].map { try idle($0).spokenLabel }

        XCTAssertEqual(
            Set(sentences).count, 3,
            "IDLE means three different things and this app prints "
            + "\(Set(sentences).count) sentence(s) for them: \(sentences)")
        XCTAssertTrue(
            sentences.allSatisfy { !$0.isEmpty }, "an empty sentence claims nothing")
    }

    /// An older deck that never served the field must keep the sentence it
    /// always had, rather than being told a thing about itself nobody said.
    func testADeckThatNeverServedTheFieldIsNotGivenAStoryAboutItself() throws {
        let quiet = try idle(.unknown)

        XCTAssertEqual(quiet.tone, .quiet)
        XCTAssertEqual(quiet.headline, "Idle")
        XCTAssertFalse(
            quiet.spokenLabel.localizedCaseInsensitiveContains("nobody"),
            "the deck said nothing about who spoke last and the app claimed "
            + "nobody ever had: \(quiet.spokenLabel)")
    }

    // MARK: 3 — it survives the stream, which does not carry it

    /// §3.0: it is **not** on the `agent_state` frame. So a frame that moves a
    /// desk to `IDLE` must not be allowed to reset or invent who spoke last.
    func testTheStateFrameMovesTheStateAndLeavesWhoSpokeLastAlone() async {
        var desk = makeAgent("hemingway")
        desk.state = .working
        desk.lastSpeaker = .owner
        let roster = RosterModel(client: ScriptedDeckClient())
        await roster.replace(with: AgentsResponse.roster(from: [desk]))

        await roster.applyAgentState(name: "hemingway", state: .idle)

        let after = await roster.agentsByName()["hemingway"]
        XCTAssertEqual(after?.state, .idle)
        XCTAssertEqual(
            after?.lastSpeaker, .owner,
            "the frame carries {type, ts, name, state, blocked} and nothing "
            + "else, so anything this app decided about who spoke last from it "
            + "is invented")
    }

    /// The other half of §3.0's instruction: *take it from the newest message
    /// in the thread you already have open*. The desk answers on the live
    /// stream and goes quiet; the pane has to stop saying he is being waited
    /// on and start saying it came back.
    @MainActor
    func testAnAnswerOnTheStreamIsWhatMovesTheLineTheFrameCannotCarry() async {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .working
        chief.lastSpeaker = .owner
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        // The desk answers, and then its session goes quiet — the two frames in
        // the order the deck sends them.
        client.feeds = [.emitThenFinish([
            .message(makeMessage("m2", text: "Draft is out.", thread: "direct:chief",
                                 author: "chief", role: .agent), readOnly: false),
            .agentState(name: "chief", state: .idle, blocked: .unspecified),
        ])]
        let store = DeckStore(client: client, approvalPollInterval: 600)

        await store.loadRoster()
        await store.settle()

        let shown = store.deskStatus?.spokenLabel ?? ""
        XCTAssertTrue(
            shown.localizedCaseInsensitiveContains("answered"),
            "chief answered him on the open stream and then went idle, and the "
            + "pane still says he is being waited on: \(shown)")
    }

    /// And the other direction, which is the one that would quietly relabel a
    /// whole conversation: a `peer:` line is two desks talking to **each
    /// other**, and it says nothing about who spoke to *him*.
    func testTwoDesksTalkingToEachOtherDoNotCountAsTheDeskAnsweringHim() async {
        var desk = makeAgent("chief")
        desk.lastSpeaker = .owner
        let roster = RosterModel(client: ScriptedDeckClient())
        await roster.replace(with: AgentsResponse.roster(from: [desk]))

        await roster.applyLastSpeaker(agent: "chief", .unknown)

        let after = await roster.agentsByName()["chief"]
        XCTAssertEqual(
            after?.lastSpeaker, .owner,
            "something with nothing to say about who spoke last was allowed to "
            + "overwrite what the deck did say")
    }
}
