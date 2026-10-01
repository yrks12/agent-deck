import XCTest
@testable import DeckKit
@testable import DeckUI

/// §6.2 — the relay. A desk's `direct:` thread carries, in order, both what the
/// desk said to the owner *and* the traffic it exchanged with another desk. The
/// peer lines keep their own `peer:` id; the rest carry the page's `direct:`
/// one. **That difference is the entire wire attribution — no field was added
/// and none was renamed.**
///
/// The failure these exist to prevent is not "the label is missing". It is that
/// a dispatch drawn as an ordinary bubble reads as the desk talking to *him* —
/// it puts a worker's dense counts and ids into the manager's mouth as if they
/// were an answer to his question. That is worse than the "Messaged Hemingway:
/// …" preview it replaced, so every case below is checked for it, not only the
/// happy one.
///
/// Shape taken from the owner's own transcript, which is the product being
/// matched: he asks "any updates?", the desk says what it is checking, two
/// dispatches go out, two counts come back, and the desk summarises — one
/// thread, in order, without leaving the chat he is already reading.
final class RelayLineTests: XCTestCase {

    private let page = "direct:chief"
    private let desk = "chief"
    private let pair = "peer:chief|hemingway"

    /// The roster as the app holds it. Display names come from here, never from
    /// the wire name and never from an id.
    private let roster: [String: Agent] = [
        "chief": Agent(name: "chief", title: "The Builder", detail: ""),
        "hemingway": Agent(name: "hemingway", title: "Designer", detail: ""),
    ]

    private func shown(_ name: String) -> String {
        roster[name]?.displayName ?? Agent.displayName(forWireName: name)
    }

    private func attribution(
        author: String, thread: String, page: String? = nil, desk: String? = nil
    ) -> RelayAttribution {
        RelayLine.attribution(
            for: makeMessage("m", text: "…", thread: thread, author: author),
            page: page ?? self.page,
            desk: desk ?? self.desk,
            displayName: shown
        )
    }

    // MARK: the three render cases

    func testAMessageCarryingThePagesOwnThreadIdIsAnOrdinaryBubble() {
        XCTAssertEqual(attribution(author: "chief", thread: "direct:chief"), .ordinary)
        XCTAssertEqual(attribution(author: DeckOwner.name, thread: "direct:chief"), .ordinary)
    }

    func testADispatchThisDeskSentIsLabelledToTheOtherDesk() {
        let attributed = attribution(author: "chief", thread: pair)

        XCTAssertEqual(attributed, .dispatch(to: "Hemingway", threadID: pair))
        XCTAssertEqual(attributed.label, "to Hemingway")
    }

    func testAReplyThatCameBackIsLabelledFromTheDeskThatSentIt() {
        let attributed = attribution(author: "hemingway", thread: pair)

        XCTAssertEqual(attributed, .reply(from: "Hemingway", threadID: pair))
        XCTAssertEqual(attributed.label, "from Hemingway")
    }

    /// The two are opposites on the same id. If the direction were read off the
    /// thread rather than off the author, both would draw the same way and the
    /// reply would read as something this desk sent.
    func testTheSamePeerThreadDrawsBothDirectionsDifferently() {
        XCTAssertNotEqual(
            attribution(author: "chief", thread: pair),
            attribution(author: "hemingway", thread: pair),
            "who wrote it is the only thing separating a dispatch from a reply"
        )
    }

    // MARK: the name

    func testTheLabelIsTheDesksDisplayNameAndNeverAWireNameOrAnId() {
        let label = try? XCTUnwrap(attribution(author: "chief", thread: pair).label)

        XCTAssertEqual(label, "to Hemingway", "the roster's display name, capitalised")
        XCTAssertFalse(label?.contains("peer:") ?? true, "never an id")
        XCTAssertFalse(label?.contains("|") ?? true, "never half a thread id")
    }

    /// The name is the *other* participant of the peer id, not the author — on
    /// a reply those differ from what a naive reading of `author` would give
    /// only by luck, and on a dispatch they always differ.
    func testTheNameIsTheOtherParticipantOfThePeerIdNotTheAuthor() {
        XCTAssertEqual(
            attribution(author: "chief", thread: pair).label, "to Hemingway",
            "a dispatch is labelled with who it went to, not with who sent it"
        )
    }

    /// A desk renames itself the moment it is hired, and the peer id it was
    /// already in keeps the old name. There is no roster row for that name any
    /// more — the label must still be a name a person can read, never the id.
    func testAPeerIdNamingADeskThatHasSinceBeenRenamedStillReadsAsAName() {
        let stale = "peer:chief|new-hire-7f3a1c"
        let attributed = attribution(author: "chief", thread: stale)

        XCTAssertEqual(attributed, .dispatch(to: "New-hire-7f3a1c", threadID: stale))
        XCTAssertFalse(attributed.label?.contains(":") ?? true, "an id leaked into the label")
        XCTAssertEqual(attributed.peerThreadID, stale, "the stale id still resolves, so keep it")
    }

    // MARK: the cases that are not the happy path

    /// `thread_id` is optional on the wire and decodes to `""` when absent.
    /// Comparing that to the page id says "differs" for **every** message in
    /// the thread, which would relabel an entire conversation as overheard
    /// traffic. An absent field is a missing fact, not a difference.
    func testAMessageWithNoThreadIdAtAllIsAnOrdinaryBubble() {
        XCTAssertEqual(attribution(author: "chief", thread: ""), .ordinary)
    }

    /// The author is not on the roster: a desk fired since, or one this client
    /// has never fetched. It is still not this desk, so it is still a reply,
    /// and the label still comes off the peer id.
    func testARelayedLineWhoseAuthorIsUnknownIsStillDrawnAsAReply() {
        let attributed = attribution(author: "ghost", thread: "peer:chief|globex")

        XCTAssertEqual(attributed, .reply(from: "Globex", threadID: "peer:chief|globex"))
        XCTAssertFalse(attributed.label?.isEmpty ?? true, "an unknown author must not erase the label")
    }

    /// A peer id this client cannot parse — a malformed one, or a `group:` id
    /// arriving on a thread that predates §14. It is still not the page's own
    /// thread, so it is still not the desk answering him. Say so without a
    /// name rather than silently promoting it to a plain bubble.
    func testAnUnparseableRelayedIdIsStillNeverDrawnAsTheDeskTalkingToTheOwner() {
        for id in ["peer:chief", "peer:", "group:launch", "peer:|hemingway"] {
            let attributed = attribution(author: "chief", thread: id)
            XCTAssertNotEqual(attributed, .ordinary, "\(id) is not this page's thread")
            XCTAssertEqual(attributed.label, "to another desk", "\(id) has no name to give")
        }
    }

    /// The fence in §6.2: a message only ever appears in the `direct:` threads
    /// of the two desks that sent and received it. If one ever arrives for a
    /// pair this desk is not in, naming it after a participant would invent an
    /// edge the org chart does not have.
    func testAPairThisDeskIsNotInIsNamedAfterNeitherOfItsMembersWrongly() {
        let attributed = attribution(author: "seeker", thread: "peer:hemingway|seeker")

        XCTAssertEqual(attributed, .reply(from: "Hemingway", threadID: "peer:hemingway|seeker"),
                       "the name is the participant that did not write it")
    }

    // MARK: the tap

    func testEveryRelayedLineCarriesThePeerThreadIdItCameFrom() {
        XCTAssertEqual(attribution(author: "chief", thread: pair).peerThreadID, pair)
        XCTAssertEqual(attribution(author: "hemingway", thread: pair).peerThreadID, pair)
        XCTAssertNil(attribution(author: "chief", thread: page).peerThreadID,
                     "an ordinary bubble has nowhere to go")
    }

    // MARK: the transcript, in order

    /// The owner's own thread, verbatim in shape: ask, answer, two dispatches,
    /// two replies, summary. Order is the server's cursor order and the relay
    /// must not reorder, group or hoist anything out of it.
    func testTheDeskThreadDrawsAskDispatchReplyAndSummaryInOneOrderedRun() {
        let rows = Transcript.rows(observedThread(), page: page, desk: desk, displayName: shown)

        XCTAssertEqual(rows.map(\.attribution.label), [
            nil,                // owner: any updates?
            nil,                // chief: Checking Globex opens and Acme ads.
            "to Globex",
            "to Acme",
            "from Acme",
            "from Globex",
            nil,                // chief: Globex just now: no new opens…
        ])
        XCTAssertEqual(rows.map(\.message.id), observedThread().map(\.id),
                       "the relay reorders nothing")
    }

    /// One record, three views, one id. The direct page, the peer page and the
    /// stream all carry it; the transcript may show it once.
    func testTheSameRecordArrivingFromThreeViewsIsDrawnOnce() {
        let relayed = makeMessage("shared", text: "counts only", thread: pair, author: "chief")
        let rows = Transcript.rows(
            [makeMessage("c1", text: "any updates?", thread: page, author: DeckOwner.name),
             relayed, relayed, relayed],
            page: page, desk: desk, displayName: shown
        )

        XCTAssertEqual(rows.count, 2, "three deliveries of one id are one line")
        XCTAssertEqual(rows.filter { $0.id == "shared" }.count, 1)
    }

    // MARK: what it says out loud

    func testARelayedLineSaysItIsOverheardTrafficBeforeItSaysWhatWasSaid() {
        let rows = Transcript.rows(observedThread(), page: page, desk: desk, displayName: shown)
        let dispatch = rows[2].spokenLabel(timestamp: "19:26")
        let reply = rows[5].spokenLabel(timestamp: "19:27")

        XCTAssertTrue(dispatch.hasPrefix("Dispatch to Globex."), "got: \(dispatch)")
        XCTAssertTrue(dispatch.contains("Counts only."), "the words themselves still get read: \(dispatch)")
        XCTAssertTrue(reply.hasPrefix("Reply from Globex."), "got: \(reply)")
    }

    func testAnOrdinaryBubbleKeepsExactlyTheSentenceItAlreadyHad() {
        let rows = Transcript.rows(observedThread(), page: page, desk: desk, displayName: shown)

        XCTAssertEqual(
            rows[0].spokenLabel(timestamp: "19:25"),
            rows[0].message.spokenLabel(timestamp: "19:25"),
            "§6.2 changed nothing about a message to the owner"
        )
    }

    // MARK: the wire

    /// The payload from §6.2, decoded as it arrives. No `relay` field, no
    /// `kind` on the message, no rename — one `thread_id` that differs.
    func testTheWireSaysNothingButADifferentThreadId() throws {
        let json = """
        {"thread_id": "direct:chief", "kind": "direct", "read_only": false,
         "participants": ["owner", "chief"], "messages": [
          {"id": "1", "cursor": "c1", "thread_id": "direct:chief", "author": "owner",
           "role": "owner", "ts": 1756000040.0, "text": "any updates?", "attachments": []},
          {"id": "2", "cursor": "c2", "thread_id": "peer:chief|hemingway", "author": "chief",
           "role": "agent", "ts": 1756000050.0, "text": "Status now: any new page_views since 307?",
           "attachments": []},
          {"id": "3", "cursor": "c3", "thread_id": "peer:chief|hemingway", "author": "hemingway",
           "role": "agent", "ts": 1756000060.0, "text": "page_views new: 0  replies: 0  max: 307",
           "attachments": []}
         ], "has_more_before": false, "has_more_after": false}
        """
        let decoded = try DeckCoding.decoder.decode(MessagePage.self, from: Data(json.utf8))
        let rows = Transcript.rows(
            decoded.messages, page: decoded.threadID, desk: "chief", displayName: shown
        )

        XCTAssertEqual(rows.map(\.attribution.label), [nil, "to Hemingway", "from Hemingway"])
    }

    // MARK: live traffic, off the one stream

    /// A dispatch that goes out while he is watching must appear while he is
    /// watching. The frame carries the `peer:` id, so a sync that only admits
    /// its own thread id drops it and the conversation stops moving.
    func testADispatchArrivingLiveLandsInTheDeskThreadHeIsWatching() async throws {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: page, messages: [],
                                    isReadOnly: false, participants: [DeckOwner.name, desk])]
        client.feeds = [.emitThenFinish([
            .message(makeMessage("r1", text: "counts only", thread: pair, author: "chief"),
                     readOnly: true)
        ])]
        let sync = ConversationSync(client: client, threadID: page)

        try await sync.run()

        let held = await sync.messages()
        XCTAssertEqual(held.map(\.id), ["r1"])
    }

    /// A `peer:` frame is `read_only: true` by contract. Taking that flag for
    /// the direct thread removes his composer — the conversation he is holding
    /// becomes view-only because somebody else's desk was mentioned in it.
    func testARelayedFrameDoesNotTakeTheComposerAway() async throws {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: page, messages: [],
                                    isReadOnly: false, participants: [DeckOwner.name, desk])]
        client.feeds = [.emitThenFinish([
            .message(makeMessage("r1", thread: pair, author: "chief"), readOnly: true)
        ])]
        let sync = ConversationSync(client: client, threadID: page)

        try await sync.run()

        let readOnly = await sync.isReadOnly
        XCTAssertFalse(readOnly, "a relayed line must not turn his own chat view-only")
    }

    /// The fence again, on the stream this time: two of a manager's reports
    /// talking to each other never enters the manager's thread.
    func testTrafficBetweenTwoOtherDesksNeverEntersThisThread() async throws {
        let client = ScriptedDeckClient()
        client.pages = [MessagePage(threadID: page, messages: [],
                                    isReadOnly: false, participants: [DeckOwner.name, desk])]
        client.feeds = [.emitThenFinish([
            .message(makeMessage("x1", thread: "peer:hemingway|seeker", author: "seeker"),
                     readOnly: true)
        ])]
        let sync = ConversationSync(client: client, threadID: page)

        try await sync.run()

        let held = await sync.messages()
        XCTAssertTrue(held.isEmpty, "the manager's chat grows with what the manager said and heard")
    }

    // MARK: what the pane publishes

    func testTheOpenThreadPublishesOneAttributedRowPerRecord() async {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [roster["chief"]!, roster["hemingway"]!],
            threads: [makeThread(page, agent: desk)],
            sectionOrder: ["Work"]
        )
        client.pages = [MessagePage(threadID: page, messages: observedThread(),
                                    isReadOnly: false, participants: [DeckOwner.name, desk])]
        client.feeds = [.emitThenFinish([])]
        let store = await DeckStore(client: client)

        await store.loadRoster()
        await store.settle()

        guard case .loaded(let screen) = await store.thread else {
            return XCTFail("the conversation never resolved")
        }
        XCTAssertEqual(screen.rows.count, screen.messages.count,
                       "a row and a message are the same record; two arrays is how one gets counted twice")
        XCTAssertEqual(screen.rows.map(\.attribution.label),
                       [nil, nil, "to Globex", "to Acme", "from Acme", "from Globex", nil])
    }

    // MARK: the fixture the harness drives

    func testTheFixtureDeskThreadCarriesADispatchAndAReplyOnPeerIds() async throws {
        let page = try await FixtureDeckClient().messages(
            threadID: "direct:chief", since: nil, limit: 50
        )
        let ids = Set(page.messages.map(\.threadID))

        XCTAssertTrue(ids.contains("direct:chief"), "the desk still answers him directly")
        XCTAssertTrue(ids.contains { $0.hasPrefix("peer:") },
                      "no peer id in the fixture means the harness can never see a relayed line")
        let rows = Transcript.rows(page.messages, page: page.threadID, desk: "chief",
                                   displayName: { $0 })
        XCTAssertTrue(rows.contains { if case .dispatch = $0.attribution { return true } else { return false } })
        XCTAssertTrue(rows.contains { if case .reply = $0.attribution { return true } else { return false } })
    }

    // MARK: -

    /// The owner's thread, in his order, with the two workers' names taken from
    /// his own transcript.
    private func observedThread() -> [Message] {
        [
            makeMessage("o1", cursor: "1", text: "any updates?", thread: page,
                        author: DeckOwner.name, role: .owner),
            makeMessage("o2", cursor: "2", text: "Checking Globex opens and Acme ads.",
                        thread: page, author: "chief"),
            makeMessage("o3", cursor: "3", text: "Counts only.",
                        thread: "peer:chief|globex", author: "chief"),
            makeMessage("o4", cursor: "4", text: "Quick status. One line.",
                        thread: "peer:chief|acme", author: "chief"),
            makeMessage("o5", cursor: "5", text: "Since 07:20: no real customer, no paid.",
                        thread: "peer:chief|acme", author: "acme"),
            makeMessage("o6", cursor: "6", text: "page_views new: 0  replies: 0  max_event_id: 307",
                        thread: "peer:chief|globex", author: "globex"),
            makeMessage("o7", cursor: "7", text: "Globex just now: no new opens.",
                        thread: page, author: "chief"),
        ]
    }
}
