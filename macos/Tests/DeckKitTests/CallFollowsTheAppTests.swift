import XCTest
@testable import DeckKit

/// "when i move to other agent the call gets disconnected". A phone call
/// belongs to the app, not to the thread on screen: it keeps going while he
/// reads other desks, a pill says so from anywhere, and it ends only when he
/// hangs up, the app quits, or the line fails. One call at a time.
@MainActor
final class CallFollowsTheAppTests: XCTestCase {
    private var log: VoiceLog!
    private var calls: FakeCalls!
    private var input: FakeSpeechInput!
    private var transport: FakeRealtimeTransport!
    private var audio: FakeCallAudio!
    private var session: VoiceSession!

    override func setUp() async throws {
        log = VoiceLog()
        calls = FakeCalls(log: log)
        calls.realtime = RealtimeOffer(wsURL: URL(string: "wss://api.openai.com/v1/realtime")!, clientSecret: "ek_test",
                                       expiresAt: Date().addingTimeInterval(60))
        input = FakeSpeechInput(log: log)
        transport = FakeRealtimeTransport()
        audio = FakeCallAudio()
        let transport = self.transport!, audio = self.audio!
        session = VoiceSession(input: input, output: FakeSpeechOutput(log: log), calls: { [calls] in calls },
                               realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls)
        })
        open("atlas", "Atlas")
    }

    private func open(_ desk: String?, _ name: String, messages: [Message] = []) {
        session.sync(desk: desk, displayName: name, voice: nil, working: false, messages: messages)
    }

    private func settle() async {
        for _ in 0..<20 { await Task.yield() }
    }

    private func message(_ id: String, from desk: String, _ text: String) -> Message {
        Message(id: id, cursor: id, threadID: "direct:\(desk)", author: desk, role: .agent, sentAt: Date(), text: text)
    }

    func testSwitchingToAnotherDeskDoesNotEndTheCall() async {
        await session.startCall()
        let live = session.realtime
        open("acme", "Acme")
        await settle()
        XCTAssertNotNil(session.call, "the call belongs to the app, not to the thread on screen")
        XCTAssertTrue(session.realtime === live)
        XCTAssertTrue(calls.ended.isEmpty, "the deck is not told the call ended")
        XCTAssertFalse(transport.closed)
        XCTAssertTrue(audio.started && !audio.stopped)
    }

    func testThePillShowsWhileHeReadsAnotherDeskAndTheBarOnlyOnTheCalledOne() async {
        await session.startCall()
        XCTAssertNil(CallPill.make(session), "on the called desk the big call bar is enough")
        XCTAssertTrue(CallPill.showsBar(session))
        open("acme", "Acme")
        let pill = CallPill.make(session)
        XCTAssertEqual(pill?.desk, "atlas")
        XCTAssertEqual(pill?.name, "Atlas")
        XCTAssertFalse(CallPill.showsBar(session), "Acme's thread does not draw Atlas's call screen")
        open(nil, "")
        XCTAssertEqual(CallPill.make(session)?.desk, "atlas", "no thread open: the pill still says he is on a call")
        open("atlas", "Atlas")
        XCTAssertNil(CallPill.make(session))
        XCTAssertTrue(CallPill.showsBar(session))
    }

    func testHangingUpFromThePillEndsTheCall() async {
        await session.startCall()
        open("acme", "Acme")
        await session.hangUp()
        XCTAssertNil(session.call)
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertNil(CallPill.make(session))
    }

    func testAnotherDesksNewsIsNotRelayedIntoTheCall() async {
        await session.startCall()
        open("acme", "Acme", messages: [message("p1", from: "acme", "old")])
        open("acme", "Acme", messages: [message("p1", from: "acme", "old"), message("p2", from: "acme", "Acme news")])
        await settle()
        let relayed = transport.sent.filter { $0["type"] as? String == "conversation.item.create" }
        XCTAssertTrue(relayed.isEmpty, "Acme's thread is not Atlas's news")
    }

    func testCallingAnotherDeskAsksFirstAndDoesNotDial() async {
        await session.startCall()
        open("acme", "Acme")
        await session.startCall()
        XCTAssertEqual(session.pendingSwitch, VoiceSession.CallSwitch(from: "Atlas", to: "Acme"))
        XCTAssertEqual(calls.started, ["atlas"], "one call at a time: nothing dialled yet")
        XCTAssertNotNil(session.call)
        XCTAssertEqual(VoiceSession.CallSwitch(from: "Atlas", to: "Acme").question, "End call with Atlas and call Acme?")
    }

    func testConfirmingTheSwitchEndsTheOldCallAndCallsTheNewDesk() async {
        await session.startCall()
        open("acme", "Acme")
        await session.startCall()
        await session.confirmSwitch()
        XCTAssertNil(session.pendingSwitch)
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertEqual(calls.started, ["atlas", "acme"])
        XCTAssertEqual(session.call?.desk, "acme")
    }

    func testDecliningTheSwitchKeepsTheCall() async {
        await session.startCall()
        open("acme", "Acme")
        await session.startCall()
        session.cancelSwitch()
        XCTAssertNil(session.pendingSwitch)
        XCTAssertEqual(session.call?.desk, "atlas")
        XCTAssertTrue(calls.ended.isEmpty)
    }

    func testAHoldToTalkCallStillEndsWhenHeLeaves() async {
        calls.realtime = nil
        await session.pressMic()
        input.say("check the deploy")
        await session.releaseMic()
        XCTAssertNotNil(session.call)
        open("acme", "Acme")
        await settle()
        XCTAssertNil(session.call, "a quiet hold-to-talk call is the thread's, not a phone call")
    }
}
