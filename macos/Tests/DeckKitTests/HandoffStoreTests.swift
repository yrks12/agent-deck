import XCTest
@testable import DeckKit

/// **What `HandoffsModel` is for, and it is not "hold a list".**
///
/// It is the thing standing between a tap and a desk being told the wrong one
/// of two opposite instructions. `done` sends a desk back to re-check a step;
/// `skipped` tells it to abandon that path for good. So the rules that matter
/// here are about *when a step stops being answerable*, and every one of them
/// fails in the same direction if it is got wrong: a card that looks settled
/// while a desk is still stopped, or a card offered twice.
///
/// **The premise, said plainly, because it is half measured and half assumed.**
///
/// Measured, by reading `server/api.py::resolve_handoff` and
/// `server/handoff.py::waiting` on his box: the deck marks the row `done` or
/// `skipped` and drops it from `GET /v1/handoffs` on the same call. There is no
/// ~1 Hz collector lag here, unlike approvals, where `ApprovalsModel` documents
/// one.
///
/// Assumed, and this is the race the local bookkeeping actually exists for: the
/// app polls this route on a timer, so a request that left **before** he
/// answered can land **after** it, carrying the step as still waiting. That is
/// ordinary overlap rather than something observed on his machine — but the
/// cost of being wrong about it is a second answer sent to a settled question,
/// and the guard is four lines.
final class HandoffStoreTests: XCTestCase {

    /// Serves whatever page it is holding, and records what was resolved.
    /// `page` can be changed between calls, which is how a poll that was
    /// already in flight is simulated without any timing.
    actor Deck: DeckClient {
        var page = HandoffsPage(handoffs: [], unreadable: 0)
        var failWith: DeckError?
        private(set) var resolved: [(String, String)] = []

        init() {}

        func serve(_ page: HandoffsPage) { self.page = page }
        func fail(with error: DeckError?) { failWith = error }

        func handoffs() async throws -> HandoffsPage {
            if let failWith { throw failWith }
            return page
        }

        func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
            if let failWith { throw failWith }
            resolved.append((id, outcome.rawValue))
            return HandoffResolution(outcome: outcome.rawValue, resumed: true)
        }

        // Nothing else on the protocol is reachable from this model.
        func roster() async throws -> RosterPayload { .init(agents: [], threads: [], sectionOrder: []) }
        func threads() async throws -> [ThreadSummary] { [] }
        func agent(named name: String) async throws -> Agent { throw DeckError.unknownAgent }
        func updateAgent(_ agent: Agent) async throws -> Agent { agent }
        func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
            throw DeckError.unknownThread
        }
        func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
            throw DeckError.unknownThread
        }
        func markRead(agent: String, upTo: String?) async throws {}
        func createAgent(_ draft: AgentDraft) async throws -> Agent { throw DeckError.unknownAgent }
        func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
            throw DeckError.unknownAgent
        }
        func approvals() async throws -> ApprovalsPage { .init(approvals: [], unreadable: 0) }
        func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
            throw DeckError.unknownAsk
        }
        func routines() async throws -> [Routine] { [] }
        func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
            throw DeckError.unknownRoutine
        }
        func setRoutine(id: String, enabled: Bool) async throws -> Routine {
            throw DeckError.unknownRoutine
        }
        func deleteRoutine(id: String) async throws {}
        nonisolated func events() -> AsyncThrowingStream<DeckEvent, Error> {
            AsyncThrowingStream { $0.finish() }
        }
    }

    /// `GET /v1/handoffs` as the deck builds it, one entry per `(id, ts)`.
    private func page(_ entries: [(id: String, ts: Double)]) throws -> HandoffsPage {
        let rows = entries.map { entry in
            """
            {"id":"\(entry.id)","ts":\(entry.ts),"agent":"acme-product",
             "asked_by":"acme-product","desk_known":true,"kind":"2fa",
             "needs":"Read the code texted to your phone and type it in",
             "state":"The transfer is prepared and not sent.",
             "where":"admin.microsoft.com","evidence":"","status":"waiting",
             "options":[
               {"reply":"done","available":true,"summary":"the desk re-checks the step worked"},
               {"reply":"skipped","available":true,"summary":"the desk abandons that path"}]}
            """
        }
        return try DeckCoding.decoder.decode(
            HandoffsPage.self,
            from: Data("{\"handoffs\":[\(rows.joined(separator: ","))]}".utf8))
    }

    // MARK: the good signal first

    /// Nothing below means anything if the model never holds a step at all.
    func testTheStepsWaitingComeBackOldestFirst() async throws {
        let deck = Deck()
        await deck.serve(try page([(id: "late", ts: 200), (id: "early", ts: 100)]))
        let model = HandoffsModel(client: deck)

        await model.load()

        let order = await model.waiting().map(\.id)
        let clean = await model.problem
        XCTAssertEqual(
            order, ["early", "late"],
            "the oldest block is the one that has been costing the most, and "
            + "nothing here can be made cheaper by answering a newer one first")
        XCTAssertNil(clean, "a clean fetch is not a problem")
    }

    // MARK: what the local bookkeeping is for

    /// **A poll that was already in flight must not resurrect an answered
    /// step.** It is the same card, offering the same two opposite
    /// instructions, on a question that is settled — and the second tap tells
    /// a desk the deck already told something else.
    func testAStepHeAnsweredIsNotRaisedAgainByAPollThatWasAlreadyInFlight() async throws {
        let deck = Deck()
        let live = try page([(id: "h1", ts: 100), (id: "h2", ts: 200)])
        await deck.serve(live)
        let model = HandoffsModel(client: deck)
        await model.load()
        let live1 = await model.waiting()
        let step = try XCTUnwrap(live1.first)

        await model.resolve(step, as: .done)
        let afterTap = await model.waiting().map(\.id)
        XCTAssertEqual(afterTap, ["h2"], "it went when the deck accepted it")

        // The deck answers a request that left before the tap, still carrying
        // `h1` as waiting. This is the overlap the guard exists for.
        await deck.serve(live)
        await model.load()

        let afterOverlap = await model.waiting().map(\.id)
        let sent = await deck.resolved.map(\.0)
        XCTAssertEqual(
            afterOverlap, ["h2"],
            "a step he already answered came back as a live question. He is "
            + "offered \"done\" and \"skip\" again on work the desk has already "
            + "been told about, and one of those two is the wrong instruction.")
        XCTAssertEqual(sent, ["h1"], "and it was only ever sent once")
    }

    /// **A refusal the deck might recover from leaves the step answerable.** A
    /// queue that failed granted nothing and told no desk anything; clearing
    /// the card would read exactly like a tap that worked, on a desk that is
    /// still stopped.
    func testARefusalTheDeckMightRecoverFromLeavesTheStepAnswerable() async throws {
        let deck = Deck()
        await deck.serve(try page([(id: "h1", ts: 100)]))
        let model = HandoffsModel(client: deck)
        await model.load()
        let live1 = await model.waiting()
        let step = try XCTUnwrap(live1.first)
        await deck.fail(with: .queueFailed)

        await model.resolve(step, as: .skipped)

        let stillThere = await model.waiting().map(\.id)
        let stillAnswerable = await model.waiting().first?.options.map(\.outcome)
        let why = await model.problem
        XCTAssertEqual(
            stillThere, ["h1"],
            "nothing happened and the card vanished, which is the one thing "
            + "that must never follow a failed tap")
        XCTAssertEqual(
            stillAnswerable, [.done, .skipped],
            "and it is still answerable — both instructions, with their sentences")
        XCTAssertEqual(why, DeckError.queueFailed.userFacingText,
                       "and he is told why, in the deck's own words")
    }

    /// The other half of the same rule, and it is the opposite behaviour: a
    /// step the deck says is already settled genuinely cannot be answered, so
    /// it goes — while the sentence saying this tap changed nothing stays.
    func testAStepSomebodyElseSettledStopsBeingOfferedAndStillSaysSo() async throws {
        let deck = Deck()
        await deck.serve(try page([(id: "h1", ts: 100)]))
        let model = HandoffsModel(client: deck)
        await model.load()
        let live1 = await model.waiting()
        let step = try XCTUnwrap(live1.first)
        await deck.fail(with: .alreadyAnswered(detail: ""))

        await model.resolve(step, as: .done)

        let left = await model.waiting().isEmpty
        let said = await model.problem
        XCTAssertTrue(
            left,
            "it is settled and unanswerable, so offering the two verbs again "
            + "would be an invitation to tell a desk something twice")
        XCTAssertEqual(
            said, DeckError.alreadyAnswered(detail: "").userFacingText,
            "and the card leaving must not read as the tap having decided "
            + "something: neither of these is a success")
    }

    // MARK: a fetch that failed is not an empty board

    /// **The one that would be silent.** The tray draws nothing when nothing is
    /// waiting — by design. So a fetch that failed and left the list empty is
    /// pixel-for-pixel identical to a deck where every desk is running fine.
    func testAFetchThatFailedIsSaidInWordsRatherThanLookingLikeAQuietDeck() async throws {
        let deck = Deck()
        await deck.fail(with: .transport("Could not connect to the server."))
        let model = HandoffsModel(client: deck)

        await model.load()

        let spoken = await model.problem
        let nothingInvented = await model.waiting().isEmpty
        XCTAssertEqual(
            spoken,
            DeckError.transport("Could not connect to the server.").userFacingText,
            "the app could not find out whether anything is stuck and said "
            + "nothing about it, which draws as \"nothing is stuck\"")
        XCTAssertTrue(nothingInvented, "and it invents nothing to fill the gap")
    }

    /// A later good fetch clears the sentence. A problem that outlived its
    /// cause would have him chasing a deck that came back minutes ago.
    func testAGoodFetchAfterABadOneStopsSayingSomethingIsWrong() async throws {
        let deck = Deck()
        await deck.fail(with: .transport("gone"))
        let model = HandoffsModel(client: deck)
        await model.load()
        let complained = await model.problem
        XCTAssertNotNil(complained)

        await deck.fail(with: nil)
        await deck.serve(try page([(id: "h1", ts: 100)]))
        await model.load()

        let quiet = await model.problem
        let held = await model.waiting().map(\.id)
        XCTAssertNil(quiet, "the deck answered; there is nothing left to warn about")
        XCTAssertEqual(held, ["h1"])
    }
}
