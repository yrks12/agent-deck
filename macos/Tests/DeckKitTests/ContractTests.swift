import XCTest
@testable import DeckKit

/// Every payload below is copied verbatim out of `docs/client-api.md`. If the
/// deck changes shape, these fail before any screen does — which is the only
/// place a wire-format change should ever be noticed.
final class ContractTests: XCTestCase {

    // MARK: GET /v1/agents — the sidebar, in one call

    private let agentsPayload = Data("""
    {
      "agents": [
        {
          "name": "chief", "label": "Negotiator", "section": "Work",
          "avatar": "chief.png", "state": "WORKING", "unread": 1,
          "last_activity_at": 1788222702.087623, "preview": "You: status please",
          "thread_id": "direct:chief", "pinned": true, "notifications": true,
          "desk": true, "project": "acme", "session_id": "sid-chief",
          "boss": null, "reports": ["hemingway"]
        },
        {
          "name": "hemingway", "label": "Researcher", "section": "Personal",
          "avatar": null, "state": "OFFLINE", "unread": 1,
          "last_activity_at": 1756000050.0,
          "preview": "Message from chief: Draft the counter-offer, keep it under 200 words.",
          "thread_id": "direct:hemingway", "pinned": false, "notifications": true,
          "desk": true, "project": "", "session_id": null,
          "boss": "chief", "reports": []
        }
      ],
      "generated_at": 1788222702.097551
    }
    """.utf8)

    func testTheAgentsPayloadDecodesIntoARosterTheSidebarCanDrawWithNoSecondCall() throws {
        let payload = try DeckCoding.decoder.decode(AgentsResponse.self, from: agentsPayload)
            .asRosterPayload()

        XCTAssertEqual(payload.agents.map(\.name), ["chief", "hemingway"])
        XCTAssertEqual(payload.agents[0].title, "Negotiator", "label is the title chip")
        XCTAssertTrue(payload.agents[0].isPinned)
        XCTAssertEqual(payload.agents[1].section, "Personal")
        XCTAssertEqual(payload.sectionOrder, ["Work", "Personal"], "sections come from the agents")

        // Row state travels on the agent row, so no per-agent follow-up exists.
        let snapshot = SidebarSnapshot.build(from: payload)
        // `allRows`, not `sections`: this payload's "chief" reports to nobody
        // and has a desk under it, so the sidebar now pins it above the roster
        // rather than mixing it into the list. Same row, one block higher.
        let chief = try XCTUnwrap(snapshot.allRows.first { $0.id == "chief" })
        XCTAssertEqual(
            snapshot.chief?.id, "chief",
            "the payload the deck really sends does name a chief of staff")
        XCTAssertEqual(chief.unreadCount, 1)
        XCTAssertEqual(chief.threadID, "direct:chief")
        XCTAssertEqual(chief.preview.line, "You: status please")
        XCTAssertEqual(chief.timestamp, Date(timeIntervalSince1970: 1788222702.087623))
    }

    func testAgentStatesDecodeAndAnUnknownOneIsTreatedAsIdle() throws {
        XCTAssertEqual(AgentState(wire: "NEEDS_YOU"), .needsYou)
        XCTAssertEqual(AgentState(wire: "OFFLINE"), .offline)
        XCTAssertEqual(AgentState(wire: "SOMETHING_NEW"), .idle, "an unknown state must not break a row")
    }

    func testATimestampIsEpochSecondsAndAMissingOneIsNull() throws {
        let json = Data("""
        {"agents": [{"name": "ghost", "label": "", "section": "Work", "avatar": null,
          "state": "IDLE", "unread": 0, "last_activity_at": null, "preview": "",
          "thread_id": "direct:ghost", "pinned": false, "notifications": true,
          "desk": true, "project": "", "session_id": null, "boss": null, "reports": []}],
         "generated_at": 1788222702.0}
        """.utf8)

        let agent = try DeckCoding.decoder.decode(AgentsResponse.self, from: json).agents[0]

        XCTAssertNil(agent.lastActivityAt, "no time is null, never epoch zero")
    }

    // MARK: GET /v1/threads

    func testTheThreadListCarriesReadOnlyAndAPreBuiltPeerTitle() throws {
        let json = Data("""
        {"threads": [
          {"id": "direct:chief", "kind": "direct", "title": "chief",
           "participants": ["owner", "chief"], "read_only": false, "message_count": 1,
           "last_ts": 1788222777.0982761, "last_cursor": "001788222777098276-393395200ac2",
           "preview": "status please"},
          {"id": "peer:chief|hemingway", "kind": "peer", "title": "chief ⇄ hemingway",
           "participants": ["chief", "hemingway"], "read_only": true, "message_count": 1,
           "last_ts": 1756000050.0, "last_cursor": "001756000050000000-9f2a15f5b1bf-1756000050",
           "preview": "Draft the counter-offer, keep it under 200 words."}
         ], "generated_at": 1788222777.207811}
        """.utf8)

        let threads = try DeckCoding.decoder.decode(ThreadsResponse.self, from: json).threads

        XCTAssertFalse(threads[0].isReadOnly)
        XCTAssertTrue(threads[1].isReadOnly)
        XCTAssertEqual(threads[1].title, "chief ⇄ hemingway", "the server already assembled it")
        XCTAssertEqual(threads[1].lastCursor, "001756000050000000-9f2a15f5b1bf-1756000050")
    }

    func testThePeerTitleFromTheServerIsWhatTheHeaderShows() {
        let presentation = ThreadPresentation.make(
            threadID: "peer:chief|hemingway",
            participants: ["chief", "hemingway"],
            isReadOnly: true,
            title: "chief ⇄ hemingway",
            agents: [:]
        )

        XCTAssertEqual(presentation.headerTitle, "chief ⇄ hemingway")
    }

    // MARK: GET /v1/threads/{id}/messages

    func testAMessagePageDecodesWithOpaqueCursorsAndDerivedAttachments() throws {
        let json = Data("""
        {"thread_id": "peer:chief|hemingway", "kind": "peer", "read_only": true,
         "participants": ["chief", "hemingway"],
         "messages": [{
           "id": "9f2a15f5b1bf-1756000050",
           "cursor": "001756000050000000-9f2a15f5b1bf-1756000050",
           "thread_id": "peer:chief|hemingway", "author": "chief", "role": "agent",
           "ts": 1756000050.0, "text": "Draft the counter-offer, keep it under 200 words.",
           "attachments": [{"kind": "image", "value": "/tmp/spread.png"},
                           {"kind": "link", "value": "https://example.com"}],
           "via": "transcript"}],
         "has_more_before": false, "has_more_after": false,
         "next_since": "001756000050000000-9f2a15f5b1bf-1756000050",
         "next_before": "001756000050000000-9f2a15f5b1bf-1756000050"}
        """.utf8)

        let page = try DeckCoding.decoder.decode(MessagePage.self, from: json)

        XCTAssertTrue(page.isReadOnly)
        XCTAssertEqual(page.participants, ["chief", "hemingway"])
        XCTAssertEqual(page.nextSince, "001756000050000000-9f2a15f5b1bf-1756000050")
        XCTAssertFalse(page.hasMoreBefore)

        let message = try XCTUnwrap(page.messages.first)
        XCTAssertEqual(message.cursor, "001756000050000000-9f2a15f5b1bf-1756000050")
        XCTAssertEqual(message.author, "chief")
        XCTAssertEqual(message.role, .agent)
        XCTAssertFalse(message.isFromUser)
        XCTAssertEqual(message.sentAt, Date(timeIntervalSince1970: 1756000050.0))
        XCTAssertEqual(message.attachments.map(\.kind), [.image, .link])
    }

    func testTheOwnersOwnMessagesAreTheOnesThatSitOnTheRight() throws {
        let json = Data("""
        {"id": "9e6da0f8c369", "cursor": "001788222777235606-9e6da0f8c369",
         "thread_id": "direct:chief", "author": "owner", "role": "owner",
         "ts": 1788222777.235606, "text": "ship it", "attachments": [], "via": "office"}
        """.utf8)

        let message = try DeckCoding.decoder.decode(Message.self, from: json)

        XCTAssertTrue(message.isFromUser)
        XCTAssertEqual(message.author, "owner")
    }

    func testCursorsAreComparableAsStringsAndTheStoreOrdersByThem() {
        var store = ConversationStore()
        store.ingest([
            makeMessage("b", cursor: "001788222777235606-b"),
            makeMessage("a", cursor: "001756000050000000-a"),
        ])

        XCTAssertEqual(store.messages.map(\.id), ["a", "b"], "lexical cursor order is chronological")
        XCTAssertEqual(store.cursor, "001788222777235606-b", "the resume point is the newest held")
    }

    func testThePostResponseWrapsTheMessageAndSaysWhetherItWasDelivered() throws {
        let json = Data("""
        {"ok": true, "delivered": false,
         "message": {"id": "9e6da0f8c369", "cursor": "001788222777235606-9e6da0f8c369",
          "thread_id": "direct:chief", "author": "owner", "role": "owner",
          "ts": 1788222777.235606, "text": "ship it", "attachments": [], "via": "office"}}
        """.utf8)

        let sent = try DeckCoding.decoder.decode(SendResponse.self, from: json)

        XCTAssertEqual(sent.message.id, "9e6da0f8c369")
        XCTAssertFalse(sent.delivered, "queued is not lost — and must not be retried")
    }

    // MARK: errors — branch on reason, not status

    func testTheErrorEnvelopeIsReadByReason() {
        let body = Data(#"{"ok":false,"reason":"thread_is_read_only","detail":"..."}"#.utf8)

        XCTAssertEqual(DeckError(status: 409, body: body), .threadIsReadOnly)
    }

    func testAClosedDeckAndARejectedTokenAreDifferentErrorsWithDifferentAdvice() {
        let closed = DeckError(
            status: 503,
            body: Data(#"{"ok":false,"reason":"auth_not_configured","detail":"..."}"#.utf8)
        )
        let rejected = DeckError(
            status: 401,
            body: Data(#"{"ok":false,"reason":"unauthorized","detail":"..."}"#.utf8)
        )

        XCTAssertEqual(closed, .authNotConfigured)
        XCTAssertEqual(rejected, .unauthorized)
        XCTAssertEqual(
            closed.userFacingText,
            "This deck is closed until its own API token is set on the server. There is no token for you to enter yet."
        )
        XCTAssertEqual(
            rejected.userFacingText,
            "The deck rejected this token. Set a different one in Settings."
        )
        XCTAssertNotEqual(closed.userFacingText, rejected.userFacingText)
    }

    func testEveryDocumentedReasonHasItsOwnCase() {
        let cases: [(Int, String, DeckError)] = [
            (400, "bad_cursor", .badCursor),
            (400, "empty_text", .emptyText),
            (404, "unknown_agent", .unknownAgent),
            (404, "unknown_thread", .unknownThread),
            (404, "unknown_message", .unknownMessage),
            (409, "thread_is_read_only", .threadIsReadOnly),
            (409, "reports_to_is_not_a_setting", .reportsToIsNotASetting),
            (409, "not_a_desk", .notADesk),
            (500, "queue_failed", .queueFailed),
        ]
        for (status, reason, expected) in cases {
            let body = Data(#"{"ok":false,"reason":"\#(reason)","detail":"d"}"#.utf8)
            XCTAssertEqual(DeckError(status: status, body: body), expected, reason)
        }
    }

    func testAReadOnlyRefusalOffersTheAgentsOwnChatRatherThanAnErrorToast() {
        XCTAssertEqual(
            DeckError.threadIsReadOnly.userFacingText,
            "This is a transcript of two agents talking. Open the agent's own chat to say something."
        )
    }

    // MARK: the workspace the deck allocated

    /// §4: a desk hired by talking to it states no folder, and the deck gives
    /// it `~/.claude/agent-bus/workspaces/<name>/`. The panel is where he finds
    /// out where it lives — the chat is not.
    func testTheSettingsPayloadCarriesTheWorkspaceTheDeckAllocated() throws {
        // Outside this Mac's home folder on purpose: the path is asserted whole
        // here, and the home-relative shortening is its own test below.
        let json = Data("""
        {"name":"new-hire-7f3a1c","label":"Working it out…","charter":"",
         "cwd":"/Volumes/decks/agent-bus/workspaces/new-hire-7f3a1c",
         "engine":"claude","is_desk":true}
        """.utf8)

        let agent = try DeckCoding.decoder.decode(Agent.self, from: json)

        XCTAssertEqual(agent.workspace, "/Volumes/decks/agent-bus/workspaces/new-hire-7f3a1c")
        XCTAssertEqual(agent.workspaceLine,
                       "Working in /Volumes/decks/agent-bus/workspaces/new-hire-7f3a1c")
    }

    func testTheWorkspaceIsShownFromTheHomeFolderDownRatherThanInFull() {
        var agent = makeAgent("new-hire")
        agent.workspace = NSHomeDirectory() + "/.claude/agent-bus/workspaces/new-hire"

        XCTAssertEqual(agent.workspaceLine, "Working in ~/.claude/agent-bus/workspaces/new-hire")
    }

    /// A payload that does not say where a desk works must show nothing, not
    /// "Working in " — a half-sentence reads as a folder with no name.
    func testADeskWithNoStatedFolderSaysNothingAboutOne() throws {
        let agent = try DeckCoding.decoder.decode(
            Agent.self, from: Data(#"{"name":"chief","label":"Boss"}"#.utf8)
        )

        XCTAssertEqual(agent.workspace, "")
        XCTAssertNil(agent.workspaceLine)
    }

    // MARK: PATCH — send only what the panel owns

    func testThePatchBodyCarriesOnlyTheKeysTheSettingsPanelOwns() throws {
        var agent = makeAgent("chief", title: "Designer")
        agent.detail = "Own the words."
        agent.notificationsEnabled = false
        agent.section = "Personal"
        agent.isPinned = true

        let body = try AgentPatch(agent).encoded()
        let keys = Set(try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        ).keys)

        XCTAssertEqual(keys, ["label", "charter", "section", "notifications", "pinned", "avatar"])
    }

    func testThePatchBodyNeverCarriesTheOrgChart() throws {
        let body = try AgentPatch(makeAgent("chief")).encoded()
        let keys = Set(try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        ).keys)

        XCTAssertTrue(keys.isDisjoint(with: ["reports_to", "boss", "reports", "name"]),
                      "sending reports_to at all is a 409, and name is the identity")
    }

    // MARK: SSE frames — typed by a field, not by an event name

    func testTheHelloFrameIsAFullSnapshot() throws {
        let frame = SSEEvent(name: nil, data: """
        {"type": "hello", "ts": 1788222702.09,
         "agents": [{"name": "chief", "label": "Negotiator", "section": "Work",
           "avatar": null, "state": "WORKING", "unread": 0, "last_activity_at": null,
           "preview": "", "thread_id": "direct:chief", "pinned": false,
           "notifications": true, "desk": true, "project": "", "session_id": null,
           "boss": null, "reports": []}],
         "threads": []}
        """, id: nil)

        guard case .hello(let payload) = try XCTUnwrap(DeckEvent(sse: frame)) else {
            return XCTFail("expected a hello snapshot")
        }
        XCTAssertEqual(payload.agents.map(\.name), ["chief"])
    }

    func testAMessageFrameCarriesItsOwnReadOnlyFlagSoItCanBeRoutedAlone() throws {
        let frame = SSEEvent(name: nil, data: """
        {"type": "message", "ts": 1788222777.3, "thread_id": "direct:chief",
         "read_only": false,
         "message": {"id": "m1", "cursor": "001788222777300000-m1",
          "thread_id": "direct:chief", "author": "chief", "role": "agent",
          "ts": 1788222777.3, "text": "on it", "attachments": [], "via": "office"}}
        """, id: nil)

        guard case .message(let message, let readOnly) = try XCTUnwrap(DeckEvent(sse: frame)) else {
            return XCTFail("expected a message frame")
        }
        XCTAssertEqual(message.id, "m1")
        XCTAssertFalse(readOnly)
    }

    func testStateAndUnreadAndHeartbeatFramesDecode() throws {
        let state = SSEEvent(name: nil, data:
            #"{"type":"agent_state","ts":1.0,"name":"chief","state":"OFFLINE"}"#, id: nil)
        let unread = SSEEvent(name: nil, data:
            #"{"type":"unread","ts":1.0,"name":"chief","unread":3}"#, id: nil)
        let beat = SSEEvent(name: nil, data: #"{"type":"heartbeat","ts":1.0}"#, id: nil)

        XCTAssertEqual(DeckEvent(sse: state), .agentState(name: "chief", state: .offline, blocked: .unspecified))
        XCTAssertEqual(DeckEvent(sse: unread), .unread(agent: "chief", count: 3))
        XCTAssertEqual(DeckEvent(sse: beat), .heartbeat)
    }

    func testAnUnknownFrameTypeIsIgnoredRatherThanFatal() {
        let frame = SSEEvent(name: nil, data: #"{"type":"something_new","ts":1.0}"#, id: nil)

        XCTAssertNil(DeckEvent(sse: frame), "a newer deck must not break an older client")
    }
}
