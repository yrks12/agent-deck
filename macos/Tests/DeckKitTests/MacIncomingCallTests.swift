import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **A desk calls him, on the Mac.** The ringing feed on the alerts page the
/// Mac already polls becomes one incoming-call card over the window; Answer
/// opens the SAME live call as the header's Call, on the calling desk; Decline
/// hands the reason to his thread; the card goes by itself when the ring does.
@MainActor
final class MacIncomingCallTests: XCTestCase {

    private func ring(_ id: String = "ring_abc", expires: Double = 130) -> IncomingRing {
        IncomingRing(id: id, agent: "atlas", reason: "prod db is down", urgent: true,
                     createdAt: expires - 30, expiresAt: expires)
    }

    private func at(_ t: Double) -> Date { Date(timeIntervalSince1970: t) }

    private func store(_ deck: RingDeck, history: [Message] = []) -> DeckStore {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief"), makeAgent("atlas")],
            threads: [makeThread("direct:chief", agent: "chief", at: 100), makeThread("direct:atlas", agent: "atlas")],
            sectionOrder: ["Work"])
        client.pages = [
            MessagePage(threadID: "direct:chief", messages: [], isReadOnly: false, participants: ["owner", "chief"]),
            MessagePage(threadID: "direct:atlas", messages: history, isReadOnly: false,
                        participants: ["owner", "atlas"]),
        ]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        return DeckStore(client: client, approvalPollInterval: 600, incomingCalls: deck)
    }

    // MARK: the card follows the feed

    func testARingInThePolledPageIsPublishedAndGoesWhenThePageDropsItOrItRunsOut() {
        let store = store(RingDeck())
        store.takeRinging([ring()], now: at(101))
        XCTAssertEqual(store.incomingRing, ring(), "the page says Atlas is ringing: the card is up")
        store.takeRinging([], now: at(102))
        XCTAssertNil(store.incomingRing, "answered or declined on the phone: the card goes")
        store.takeRinging([ring()], now: at(103))
        XCTAssertNotNil(store.incomingRing)
        store.takeRinging([ring()], now: at(130))
        XCTAssertNil(store.incomingRing, "thirty seconds and it stops ringing, even if a page is late")
    }

    // MARK: Answer

    func testAnswerAnswersTheRingOnceAndOpensTheLiveCallOnTheCallingDesk() async throws {
        let deck = RingDeck()
        let history = [makeMessage("old1", text: "old news", thread: "direct:atlas", author: "atlas")]
        let store = store(deck, history: history)
        await store.loadRoster()
        await store.settle()
        XCTAssertEqual(store.selectedThreadID, "direct:chief", "he is reading another desk")

        let log = VoiceLog()
        let transport = FakeRealtimeTransport()
        let audio = FakeCallAudio()
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { deck }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls)
        })
        session.sync(desk: "chief", displayName: "Chief", voice: nil, working: false, messages: [])

        let now = Date().timeIntervalSince1970
        let calling = ring(expires: now + 30)
        store.takeRinging([calling], now: Date())
        await store.answer(calling, on: session)
        for _ in 0..<20 { await Task.yield() }

        XCTAssertEqual(deck.answered, ["ring_abc"], "answered exactly once, through the call path")
        XCTAssertEqual(deck.started, [], "not a second, outgoing call")
        XCTAssertEqual(deck.declined, [])
        XCTAssertEqual(session.call?.desk, "atlas", "the call is with the desk that rang")
        XCTAssertNotNil(session.realtime)
        XCTAssertEqual(store.selectedThreadID, "direct:atlas", "its thread is open beside the call")
        XCTAssertNil(store.incomingRing)
        store.takeRinging([calling], now: Date())
        XCTAssertNil(store.incomingRing, "a late page does not ring it again")

        // The thread feed hands the session the same transcript again, as the
        // window does: what was already there is history, not news to read out.
        session.sync(desk: "atlas", displayName: "atlas", voice: nil, working: false, messages: history)
        for _ in 0..<20 { await Task.yield() }
        let said = transport.sent.compactMap { event -> String? in
            guard let item = event["item"] as? [String: Any],
                  let content = item["content"] as? [[String: Any]] else { return nil }
            return content.first?["text"] as? String
        }
        XCTAssertEqual(said.first, "[Atlas update] prod db is down", "the reason is the call's first line")
        XCTAssertFalse(said.contains { $0.contains("old news") }, "the thread's history is not replayed: \(said)")
    }

    // MARK: Decline

    func testDeclineDeclinesTheRingAndTheCardGoes() async {
        let deck = RingDeck()
        let store = store(deck)
        store.takeRinging([ring()], now: at(101))
        await store.decline(ring())
        XCTAssertEqual(deck.declined, ["ring_abc"])
        XCTAssertEqual(deck.answered, [])
        XCTAssertNil(store.incomingRing)
        XCTAssertNil(store.incomingCallProblem)
        store.takeRinging([ring()], now: at(102))
        XCTAssertNil(store.incomingRing, "a late page does not ring it again")
    }

    func testARefusedDeclineSaysWhyInOneLineAndLeavesTheCardUp() async {
        let deck = RingDeck()
        deck.declineError = DeckError.transport("the deck is restarting")
        let store = store(deck)
        store.takeRinging([ring()], now: at(101))
        await store.decline(ring())
        XCTAssertEqual(store.incomingRing?.id, "ring_abc", "he can still answer it")
        let problem = store.incomingCallProblem
        XCTAssertNotNil(problem, "the refusal is said, not swallowed")
        XCTAssertFalse(problem?.contains("\n") ?? true, "one line")
    }

    // MARK: the card as drawn

    func testTheCardSaysWhoIsCallingWhyAndOffersAnswerAndDecline() {
        let view = IncomingCallView(ring: ring(expires: Date().timeIntervalSince1970 + 30), agent: nil,
                                    problem: nil, answer: {}, decline: {})
        let (host, window) = RightPaneFixture.host(view, size: CGSize(width: 480, height: 420))
        defer { window.orderOut(nil); window.contentView = nil }
        let words = RightPaneFixture.render(host).map { RightPaneFixture.readText($0).map(\.text) }?
            .joined(separator: " | ") ?? ""
        for expected in ["Atlas is calling", "prod db is down", "Answer", "Decline"] {
            XCTAssertTrue(words.contains(expected), "\(expected) missing from: \(words)")
        }
    }

}

/// A deck that can ring him: records what was answered, declined and patched.
final class RingDeck: CallClient, IncomingCallClient, @unchecked Sendable {
    private(set) var answered: [String] = []
    private(set) var declined: [String] = []
    private(set) var started: [String] = []
    private(set) var patches: [[String: CallSettingValue]] = []
    var declineError: Error?
    var updateError: Error?
    private let offer = RealtimeOffer(wsURL: URL(string: "wss://api.openai.com/v1/realtime")!,
                                      clientSecret: "ek_test", expiresAt: Date().addingTimeInterval(60))

    func startCall(agent: String) async throws -> CallStart {
        started.append(agent)
        return CallStart(callID: "call_1", threadID: "direct:\(agent)")
    }

    func answerRing(id: String) async throws -> CallStart {
        answered.append(id)
        return CallStart(callID: "call_9", threadID: "direct:atlas", realtime: offer,
                         ringID: id, opening: "prod db is down")
    }

    func declineRing(id: String) async throws {
        if let declineError { throw declineError }
        declined.append(id)
    }

    func callSettings() async throws -> CallOwnerSettings { .deckDefault }

    func updateCallSettings(_ changes: [String: CallSettingValue]) async throws -> CallOwnerSettings {
        if let updateError { throw updateError }
        patches.append(changes)
        var out = CallOwnerSettings.deckDefault
        if case .text(let when)? = changes["when"] { out.when = .init(rawValue: when) ?? out.when }
        out.maxPerDay = 5
        return out
    }

    func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        Message(id: "m", cursor: "", threadID: threadID, author: DeckOwner.name, role: .owner,
                sentAt: Date(), text: text, channel: .voice)
    }
    func endCall(id: String) async throws {}
    func postTranscript(callID: String, lines: [CallLine]) async throws {}
}
