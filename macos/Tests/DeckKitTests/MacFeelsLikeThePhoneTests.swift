import XCTest
@testable import DeckKit

/// **The rules both apps draw by, decided once in DeckKit.** The owner asked
/// for the Mac to feel exactly like the iPhone: the same status words under a
/// name, the same one-line pulse over the roster, and the same composer that
/// offers his voice when the box is empty and the send arrow when it is not.
/// These used to live in the iPhone target only, where the Mac could not
/// reach them and no test ran them.
final class MacFeelsLikeThePhoneTests: XCTestCase {

    private func row(_ name: String, _ state: AgentState, blocked: Bool = false) -> SidebarRow {
        var agent = Agent(name: name, title: "", state: state)
        if blocked { agent.blocked = Blocked(what: "Sign in", reason: "dialog_unrelayed") }
        return SidebarRow(agent: agent, threadID: "direct:\(name)", threads: [],
                          preview: ThreadPreview(text: "hi"), timestamp: nil, unreadCount: 0)
    }

    // MARK: the status line under a name

    func testTheRosterStatusLineSaysWhatNeedsHimAndWhatIsWorking() {
        XCTAssertEqual(RowAttention.waitingForYou.statusLine, StatusLine("Waiting for you", tone: .waiting))
        XCTAssertEqual(RowAttention.working.statusLine, StatusLine("Working", tone: .working))
        XCTAssertNil(RowAttention.quiet.statusLine, "a desk that is fine says nothing")
    }

    func testTheHeaderPutsWaitingFirstThenAStreamThatIsDownThenWork() {
        XCTAssertEqual(ThreadHeaderStatus.line(attention: .waitingForYou, connection: .idle, loaded: true,
                                               agentState: "Idle"),
                       StatusLine("Waiting for you", tone: .waiting))
        XCTAssertEqual(ThreadHeaderStatus.line(attention: .working, connection: .reconnecting(attempt: 2),
                                               loaded: true, agentState: "Working"),
                       StatusLine("Reconnecting", tone: .neutral))
        XCTAssertEqual(ThreadHeaderStatus.line(attention: .working, connection: .connecting, loaded: false,
                                               agentState: "Working"),
                       StatusLine("Working", tone: .working),
                       "a pane that is still opening does not say Connecting")
        XCTAssertEqual(ThreadHeaderStatus.line(attention: .quiet, connection: .live, loaded: true,
                                               agentState: "Idle"),
                       StatusLine("Idle", tone: .neutral))
    }

    // MARK: the composer's button

    func testAnEmptyBoxOffersHisVoiceAndWordsTurnItIntoSend() {
        XCTAssertEqual(ComposerMode.make(draft: "", isReadOnly: false, canRecord: true, isSending: false), .record)
        XCTAssertEqual(ComposerMode.make(draft: "  \n", isReadOnly: false, canRecord: true, isSending: false), .record,
                       "whitespace is not words")
        XCTAssertEqual(ComposerMode.make(draft: "ship it", isReadOnly: false, canRecord: true, isSending: false),
                       .send(enabled: true))
        XCTAssertEqual(ComposerMode.make(draft: "ship it", isReadOnly: false, canRecord: true, isSending: true),
                       .send(enabled: false), "no second send while the first is in flight")
    }

    func testWithNoVoiceTheBoxShowsADisabledSendAndAPeerThreadHasNoBox() {
        XCTAssertEqual(ComposerMode.make(draft: "", isReadOnly: false, canRecord: false, isSending: false),
                       .send(enabled: false))
        XCTAssertEqual(ComposerMode.make(draft: "hi", isReadOnly: true, canRecord: true, isSending: false),
                       .viewOnly)
    }

    // MARK: the live roster status line

    func testThePulseCountsEachDeskOnceAndLeadsWithTheWaitingFaces() {
        let rows = [row("a", .working), row("b", .needsYou), row("a", .working),
                    row("c", .idle), row("d", .idle, blocked: true), row("e", .working)]
        let pulse = RosterPulseSummary(rows: rows)
        XCTAssertEqual(pulse.working, 2)
        XCTAssertEqual(pulse.waiting, 2)
        XCTAssertEqual(pulse.line, "2 working · 2 waiting on you")
        XCTAssertEqual(pulse.faces.map(\.agent.name), ["b", "d", "a"])
    }

    func testAQuietDeckSaysSoAndShowsItsFirstFaces() {
        let pulse = RosterPulseSummary(rows: [row("x", .idle), row("y", .done), row("z", .idle), row("w", .idle)])
        XCTAssertEqual(pulse.line, "All quiet. Nothing is running right now.")
        XCTAssertEqual(pulse.faces.map(\.agent.name), ["x", "y", "z"])
        XCTAssertNil(pulse.workingText)
        XCTAssertEqual(RosterPulseSummary(rows: [row("x", .working)]).line, "1 working")
    }

    // MARK: the tokens are the phone's

    func testTheTokensAreThePhonesNumbers() {
        XCTAssertEqual(DeckTokens.canvas, DeckTokens.Grey(light: 1.0, dark: 0.0))
        XCTAssertEqual(DeckTokens.ownerBubble, DeckTokens.Grey(light: 0.87, dark: 0.23))
        XCTAssertEqual(DeckTokens.agentBubble, DeckTokens.Grey(light: 0.955, dark: 0.12))
        XCTAssertEqual(DeckTokens.card, DeckTokens.Grey(light: 0.965, dark: 0.10))
        XCTAssertEqual(DeckTokens.field, DeckTokens.Grey(light: 0.94, dark: 0.14))
        XCTAssertEqual(DeckTokens.bubbleRadius, 20)
    }
}
