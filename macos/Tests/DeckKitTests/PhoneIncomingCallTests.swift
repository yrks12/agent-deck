import XCTest
@testable import DeckKit

/// **A desk calls him, on the phone.** The phone has no CallKit and no push
/// (free Apple team): it sees a ring by asking the deck often enough while it
/// is in front, and by a tap on the ntfy push or the local notification. The
/// decisions the phone makes about that are pure and tested here.
@MainActor
final class PhoneIncomingCallTests: XCTestCase {
    private let at = { (t: Double) in Date(timeIntervalSince1970: t) }

    private func ring(_ id: String = "ring_abc", agent: String = "atlas", expires: Double = 130) -> IncomingRing {
        IncomingRing(id: id, agent: agent, reason: "prod db is down", urgent: true,
                     createdAt: expires - 30, expiresAt: expires)
    }

    private func tap(_ ring: String?, agent: String = "atlas") -> AlertRoute {
        AlertRoute(threadID: "direct:\(agent)", agent: agent, cardID: nil, ringID: ring)
    }

    // MARK: how often the phone asks

    func testInFrontThePhoneAsksOftenEnoughToSeeA30SecondRing() {
        XCTAssertLessThanOrEqual(OwnerAlertCadence.interval(active: true), 3,
                                 "a 30 s ring must be seen within a few seconds while he is in the app")
        XCTAssertEqual(OwnerAlertCadence.interval(active: false), 12,
                       "kept alive behind the lock screen (a call): the old pace")
    }

    // MARK: a tap on the ring's notification or ntfy link

    func testATapOnARingStillRingingOpensTheIncomingCall() {
        var incoming = IncomingCallState()
        let opened = incoming.open(tap("ring_abc"), ringing: [ring()], now: at(110))
        XCTAssertEqual(opened, .ringing(ring()))
        XCTAssertEqual(incoming.showing?.id, "ring_abc")
    }

    func testATapAfterTheRingRanOutOpensTheThreadWithAOneLineNotice() {
        var incoming = IncomingCallState()
        XCTAssertEqual(incoming.open(tap("ring_abc"), ringing: [], now: at(140)),
                       .missed(threadID: "direct:atlas", agent: "atlas",
                               notice: "Missed — Atlas's reason is in the thread"))
        XCTAssertEqual(incoming.open(tap("ring_abc"), ringing: [ring()], now: at(131)),
                       .missed(threadID: "direct:atlas", agent: "atlas",
                               notice: "Missed — Atlas's reason is in the thread"),
                       "a page that still lists it after 30 s is not ringing")
        XCTAssertNil(incoming.showing)
    }

    func testADeckOutOfReachSaysSoInsteadOfMissed() {
        var incoming = IncomingCallState()
        guard case .missed(let thread, _, let notice)? = incoming.open(tap("ring_abc"), ringing: nil, now: at(110)) else {
            return XCTFail("no page: open the thread")
        }
        XCTAssertEqual(thread, "direct:atlas")
        XCTAssertEqual(notice, "Couldn't reach the deck — Atlas's reason will be in the thread")
    }

    func testAnOrdinaryTapIsNotACall() {
        var incoming = IncomingCallState()
        XCTAssertNil(incoming.open(tap(nil), ringing: [ring()], now: at(110)),
                     "no ring id: the old path opens the thread")
    }

    // MARK: which ring the screen shows

    func testTheTappedRingIsShownOverAnOlderOne() {
        var incoming = IncomingCallState()
        let older = ring("ring_old", agent: "acme", expires: 125)
        let tapped = ring("ring_abc")
        XCTAssertEqual(incoming.update([older, tapped], now: at(110))?.id, "ring_old", "no tap: the oldest")
        _ = incoming.open(tap("ring_abc"), ringing: [older, tapped], now: at(110))
        XCTAssertEqual(incoming.update([older, tapped], now: at(111))?.id, "ring_abc")
        XCTAssertEqual(incoming.update([older], now: at(112))?.id, "ring_old",
                       "the tapped one left the feed: back to what is ringing")
    }

    func testAnsweredOrDeclinedHereItNeverRingsAgainAndItGoesWhenItRunsOut() {
        var incoming = IncomingCallState()
        XCTAssertEqual(incoming.update([ring()], now: at(110))?.id, "ring_abc")
        XCTAssertNil(incoming.update([ring()], now: at(130)), "ran out")
        _ = incoming.open(tap("ring_abc"), ringing: [ring()], now: at(111))
        incoming.handle("ring_abc")
        XCTAssertNil(incoming.showing)
        XCTAssertNil(incoming.update([ring()], now: at(112)), "a late page does not ring it again")
    }

    // MARK: Answer on the phone is the phone's own call screen

    private func center(_ calls: AnsweringCalls) -> PhoneCallCenter {
        let log = VoiceLog()
        let transport = FakeRealtimeTransport(), audio = FakeCallAudio()
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { calls }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls)
        })
        return PhoneCallCenter(session: session, audioSession: FakeAudioSession(), working: { _ in false },
                               feed: { _ in AsyncStream { $0.yield([]) } })
    }

    func testAnswerOpensTheCallScreenByAnsweringTheRingNotByDialling() async {
        let calls = AnsweringCalls()
        let phone = center(calls)
        await phone.answer(ring: ring(), name: "Atlas", voice: nil)
        XCTAssertEqual(calls.answered, ["ring_abc"])
        XCTAssertEqual(calls.started, [], "answering is not a second, outgoing call")
        XCTAssertTrue(phone.isCallScreenShown, "the same full-screen call as one he placed")
        XCTAssertEqual(phone.session.call?.desk, "atlas")
        XCTAssertEqual(phone.session.call?.name, "Atlas")
    }

    func testAnsweringWhileOnAnotherCallEndsThatCallFirst() async {
        let calls = AnsweringCalls()
        let phone = center(calls)
        await phone.call(desk: "acme", name: "Acme", voice: nil)
        await phone.call(desk: "bravo", name: "Bravo", voice: nil)
        XCTAssertNotNil(phone.pendingSwitch, "the ring came in while he was asked to switch")
        await phone.answer(ring: ring(), name: "Atlas", voice: nil)
        XCTAssertEqual(calls.ended, ["call_acme"])
        XCTAssertEqual(calls.answered, ["ring_abc"])
        XCTAssertEqual(phone.session.call?.desk, "atlas")
        XCTAssertNil(phone.pendingSwitch, "he already chose by tapping Answer")
    }
}

/// A deck that can be called and can call; no live voice, so calls stay simple.
private final class AnsweringCalls: CallClient, IncomingCallClient, @unchecked Sendable {
    private(set) var answered: [String] = []
    private(set) var started: [String] = []
    private(set) var ended: [String] = []

    func startCall(agent: String) async throws -> CallStart {
        started.append(agent)
        return CallStart(callID: "call_\(agent)", threadID: "direct:\(agent)")
    }

    func answerRing(id: String) async throws -> CallStart {
        answered.append(id)
        return CallStart(callID: "call_9", threadID: "direct:atlas", ringID: id, opening: "prod db is down")
    }

    func declineRing(id: String) async throws {}
    func callSettings() async throws -> CallOwnerSettings { .deckDefault }
    func updateCallSettings(_ changes: [String: CallSettingValue]) async throws -> CallOwnerSettings { .deckDefault }
    func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        Message(id: "m", cursor: "", threadID: threadID, author: DeckOwner.name, role: .owner,
                sentAt: Date(), text: text, channel: .voice)
    }
    func endCall(id: String) async throws { ended.append(id) }
    func postTranscript(callID: String, lines: [CallLine]) async throws {}
}
