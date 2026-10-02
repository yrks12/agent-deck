import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **K4 in the thread: a decision is a card with buttons, and it remembers
/// what happened to it.**
///
/// Grok: "Umbrella waitlist copy?" [Approve as-is] [I'll send edits] [Hold
/// copy], answered in one tap; typing something else instead leaves the card
/// marked skipped. Ours had prose questions only.
@MainActor
final class DecisionCardsTests: XCTestCase {

    private func asked(_ id: String = "q1", state: Decision.State = .open, ts: TimeInterval = 10) -> Message {
        var message = makeMessage(id, text: "Headline?", thread: "direct:atlas", author: "atlas", ts: ts)
        message.kind = .decision
        message.decision = Decision(
            id: "dec_\(id)", prompt: "Headline?",
            options: [DecisionOption(label: "A", value: "Go with A", style: .primary),
                      DecisionOption(label: "B", value: "Go with B")],
            state: state)
        return message
    }

    // MARK: the rules, as values

    /// He typed instead of tapping: the open card is skipped (Grok's
    /// `widgetSkipped`), so it stops offering buttons nobody will read.
    func testALaterOwnerMessageSkipsAnOpenDecision() {
        let settled = ThreadTimeline.settleDecisions(
            [asked(), makeMessage("y1", text: "actually, neither", thread: "direct:atlas",
                                  author: DeckOwner.name, role: .owner, ts: 11)],
            overrides: [:])
        XCTAssertEqual(settled.first?.decision?.state, .skipped)
    }

    func testARoutineAfterTheCardDoesNotSkipIt() {
        let settled = ThreadTimeline.settleDecisions(
            [asked(), makeMessage("r1", text: "Morning", thread: "direct:atlas",
                                  author: "routine", role: .system, ts: 11)],
            overrides: [:])
        XCTAssertEqual(settled.first?.decision?.state, .open)
    }

    /// The deck's newer word (an SSE frame, or his own tap) wins over the copy
    /// the page carried; an answer is never turned back into skipped.
    func testTheNewestStateWinsAndAnAnswerStaysAnswered() {
        let answered = Decision(id: "dec_q1", prompt: "Headline?", options: [], state: .answered,
                                answer: "Go with B")
        let settled = ThreadTimeline.settleDecisions(
            [asked(), makeMessage("y1", text: "Go with B", thread: "direct:atlas",
                                  author: DeckOwner.name, role: .owner, ts: 11)],
            overrides: ["dec_q1": answered])
        XCTAssertEqual(settled.first?.decision?.state, .answered)
        XCTAssertEqual(settled.first?.decision?.answer, "Go with B")
        XCTAssertEqual(settled.first?.decision?.options.count, 2,
                       "a frame's state must not wipe the options the card was drawn with")
    }

    // MARK: the store

    /// A frame about the open thread flips the card without a refetch.
    func testADecisionFrameUpdatesTheOpenCard() async throws {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("atlas", title: "COS")],
            threads: [makeThread("direct:atlas", agent: "atlas")], sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:atlas", messages: [asked()],
                                    isReadOnly: false, participants: [DeckOwner.name, "atlas"])]
        var frame = try XCTUnwrap(asked().decision)
        frame.state = .answered
        frame.answer = "Go with A"
        client.feeds = [.emitThenFinish([.decision(threadID: "direct:atlas", decision: frame)])]

        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()

        guard case .loaded(let screen) = store.thread else { return XCTFail("thread never opened") }
        XCTAssertEqual(screen.messages.first?.decision?.state, .answered)
        XCTAssertEqual(screen.messages.first?.decision?.answer, "Go with A")
    }

    /// Tapping an option answers it on the deck and the card says so at once.
    func testTappingAnOptionAnswersTheCard() async throws {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        let card = try await openCard(in: store)

        await store.answer(decision: card, with: "Go with A")

        guard case .loaded(let screen) = store.thread else { return XCTFail("thread closed") }
        let after = screen.messages.first { $0.decision?.id == card.id }?.decision
        XCTAssertEqual(after?.state, .answered)
        XCTAssertEqual(after?.answer, "Go with A")
        XCTAssertNil(store.decisionProblem)
    }

    private func openCard(in store: DeckStore) async throws -> Decision {
        let began = Date()
        while Date().timeIntervalSince(began) < 5 {
            if case .loaded(let screen) = store.thread,
               let open = screen.messages.compactMap(\.decision).first(where: { $0.state == .open }) {
                return open
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("the fixture's chief thread carries no open decision")
        throw NoCard()
    }

    private struct NoCard: Error {}

    // MARK: the card, as words and as width

    /// Open: nothing under the buttons. Answered: what he chose, by its label.
    /// Skipped: says so — and, per §6.5, still offers the buttons, because a
    /// skipped card can still be answered.
    func testTheCardSaysWhatHappenedToIt() throws {
        var decision = try XCTUnwrap(asked().decision)
        XCTAssertNil(decision.statusLine)
        XCTAssertTrue(decision.offersChoices)

        decision.state = .answered
        decision.answer = "Go with B"
        XCTAssertEqual(decision.statusLine, "You chose: B")
        XCTAssertFalse(decision.offersChoices, "an answered card still offers buttons")

        decision.answer = "Neither, try C"
        XCTAssertEqual(decision.statusLine, "You chose: Neither, try C", "his own words, as he typed them")

        decision.state = .skipped
        decision.answer = nil
        XCTAssertEqual(decision.statusLine, "Skipped — you answered in the chat")
        XCTAssertTrue(decision.offersChoices, "a skipped card can still be answered (§6.5)")
    }

    /// Four long options in the narrowest column still fit it.
    func testACardWithFourLongOptionsFitsTheNarrowestColumn() throws {
        var decision = try XCTUnwrap(asked().decision)
        decision.options = (1...4).map {
            DecisionOption(label: String(repeating: "Option \($0) ", count: 5), value: "v\($0)")
        }
        decision.help = String(repeating: "Full draft is in ~/Projects/acme/copy/headlines.md ", count: 3)
        let asked = NSHostingController(rootView: DecisionCardView(decision: decision, answer: { _ in })
            .padding(.horizontal, TranscriptMetrics.horizontalPadding))
            .sizeThatFits(in: CGSize(width: 380, height: 2000))
        XCTAssertLessThanOrEqual(asked.width, 380.5, "the card needs \(asked.width)pt in a 380pt column")
    }
}
