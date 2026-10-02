import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"I need to be able to reply to messages on both apps."**
///
/// A quote-reply, the WhatsApp / iMessage way: he picks one message, a strip
/// above the composer says which, his bubble carries the quote, and a tap on
/// the quote takes him back to the original. The deck side is
/// `tests/test_quote_reply.py`; this pins the client half both apps share:
///
/// 1. the wire: `reply_to` decodes onto `Message` and goes out on a send;
/// 2. the composer's reply state — set, cancelled, sent, and kept on a refusal;
/// 3. the jump — a tapped quote names the row to scroll to, opening the
///    agent-to-agent rollup the original is folded into when it has to.
@MainActor
final class QuoteReplyTests: XCTestCase {

    // MARK: 1 — the wire

    func testAReplyDecodesItsQuoteOffTheWire() throws {
        let json = """
        {"id":"m2","cursor":"2-m2","thread_id":"direct:chief","author":"owner",
         "role":"owner","ts":1788222777.2,"text":"yes, tag it",
         "reply_to":{"id":"m1","author":"chief","excerpt":"Want me to tag the release?"}}
        """
        let message = try JSONDecoder().decode(Message.self, from: Data(json.utf8))
        XCTAssertEqual(message.replyTo,
                       MessageQuote(id: "m1", author: "chief", excerpt: "Want me to tag the release?"))

        let again = try JSONDecoder().decode(Message.self, from: JSONEncoder().encode(message))
        XCTAssertEqual(again.replyTo, message.replyTo, "the quote is lost on a round-trip")
    }

    func testAMessageThatAnswersNothingHasNoQuote() throws {
        let json = #"{"id":"m1","cursor":"1-m1","thread_id":"direct:chief","author":"chief","text":"hi"}"#
        XCTAssertNil(try JSONDecoder().decode(Message.self, from: Data(json.utf8)).replyTo)
    }

    func testAReplySendsTheQuotedIdAndAPlainSendDoesNot() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/threads/direct:chief/messages"] = Data("""
        {"ok":true,"delivered":true,
         "message":{"id":"m2","cursor":"2-m2","thread_id":"direct:chief","author":"owner",
          "role":"owner","ts":1.0,"text":"yes",
          "reply_to":{"id":"m1","author":"chief","excerpt":"Tag it?"}}}
        """.utf8)
        let store = InMemoryTokenStore()
        try store.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: store, performer: performer)

        let sent = try await client.send(threadID: "direct:chief", text: "yes", replyTo: "m1")

        let body = try JSONSerialization.jsonObject(
            with: try XCTUnwrap(performer.requests.first?.httpBody)) as? [String: String]
        XCTAssertEqual(body?["reply_to"], "m1")
        XCTAssertEqual(body?["text"], "yes")
        XCTAssertEqual(sent.replyTo?.id, "m1")
    }

    // MARK: 2 — the composer's reply state

    func testTheStripNamesTheAuthorAndTheFirstLine() {
        let card = makeMessage("m1", text: "\n  Three options for the launch:\n1. Friday\n2. Next week",
                               thread: "direct:chief", author: "chief")
        var draft = ReplyDraft()

        XCTAssertTrue(draft.begin(card, displayName: { $0 == "chief" ? "Chief" : $0 }))

        XCTAssertEqual(draft.target?.messageID, "m1")
        XCTAssertEqual(draft.target?.authorLabel, "Chief")
        XCTAssertEqual(draft.target?.firstLine, "Three options for the launch:")
        XCTAssertEqual(draft.replyToID, "m1")
    }

    func testHisOwnLineIsQuotedAsYou() {
        var draft = ReplyDraft()
        draft.begin(makeMessage("m1", text: "look at churn", author: DeckOwner.name, role: .owner))
        XCTAssertEqual(draft.target?.authorLabel, "You")
    }

    func testTheCrossCancelsTheReply() {
        var draft = ReplyDraft()
        draft.begin(makeMessage("m1"))
        draft.cancel()
        XCTAssertNil(draft.target)
        XCTAssertNil(draft.replyToID)
    }

    /// A line the deck has not given an id yet, or a deck caption, cannot be
    /// quoted: there is nothing on the deck for `reply_to` to name.
    func testAnUnsentEchoOrACaptionCannotBeQuoted() {
        var draft = ReplyDraft()
        XCTAssertFalse(draft.begin(makeMessage("pending:direct:chief:1", role: .owner)))
        XCTAssertFalse(draft.begin(makeMessage("r1", author: "routine", role: .system)))
        XCTAssertNil(draft.target)
    }

    func testTheMacComposerSendsTheReplyAndThenForgetsIt() async {
        let (store, client) = await loadedStore()
        store.reply(to: makeMessage("m1", text: "Tag the release?", thread: "direct:chief", author: "chief"))
        XCTAssertEqual(store.replyDraft.target?.messageID, "m1")

        store.composerDraft = "yes"
        await store.submitComposer()

        XCTAssertEqual(client.sentReplyTo, ["m1"], "the send did not say which message it answers")
        XCTAssertNil(store.replyDraft.target, "the strip is still up after the reply went")
        guard case .loaded(let screen) = store.thread,
              let echoed = screen.messages.first(where: { $0.text == "yes" })
        else { return XCTFail("his reply is not on screen") }
        XCTAssertEqual(echoed.replyTo?.id, "m1", "his bubble does not show the quote until the deck echoes it")
    }

    func testARefusedReplyKeepsTheQuoteUpWithHisWords() async {
        let (store, client) = await loadedStore()
        client.sendError = .transport("connection refused")
        store.reply(to: makeMessage("m1", thread: "direct:chief", author: "chief"))

        store.composerDraft = "yes"
        await store.submitComposer()

        XCTAssertEqual(store.replyDraft.target?.messageID, "m1")
        XCTAssertEqual(store.composerDraft, "yes")
    }

    func testCancellingOnTheMacSendsAPlainMessage() async {
        let (store, client) = await loadedStore()
        store.reply(to: makeMessage("m1", thread: "direct:chief", author: "chief"))
        store.cancelReply()

        store.composerDraft = "unrelated"
        await store.submitComposer()

        XCTAssertEqual(client.sentReplyTo, [nil])
    }

    /// ⌘R: the selected message, or — nothing selected — the desk's newest.
    func testCommandRRepliesToTheSelectedMessageOrElseTheDesksNewest() async {
        let (store, _) = await loadedStore(history: [
            makeMessage("a1", cursor: "1-a1", text: "first", thread: "direct:chief", author: "chief"),
            makeMessage("o1", cursor: "2-o1", text: "mine", thread: "direct:chief",
                        author: DeckOwner.name, role: .owner),
            makeMessage("a2", cursor: "3-a2", text: "second", thread: "direct:chief", author: "chief"),
        ])

        store.replyToSelected()
        XCTAssertEqual(store.replyDraft.target?.messageID, "a2")

        store.selectedMessageID = "a1"
        store.replyToSelected()
        XCTAssertEqual(store.replyDraft.target?.messageID, "a1")
    }

    // MARK: 3 — a tap on the quote goes to the original

    func testAQuoteOnScreenScrollsStraightToItsRow() {
        let items = timeline([
            .said(row("m1", author: "chief")),
            .said(row("m2", author: DeckOwner.name, role: .owner)),
        ])
        XCTAssertEqual(QuoteJump.destination(of: "m1", in: items), .row("said:m1"))
    }

    /// The agent-to-agent chip: the original is folded into "2 messages with
    /// Seeker". The jump opens that rollup and then lands on the line.
    func testAQuoteInsideARollupOpensItFirst() {
        let relayed = { (id: String) in
            ThreadEntry.said(TranscriptRow(
                message: makeMessage(id, text: "x", thread: "peer:chief|seeker", author: "seeker"),
                attribution: .reply(from: "Seeker", threadID: "peer:chief|seeker")))
        }
        let items = timeline([relayed("p1"), relayed("p2"), .said(row("m3", author: "chief"))])
        guard case .rollup(let rollup) = items.first(where: {
            if case .rollup = $0 { return true } else { return false }
        }) else { return XCTFail("the relayed lines were not rolled up") }

        XCTAssertEqual(QuoteJump.destination(of: "p2", in: items),
                       .insideRollup(rollupID: rollup.id, row: "said:p2"))
    }

    func testAQuoteOfALineNotLoadedSaysSo() {
        XCTAssertEqual(QuoteJump.destination(of: "gone", in: timeline([.said(row("m1"))])),
                       .notLoaded)
    }

    func testTheMacStoreTurnsATapIntoAScrollRequest() async {
        let (store, _) = await loadedStore()
        store.jump(to: MessageQuote(id: "m1", author: "chief", excerpt: "x"))
        let first = store.quoteJump
        XCTAssertEqual(first?.messageID, "m1")

        // A second tap on the same quote must scroll again, so it is a new request.
        store.jump(to: MessageQuote(id: "m1", author: "chief", excerpt: "x"))
        XCTAssertNotEqual(store.quoteJump, first)
    }

    // MARK: helpers

    private func row(_ id: String, author: String = "chief", role: MessageRole = .agent) -> TranscriptRow {
        TranscriptRow(message: makeMessage(id, text: "t", thread: "direct:chief", author: author, role: role),
                      attribution: .ordinary)
    }

    private func timeline(_ entries: [ThreadEntry]) -> [TranscriptItem] {
        ThreadTimeline.items(entries, desk: "chief")
    }

    private func loadedStore(history: [Message] = []) async -> (DeckStore, ScriptedDeckClient) {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief")], threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:chief", messages: history,
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        client.sendResult = makeMessage("mX", cursor: "9-mX", thread: "direct:chief",
                                        author: DeckOwner.name, role: .owner)
        let store = DeckStore(client: client)
        await store.loadRoster()
        await store.settle()
        return (store, client)
    }
}
