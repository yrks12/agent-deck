import XCTest
@testable import DeckKit
@testable import DeckUI

/// **He must be able to see the work, not just the answer.**
///
/// His words, 2026-09-06, holding this app next to xAI's Grok Bot:
/// *"why i dont see anything … to understand work is under the hood?"*
///
/// What the other product shows him, from his own screenshot: a card per tool
/// call **inline in the conversation**, each with a title, a status pill
/// (`Always allowed`, `Done`) and an expandable "Show the details"; the agent's
/// traffic with other agents, inline; and one sentence saying which rule got
/// auto-approved. What ours showed him was a single line reading
/// `Idle — Its session is up and nothing is running.`
///
/// Everything asserted here is the **good signal** — the card is on screen,
/// with its status, in the right place in the conversation. Nothing here
/// asserts the absence of something bad.
///
/// The class this sweeps: every activity kind the transcript can carry (a
/// message, a dispatch to another desk, a reply from one, a tool call), and
/// every state one card can be in (waiting, allowed once, always allowed,
/// denied, settled somewhere else).
@MainActor
final class WorkIsVisibleTests: XCTestCase {

    // MARK: DETECTOR — a tool call is a card in the conversation, in time order

    /// The card was drawn in a block pinned under the **whole** transcript, so
    /// a question asked at 09:00 sat below a message sent at 17:00 and read as
    /// the newest thing on screen. A tool call happens *at a time*; it belongs
    /// where that time is, the way every other line does.
    func testAToolCallIsDrawnWhereItHappened_NotPinnedUnderEverything() async throws {
        let store = try await loadedStore(
            saying: [("m1", "start on the PR", 0), ("m2", "pushed and merged", 900)],
            asking: [("z47x8", 300)]
        )

        XCTAssertEqual(
            store.timeline.map(\.kind),
            ["said", "tool call", "said"],
            "the tool call was drawn under the whole conversation instead of at "
            + "the moment it was asked, so a question from five minutes in reads "
            + "as the newest thing on screen: \(store.timeline.map(\.kind))")
        XCTAssertEqual(
            store.timeline.map(\.spoken),
            ["start on the PR",
             "Bash — gh pr create --title 'ship the routes'",
             "pushed and merged"],
            "what the conversation says, top to bottom")
    }

    /// Two desks talking to each other is most of what a working agent does
    /// (§6.2, and the deck's own preview line says so). It is a third kind of
    /// activity and it has to be tellable apart from the desk answering him.
    func testTheThreeKindsOfActivityAreTellableApartInOneList() async throws {
        let store = try await loadedStore(
            saying: [("m1", "any updates?", 0)],
            relaying: [("m2", "Status now: any new page_views since 307?", 100, "chief"),
                       ("m3", "page_views new: 0  replies: 0  max: 307", 200, "hemingway")],
            asking: [("z47x8", 300)]
        )

        XCTAssertEqual(
            store.timeline.map(\.kind),
            // Capitalised, because §6.2's label is a desk's *name* and never a
            // wire id — `RelayLine` resolves it through the roster.
            ["said", "to Hemingway", "from Hemingway", "tool call"],
            "he cannot tell his own conversation from the desk's traffic with "
            + "another desk from a tool call: \(store.timeline.map(\.kind))")
    }

    // MARK: DETECTOR — every card says what state it is in

    /// A pill on every card, always, because "waiting" and "done" are the two
    /// facts he is reading the screen for.
    func testAWaitingToolCallSaysItIsWaitingOnHim() async throws {
        let store = try await loadedStore(saying: [("m1", "go", 0)], asking: [("z47x8", 100)])

        let card = try XCTUnwrap(store.approvals.first)
        XCTAssertEqual(card.statusPill, "Waiting on you")
        XCTAssertTrue(card.isAnswerable, "a live question has to keep its buttons")
    }

    /// **The card must not simply vanish when he answers it.**
    ///
    /// It did, and that is the single worst version of "I cannot see the work":
    /// he taps Approve, the card disappears, and the screen is exactly as
    /// empty as it was before — with no way to tell whether anything was
    /// granted, what was granted, or whether the desk carried on.
    ///
    /// §12 sends all three facts back on the `POST` and this client threw the
    /// whole response away: `_ = try await raw(request)`.
    func testAnAnsweredToolCallStaysOnScreenSayingWhatWasGranted() async throws {
        let client = LiveDeckClient.withChief(saying: "go")
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")
        client.decision = ApprovalDecision(
            answered: "always",
            rule: try LiveDeckClient.alwaysAllowRule(),
            resumed: true
        )
        let store = DeckStore(client: client, approvalPollInterval: 0.02)
        await store.loadRoster()
        await settle(until: { !store.approvals.isEmpty }, "the card appears")

        let card = try XCTUnwrap(store.approvals.first)
        let always = try XCTUnwrap(card.options.first { $0.reply == .always })
        await store.answer(card: card, with: always)

        let after = try XCTUnwrap(
            store.approvals.first { $0.approvalID == "z47x8" },
            "the card vanished on the tap, so nothing on screen says a standing "
            + "permission was just written on his Mac")
        XCTAssertEqual(after.statusPill, "Always allowed")
        XCTAssertFalse(after.isAnswerable, "a settled question must not still offer to grant")
        XCTAssertEqual(
            after.outcome,
            "Allowed `gh pr*` for Bash in ~/Projects/acme from now on. Chief was "
            + "told to carry on with it.",
            "the rule that was written, and whether the desk actually moved — "
            + "§12's `resumed`, which the reference product shows as one sentence")
    }

    /// The sweep over the answers. Three replies, three different outcomes, and
    /// `resumed` is a real fourth fact: `never` grants nothing and restarts
    /// nobody, and a card that read the same for a denial as for a grant would
    /// be worse than no card.
    func testEveryWayAToolCallCanBeSettledReadsDifferently() throws {
        let rule = try LiveDeckClient.alwaysAllowRule()
        let deny = try LiveDeckClient.denyRule()
        let cases: [(ApprovalDecision, String, String)] = [
            (ApprovalDecision(answered: "once", rule: nil, resumed: true),
             "Allowed once",
             "Allowed this one time; no rule was written. Chief was told to carry on with it."),
            (ApprovalDecision(answered: "always", rule: rule, resumed: true),
             "Always allowed",
             "Allowed `gh pr*` for Bash in ~/Projects/acme from now on. Chief was told to carry on with it."),
            (ApprovalDecision(answered: "never", rule: deny, resumed: false),
             "Denied",
             "Refused `gh pr*` for Bash in ~/Projects/acme from now on. Nothing was restarted."),
        ]

        var pills: Set<String> = []
        var sentences: Set<String> = []
        for (decision, pill, sentence) in cases {
            let card = try card(settledBy: decision)
            XCTAssertEqual(card.statusPill, pill)
            XCTAssertEqual(card.outcome, sentence)
            XCTAssertFalse(card.isAnswerable)
            pills.insert(card.statusPill)
            sentences.insert(card.outcome ?? "")
        }
        XCTAssertEqual(pills.count, 3, "two answers read the same: \(pills)")
        XCTAssertEqual(sentences.count, 3, "two outcomes read the same: \(sentences)")
    }

    /// §12: `409 already_answered` and `409 expired` are refusals, not
    /// successes — "a reply that lands on a settled question must not look like
    /// it decided something". The card has to stay and say who decided it.
    func testAQuestionSettledInTheAgentsOwnTerminalSaysSoRatherThanLookingAnswered() async throws {
        let client = LiveDeckClient.withChief(saying: "go")
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")
        client.decideError = DeckError(
            status: 409,
            body: Data(#"{"ok":false,"reason":"already_answered","detail":"answered with 'always'"}"#.utf8)
        )
        let store = DeckStore(client: client, approvalPollInterval: 0.02)
        await store.loadRoster()
        await settle(until: { !store.approvals.isEmpty }, "the card appears")

        let card = try XCTUnwrap(store.approvals.first)
        await store.answer(card: card, with: card.options[0])

        let after = try XCTUnwrap(
            store.approvals.first { $0.approvalID == "z47x8" },
            "the card went and the only thing left was a red line at the bottom "
            + "of the conversation, so it read exactly like a tap that worked")
        XCTAssertEqual(after.statusPill, "Settled elsewhere")
        XCTAssertFalse(after.isAnswerable)
        XCTAssertEqual(
            after.outcome?.contains("Your tap changed nothing"), true,
            "the sentence has to say that this tap decided nothing: \(after.outcome ?? "nil")")
    }

    /// A settled card must not be resurrected as a live question by the next
    /// poll, and a live one must not be frozen by a stale settlement. The two
    /// lists are joined on the ask id, once.
    func testASettledCardIsNotRaisedAgainByTheNextPoll() async throws {
        let client = LiveDeckClient.withChief(saying: "go")
        client.approvalsPage = try LiveDeckClient.oneApproval(from: "chief")
        client.decision = ApprovalDecision(answered: "once", rule: nil, resumed: true)
        let store = DeckStore(client: client, approvalPollInterval: 0.02)
        await store.loadRoster()
        await settle(until: { !store.approvals.isEmpty }, "the card appears")

        let card = try XCTUnwrap(store.approvals.first)
        await store.answer(card: card, with: card.options[0])
        // The deck has not swept it yet — it is still on /v1/approvals as
        // `pending`, which is exactly what a 1Hz collector looks like.
        let polls = client.approvalFetches
        await settle(until: { client.approvalFetches > polls + 2 }, "two more polls")

        XCTAssertEqual(store.approvals.map(\.statusPill), ["Allowed once"],
                       "the answered card flipped back to a live question and "
                       + "offered to grant the permission a second time")
    }

    // MARK: helpers

    private func card(settledBy decision: ApprovalDecision) throws -> ApprovalCard {
        let page = try LiveDeckClient.oneApproval(from: "chief")
        let approval = try XCTUnwrap(page.approvals.first)
        return ApprovalCard.make(
            approval: approval,
            agents: ["chief": makeAgent("chief", title: "Negotiator")],
            decision: decision
        )
    }

    /// A store with a conversation, some relayed traffic and some tool calls,
    /// all at stated times, already open and settled.
    private func loadedStore(
        saying said: [(String, String, TimeInterval)],
        relaying relayed: [(String, String, TimeInterval, String)] = [],
        asking asks: [(String, TimeInterval)]
    ) async throws -> DeckStore {
        let client = LiveDeckClient.withChief(saying: "")
        var messages = said.map { id, text, offset -> Message in
            var message = LiveDeckClient.reply(id, text, at: offset)
            message.author = DeckOwner.name
            message.role = .owner
            return message
        }
        messages += relayed.map { id, text, offset, author in
            var message = LiveDeckClient.reply(id, text, at: offset)
            message.threadID = "peer:chief|hemingway"
            message.author = author
            return message
        }
        client.page = MessagePage(
            threadID: "direct:chief",
            messages: messages.sorted { $0.cursor < $1.cursor },
            isReadOnly: false,
            participants: ["owner", "chief"]
        )
        client.approvalsPage = try LiveDeckClient.approvals(
            asks.map { ($0.0, "chief", 1_788_222_700 + $0.1) }
        )
        let store = DeckStore(client: client, approvalPollInterval: 0.02)
        await store.loadRoster()
        await settle(until: { !store.approvals.isEmpty && !store.timeline.isEmpty },
                     "the conversation is open with its cards")
        return store
    }

    private func settle(
        until condition: () -> Bool,
        _ what: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out waiting for: \(what)", file: file, line: line)
    }
}

@MainActor
extension DeckStore {
    /// The conversation as one ordered list, the way it is drawn.
    var timeline: [ThreadEntry] {
        guard case .loaded(let screen) = thread else { return [] }
        return screen.entries
    }
}

extension ThreadEntry {
    /// What kind of activity this is, for an assertion that reads like the
    /// screen: "said", "to hemingway", "from hemingway", "tool call".
    var kind: String {
        switch self {
        case .said(let row): return row.attribution.label ?? "said"
        case .toolCall: return "tool call"
        }
    }

    /// The one line each entry contributes, so a test can pin the whole
    /// conversation top to bottom in one assertion.
    var spoken: String {
        switch self {
        case .said(let row): return row.message.text
        case .toolCall(let card): return "\(card.tool) — \(card.title)"
        }
    }
}
