import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The other half of "why is this desk stopped", and the half no permission
/// can fix.**
///
/// An approval is a question the desk could answer itself with a yes. A secure
/// handoff is the case where the desk **cannot act at all**: a 2FA code
/// arriving on a phone, a CAPTCHA, an SMS confirmation, `gh auth login`,
/// signing into Google. There is nothing to grant. A human has to physically
/// do it, and until one does the desk is stopped.
///
/// `GET /v1/handoffs` is live on his deck and shaped like `/v1/approvals` on
/// purpose — the same `agent` / `asked_by` / `desk_known` join, options
/// carrying their own sentences — so **one tray draws both and filters both on
/// the same field**. That is what is asserted here.
///
/// The two verbs are not two words for one thing, and this file pins that:
/// `done` tells the desk to go back and *re-check that the step worked*, and
/// `skipped` tells it to *abandon that path for good* and report what it can no
/// longer finish. Sending one where the other belongs turns a finished job into
/// an abandoned one, or a skipped login into a screen hammered until something
/// locks. `taken_over` is deliberately not offered: it leaves the work
/// unfinished and the card on the board.
@MainActor
final class SecureHandoffTests: XCTestCase {

    // MARK: a deck that has both kinds of stopped on it

    /// Serves `/v1/approvals` and `/v1/handoffs` from the same payloads the
    /// live deck sends, and records what was answered on which route.
    final class BlockedDeck: DeckClient, @unchecked Sendable {
        private let lock = NSLock()
        private let payload: RosterPayload
        var approvalsPage = ApprovalsPage(approvals: [], unreadable: 0)
        var handoffsPage = HandoffsPage(handoffs: [], unreadable: 0)
        var resolveError: DeckError?
        private var _resolved: [(String, String)] = []
        private var _decided: [(String, String)] = []

        /// `(id, outcome)` for every `POST /v1/handoffs/{id}`.
        var resolved: [(String, String)] { lock.withLock { _resolved } }
        /// `(id, reply)` for every `POST /v1/approvals/{id}` — so a handoff
        /// answered down the permission route would show up here and nowhere
        /// else.
        var decided: [(String, String)] { lock.withLock { _decided } }

        init(agents: [Agent]) { payload = AgentsResponse.roster(from: agents) }

        func roster() async throws -> RosterPayload { payload }
        func threads() async throws -> [ThreadSummary] { payload.threads }
        func agent(named name: String) async throws -> Agent {
            guard let found = payload.agents.first(where: { $0.name == name }) else {
                throw DeckError.unknownAgent
            }
            return found
        }
        func updateAgent(_ agent: Agent) async throws -> Agent { agent }
        func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
            MessagePage(threadID: threadID, messages: [], isReadOnly: false,
                        participants: ["owner", ThreadID.desk(ofDirect: threadID) ?? ""])
        }
        func send(threadID: String, text: String) async throws -> Message {
            throw DeckError.unknownThread
        }
        func markRead(agent: String, upTo: String?) async throws {}
        func createAgent(_ draft: AgentDraft) async throws -> Agent { throw DeckError.unknownAgent }
        func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
            throw DeckError.unknownAgent
        }
        func approvals() async throws -> ApprovalsPage { lock.withLock { approvalsPage } }
        func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
            lock.withLock { _decided.append((id, option.reply.rawValue)) }
            return ApprovalDecision(answered: option.reply.rawValue, rule: nil, resumed: true)
        }
        func handoffs() async throws -> HandoffsPage { lock.withLock { handoffsPage } }
        func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
            if let error = lock.withLock({ resolveError }) { throw error }
            lock.withLock { _resolved.append((id, outcome.rawValue)) }
            return HandoffResolution(outcome: outcome.rawValue, resumed: true)
        }
        func routines() async throws -> [Routine] { [] }
        func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
            throw DeckError.unknownRoutine
        }
        func setRoutine(id: String, enabled: Bool) async throws -> Routine {
            throw DeckError.unknownRoutine
        }
        func deleteRoutine(id: String) async throws {}
        func events() -> AsyncThrowingStream<DeckEvent, Error> {
            AsyncThrowingStream { $0.yield(.heartbeat) }
        }
    }

    // MARK: fixtures — the deck's own payloads, field for field

    /// `GET /v1/handoffs`, exactly as `server/api.py::_handoff_row` builds it,
    /// including the two summaries it puts on the buttons verbatim.
    private func handoffPage(
        id: String = "h1", agent: String = "northwind-product",
        askedBy: String = "bfffdc59-5f34-4b6b-b304-300c80cb3c25",
        deskKnown: Bool = true, ts: Double = 1_756_000_100
    ) throws -> HandoffsPage {
        try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"\(id)","ts":\(ts),"agent":"\(agent)",
          "asked_by":"\(askedBy)","desk_known":\(deskKnown),"kind":"login",
          "needs":"Sign in to Microsoft 365 admin (initech.example), then hand back",
          "state":"The tenant is created and nothing has been billed yet.",
          "where":"admin.microsoft.com","evidence":"waiting on the sign-in page",
          "status":"waiting","options":[
            {"reply":"done","available":true,
             "summary":"I'm done, continue — the desk goes back and checks the step actually worked before carrying on"},
            {"reply":"skipped","available":true,
             "summary":"Skip this step — the desk abandons that path for good and reports what it can no longer finish"}]}]}
        """.utf8))
    }

    /// `GET /v1/approvals`, so the two can be seen sharing a tray.
    private func approvalPage(id: String = "a1", agent: String = "atlas",
                              ts: Double = 1_756_000_000) throws -> ApprovalsPage {
        try DeckCoding.decoder.decode(ApprovalsPage.self, from: Data("""
        {"approvals":[{"id":"\(id)","ts":\(ts),"agent":"\(agent)","asked_by":"\(agent)",
          "desk_known":true,"tool":"Bash","subject":"gh pr list",
          "cwd":"/Users/y/Projects/northwind","cwd_short":"~/Projects/northwind","status":"pending",
          "options":[{"reply":"once","available":true,"rule":null,"summary":"just this time"}]}]}
        """.utf8))
    }

    private func loaded(_ deck: BlockedDeck) async -> DeckStore {
        let store = DeckStore(client: deck, approvalPollInterval: 600)
        await store.loadRoster()
        await store.loadApprovals()
        return store
    }

    // MARK: 1 — it reaches the tray at all

    /// **A desk that cannot act reaches him on the same strip as one that can.**
    func testAStepOnlyAHumanCanTakeIsInTheTrayWithWhatItNeedsAndWhereToGo() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas"), makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        let store = await loaded(deck)

        XCTAssertEqual(
            store.attention.map(\.askID), ["h1"],
            "a desk is stopped on something no permission can lift and there is "
            + "nothing on screen about it. It reached him on WhatsApp or not at all.")

        let item = try XCTUnwrap(store.attention.first)
        XCTAssertTrue(item.isHandoff, "this is not a permission he can grant from his chair")
        XCTAssertEqual(item.deskLine, "Northwind-product", "the card has to say which desk")
        XCTAssertEqual(
            item.request,
            "Sign in to Microsoft 365 admin (initech.example), then hand back",
            "the instruction is the deck's own sentence, written for a phone")
        XCTAssertEqual(
            item.situation, "The tenant is created and nothing has been billed yet.",
            "the state of the work is what decides whether he opens a laptop now "
            + "or after dinner. Without it the card says \"I need you\" and nothing "
            + "about whether money is already being spent.")
    }

    /// The two buttons, and the two different instructions behind them.
    func testTheTwoAnswersSayWhatEachOneTellsTheDeskToDo() async throws {
        let deck = BlockedDeck(agents: [makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        let store = await loaded(deck)
        let item = try XCTUnwrap(store.attention.first)

        XCTAssertEqual(item.actions.map(\.label), ["I'm done, continue", "Skip this step"])
        XCTAssertEqual(
            item.actions.map(\.verb),
            [.handoff(.done), .handoff(.skipped)],
            "`taken_over` must not be on offer: it leaves the work unfinished and "
            + "the card on the board, so it is a button that gets tapped twice")
        XCTAssertTrue(
            item.actions.allSatisfy { !$0.summary.isEmpty },
            "a button with no sentence on it is one he taps to find out what it did")

        let done = try XCTUnwrap(item.actions.first { $0.verb == .handoff(.done) })
        XCTAssertTrue(done.summary.contains("checks the step actually worked"),
                      "\"done\" is a claim from a human; the desk has to re-check it: "
                      + done.summary)
        let skipped = try XCTUnwrap(item.actions.first { $0.verb == .handoff(.skipped) })
        XCTAssertTrue(skipped.summary.contains("abandons that path"),
                      "read as retry-with-a-pause, a skip hammers a login screen "
                      + "until something locks: " + skipped.summary)
    }

    // MARK: 2 — one tray, one filter, both kinds

    /// **One strip, both kinds, oldest first** — because to him they are the
    /// same event: something of his is not moving until he acts.
    func testAPermissionAndAHandoffShareOneTrayInTheOrderTheyStopped() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas"), makeAgent("northwind-product")])
        deck.approvalsPage = try approvalPage(ts: 1_756_000_000)
        deck.handoffsPage = try handoffPage(ts: 1_756_000_100)
        let store = await loaded(deck)

        XCTAssertEqual(
            store.attention.map(\.askID), ["a1", "h1"],
            "both routes have something waiting and the tray drew \(store.attention.count). "
            + "Whichever it dropped is a desk stopped with nothing on screen.")
        XCTAssertEqual(store.attention.map(\.isHandoff), [false, true],
                       "and they are still tellable apart")
    }

    /// **The same field decides both.** `desk_known` is the deck's own answer to
    /// "could I resolve this to a desk"; a handoff it could not place must still
    /// reach him, carrying the raw id it was filed under.
    func testAHandoffTheDeckCouldNotAttributeStillReachesHimWithWhatItDidKnow() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas")])
        deck.handoffsPage = try handoffPage(
            agent: "64fec5a7-ff1a-48f4-9214-f2dc178c87fc",
            askedBy: "64fec5a7-ff1a-48f4-9214-f2dc178c87fc", deskKnown: false)
        let store = await loaded(deck)

        let item = try XCTUnwrap(store.attention.first)
        XCTAssertFalse(item.isAttributed, "no desk on this roster answers to that id")
        XCTAssertNotEqual(
            item.deskLine, "64fec5a7-ff1a-48f4-9214-f2dc178c87fc",
            "a session id dressed up as a desk name is a worse lie than saying "
            + "the deck did not name one")
        XCTAssertTrue(item.evidence.contains("64fec5a7"),
                      "the raw id is the only handle he has on it: " + item.evidence)
        XCTAssertEqual(item.request,
                       "Sign in to Microsoft 365 admin (initech.example), then hand back",
                       "and it still says what has to be done")
    }

    /// The join is the deck's, not the roster's. A desk the deck resolved but
    /// this roster has not caught up with is still a named desk.
    func testADeskTheDeckResolvedIsNamedEvenBeforeTheRosterHasIt() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas")])
        deck.handoffsPage = try handoffPage(agent: "listing-closer", deskKnown: true)
        let store = await loaded(deck)

        let item = try XCTUnwrap(store.attention.first)
        XCTAssertTrue(item.isAttributed,
                      "the deck said it resolved this one and the app second-guessed it")
        XCTAssertEqual(item.deskLine, "Listing-closer")
    }

    // MARK: 3 — answering it, down its own route

    func testAnsweringAHandoffGoesToTheHandoffRouteAndNotThePermissionOne() async throws {
        let deck = BlockedDeck(agents: [makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        let store = await loaded(deck)
        let item = try XCTUnwrap(store.attention.first)
        let skip = try XCTUnwrap(item.actions.first { $0.verb == .handoff(.skipped) })

        await store.answer(item: item, with: skip)

        XCTAssertEqual(deck.resolved.map(\.0), ["h1"], "it never reached the deck")
        XCTAssertEqual(
            deck.resolved.map(\.1), ["skipped"],
            "the verb he read on the button is the verb the desk is told, and "
            + "these two are different instructions rather than different words")
        XCTAssertTrue(
            deck.decided.isEmpty,
            "a handed-over step was sent to POST /v1/approvals, which grants a "
            + "permission nobody asked for and leaves the desk still stopped")
        XCTAssertTrue(store.attention.isEmpty, "and it stops waiting on him")
    }

    /// A refusal from the deck must leave it up. A tray that emptied on a tap
    /// the deck rejected reads exactly like one that worked — and the desk on
    /// the other end is still stopped.
    func testAnAnswerTheDeckRefusesLeavesTheStepWaitingAndSaysWhy() async throws {
        let deck = BlockedDeck(agents: [makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        deck.resolveError = .queueFailed
        let store = await loaded(deck)
        let item = try XCTUnwrap(store.attention.first)

        await store.answer(item: item, with: item.actions[0])

        XCTAssertEqual(store.attention.map(\.askID), ["h1"],
                       "nothing happened, so nothing may disappear")
        XCTAssertEqual(store.approvalProblem, DeckError.queueFailed.userFacingText)
    }

    /// A step too broken to draw is still a desk sitting stopped, so it is
    /// counted out loud rather than dropped.
    func testAStepTooBrokenToDrawIsStillCountedOutLoud() async throws {
        let deck = BlockedDeck(agents: [makeAgent("northwind-product")])
        deck.handoffsPage = try DeckCoding.decoder.decode(
            HandoffsPage.self,
            from: Data(#"""
            {"handoffs":[{"id":"h9","ts":1.0,"agent":"northwind-product","kind":"2fa",
              "state":"waiting","where":"x","status":"waiting","options":[]}]}
            """#.utf8))
        let store = await loaded(deck)

        XCTAssertTrue(store.attention.isEmpty, "there is nothing here he could answer")
        XCTAssertEqual(
            store.approvalNotice,
            "1 handed-over step could not be read and is not shown here. "
            + "The desk is still waiting on it.",
            "a step nobody can see is still a desk that cannot go on")
    }

    // MARK: 3b — the way to the place where he does it

    /// **The card tells him to go and sign in somewhere. It has to take him
    /// there.**
    ///
    /// A handed-over step is not answered from the strip — he has to type a
    /// code or sign into something, and the place that happens is the desk's
    /// own conversation and its screen. The desk is by definition one he is not
    /// looking at, so leaving him to find it in the sidebar gives most of the
    /// cost of the block straight back.
    func testAHandoffTakesHimToTheDeskWhereHeHasToDoIt() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas"), makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        let store = await loaded(deck)

        XCTAssertEqual(store.selectedAgentName, "atlas", "the case needs the other desk open")
        let item = try XCTUnwrap(store.attention.first)
        XCTAssertEqual(
            item.openDeskLabel, "Open Northwind-product",
            "the card says go and sign in and offers no way to get there")
        let name = try XCTUnwrap(item.deskName, "the wire name is what a thread id is keyed on")

        store.openDesk(named: name)

        XCTAssertEqual(store.selectedAgentName, "northwind-product",
                       "the tap did not move him to the desk that is stopped")
        XCTAssertEqual(store.selectedThreadID, "direct:northwind-product",
                       "and its conversation — which is where its screen is drawn")
    }

    /// A permission he answers from the strip itself, so there is nothing to
    /// open; and a desk nothing answers to has nowhere to go. Neither draws a
    /// control, because a button that leads nowhere costs him the click and
    /// teaches him not to trust the strip.
    func testNothingOffersToOpenADeskThatIsNotThereOrNotNeeded() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas")])
        deck.approvalsPage = try approvalPage()
        deck.handoffsPage = try handoffPage(
            agent: "64fec5a7-ff1a-48f4-9214-f2dc178c87fc",
            askedBy: "64fec5a7-ff1a-48f4-9214-f2dc178c87fc", deskKnown: false)
        let store = await loaded(deck)

        let permission = try XCTUnwrap(store.attention.first { !$0.isHandoff })
        XCTAssertNil(permission.openDeskLabel,
                     "he answers this one right here; there is nowhere to send him")
        let orphan = try XCTUnwrap(store.attention.first { $0.isHandoff })
        XCTAssertNil(orphan.openDeskLabel,
                     "no desk on this roster answers to that id, so there is no "
                     + "conversation to open — and it must still be in the tray")
        XCTAssertEqual(store.attention.count, 2, "both are still on screen")
    }

    /// And asking for a desk that is not on the roster leaves him where he is.
    /// Landing on an empty pane would be a worse answer than not moving.
    func testAskingForADeskThatIsNotOnTheRosterLeavesHimWhereHeWas() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas")])
        let store = await loaded(deck)

        store.openDesk(named: "a-desk-that-was-fired")

        XCTAssertEqual(store.selectedAgentName, "atlas")
    }

    // MARK: 4 — what it says out loud

    func testEveryHandoffActionNamesItsDeskAndWhatItWillTellIt() async throws {
        let deck = BlockedDeck(agents: [makeAgent("northwind-product")])
        deck.handoffsPage = try handoffPage()
        let store = await loaded(deck)
        let item = try XCTUnwrap(store.attention.first)

        let spoken = item.actions.map { item.spokenAction($0) }
        XCTAssertTrue(spoken.allSatisfy { $0.contains("Northwind-product") },
                      "with two desks waiting, an action that does not name its "
                      + "desk answers the wrong question: \(spoken)")
        XCTAssertTrue(spoken.contains { $0.contains("abandons that path") },
                      "the consequence is spoken before the tap: \(spoken)")
        XCTAssertTrue(
            item.spoken.contains("cannot go on without you"),
            "a handoff is not a permission and must not be announced as one: "
            + item.spoken)
        XCTAssertTrue(
            item.spoken.contains("nothing has been billed yet"),
            "the state of the work is read out too — it is the half that says "
            + "how urgent this is: " + item.spoken)
    }

    // MARK: 5 — the tray still costs nothing when it is empty, with both routes

    func testWithNeitherKindWaitingTheTrayIsStillZeroHigh() async throws {
        let deck = BlockedDeck(agents: [makeAgent("atlas")])
        let store = await loaded(deck)

        XCTAssertTrue(store.attention.isEmpty)
        let probe = NSHostingView(
            rootView: AttentionTrayView(items: store.attention, answer: { _, _ in }, openDesk: { _ in })
                .frame(width: 300))
        probe.layoutSubtreeIfNeeded()
        XCTAssertEqual(
            probe.fittingSize.height, 0,
            "an empty tray takes \(probe.fittingSize.height)pt at the top of the "
            + "inspector all day, which is how he learns to stop looking at the "
            + "one place an ask appears")
    }
}
