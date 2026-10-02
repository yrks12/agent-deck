import XCTest
@testable import DeckKit

/// **K1 on the client: a routine is not the owner, and an unknown role is not
/// either.**
///
/// ours-03: "Hired: listing-closer now reports to you…" drawn as his own blue
/// bubble, and every 07:57 routine fire with it. The deck now says who actually
/// sent a line (`role: "system"`, `author: "routine" | "deck" | "engineer"`);
/// this pins that the app keeps that fact instead of folding it into `owner`.
final class MessageAuthorTruthTests: XCTestCase {

    private func decode(_ json: String) throws -> Message {
        try DeckCoding.decoder.decode(Message.self, from: Data(json.utf8))
    }

    func testARoutineFireIsASystemLineAndNeverHisOwn() throws {
        let fire = try decode(#"""
        {"id":"m1","thread_id":"direct:atlas","author":"routine","role":"system",
         "ts":1.0,"text":"Morning Shorts report","channel":"text","kind":"text"}
        """#)
        XCTAssertEqual(fire.role, .system)
        XCTAssertFalse(fire.isFromUser, "a routine fire drawn as his bubble is ours-03")
        XCTAssertTrue(fire.isSystem)
    }

    /// The contract: a closed set, and anything else renders as agent — never
    /// guessed back into `owner` from the author.
    func testAnUnknownRoleRendersAsAnAgentEvenWhenTheAuthorIsHim() throws {
        let odd = try decode(#"{"id":"m2","author":"owner","role":"narrator","ts":1.0,"text":"x"}"#)
        XCTAssertEqual(odd.role, .agent)
        XCTAssertFalse(odd.isFromUser)
    }

    /// An older deck sends no role at all; the author still decides then.
    func testAMissingRoleIsStillDerivedFromTheAuthor() throws {
        XCTAssertEqual(try decode(#"{"id":"m3","author":"owner","ts":1.0,"text":"x"}"#).role, .owner)
        XCTAssertEqual(try decode(#"{"id":"m4","author":"atlas","ts":1.0,"text":"x"}"#).role, .agent)
    }

    func testChannelAndKindDefaultToTextAndKeepVoice() throws {
        let plain = try decode(#"{"id":"m5","author":"owner","role":"owner","ts":1.0,"text":"x"}"#)
        XCTAssertEqual(plain.channel, .text)
        XCTAssertEqual(plain.kind, .text)
        XCTAssertNil(plain.decision)

        let spoken = try decode(#"""
        {"id":"m6","author":"owner","role":"owner","ts":1.0,"text":"status of acme","channel":"voice","kind":"text"}
        """#)
        XCTAssertEqual(spoken.channel, .voice)

        let future = try decode(#"""
        {"id":"m7","author":"atlas","role":"agent","ts":1.0,"text":"x","channel":"carrier-pigeon","kind":"poll"}
        """#)
        XCTAssertEqual(future.channel, .text, "an unknown channel must not fail the page")
        XCTAssertEqual(future.kind, .text)
    }

    /// K4: the decision rides on the message, with every field the card draws.
    func testADecisionMessageCarriesItsPromptOptionsAndState() throws {
        let asked = try decode(Self.decisionJSON(state: "open", answer: "null"))
        XCTAssertEqual(asked.kind, .decision)
        let decision = try XCTUnwrap(asked.decision)
        XCTAssertEqual(decision.id, "dec_0123456789ab")
        XCTAssertEqual(decision.prompt, "Umbrella waitlist copy?")
        XCTAssertEqual(decision.help, "Full draft: /workspace/copy.md")
        XCTAssertEqual(decision.options.map(\.label), ["Approve as-is", "I'll send edits", "Hold copy"])
        XCTAssertEqual(decision.options.map(\.style), [.primary, .default, .danger])
        XCTAssertTrue(decision.allowCustom)
        XCTAssertEqual(decision.state, .open)
        XCTAssertNil(decision.answer)

        let answered = try XCTUnwrap(
            try decode(Self.decisionJSON(state: "answered", answer: #""Hold copy""#)).decision)
        XCTAssertEqual(answered.state, .answered)
        XCTAssertEqual(answered.answer, "Hold copy")
    }

    /// The same record survives an encode/decode round trip — the fixture and
    /// the local cache both go through it.
    func testAMessageRoundTripsItsNewFields() throws {
        let original = try decode(Self.decisionJSON(state: "skipped", answer: "null"))
        let again = try DeckCoding.decoder.decode(
            Message.self, from: try DeckCoding.encoder.encode(original))
        XCTAssertEqual(again, original)
    }

    /// K4's SSE frame: create and every state change.
    func testTheStreamCarriesADecisionFrame() throws {
        let frame = SSEEvent(name: nil, data: #"""
        {"type":"decision","thread_id":"direct:atlas","decision":{"id":"dec_0123456789ab",
         "prompt":"Headline?","options":[{"label":"A","value":"Go with A"},{"label":"B","value":"Go with B"}],
         "allow_custom":false,"state":"answered","answer":"Go with B"}}
        """#, id: nil)
        guard case .decision(let threadID, let decision)? = DeckEvent(sse: frame) else {
            return XCTFail("a decision frame was dropped")
        }
        XCTAssertEqual(threadID, "direct:atlas")
        XCTAssertEqual(decision.state, .answered)
        XCTAssertEqual(decision.options.map(\.style), [.default, .default])
    }

    static func decisionJSON(state: String, answer: String) -> String {
        """
        {"id":"m8","thread_id":"direct:atlas","author":"atlas","role":"agent","ts":2.0,
         "text":"Umbrella waitlist copy?","channel":"text","kind":"decision",
         "decision":{"id":"dec_0123456789ab","prompt":"Umbrella waitlist copy?",
           "help":"Full draft: /workspace/copy.md",
           "options":[{"label":"Approve as-is","value":"Approve copy as-is","style":"primary"},
                      {"label":"I'll send edits","value":"I'll send edits","style":"default"},
                      {"label":"Hold copy","value":"Hold copy","style":"danger"}],
           "allow_custom":true,"state":"\(state)","answer":\(answer)}}
        """
    }
}
