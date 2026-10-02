import XCTest
@testable import DeckKit

/// "on the iphone we need [calls]": the same live call the Mac has, on a
/// phone — which adds an audio session the OS owns and interrupts (a real
/// phone call, Siri), a speaker/earpiece choice, and a call screen that
/// minimises to a pill while he reads anything else. The phone's rules live
/// in `PhoneCallCenter` so they are tested here, on the Mac, with fakes.
@MainActor
final class PhoneCallTests: XCTestCase {
    private var log: VoiceLog!
    private var calls: FakeCalls!
    private var transport: FakeRealtimeTransport!
    private var audio: FakeCallAudio!
    private var audioSession: FakeAudioSession!
    private var feeds: [String: AsyncStream<[Message]>.Continuation] = [:]
    private var resumed = 0
    private var center: PhoneCallCenter!

    override func setUp() async throws {
        log = VoiceLog()
        calls = FakeCalls(log: log)
        calls.realtime = RealtimeOffer(wsURL: URL(string: "wss://api.openai.com/v1/realtime")!, clientSecret: "ek_test",
                                       expiresAt: Date().addingTimeInterval(60))
        transport = FakeRealtimeTransport()
        audio = FakeCallAudio()
        audioSession = FakeAudioSession()
        let transport = self.transport!, audio = self.audio!
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { [calls] in calls }, realtime: { dial in
            RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                callID: dial.callID, threadID: dial.threadID,
                                transport: transport, audio: audio, calls: dial.calls)
        })
        center = PhoneCallCenter(session: session, audioSession: audioSession, working: { _ in false },
                                 feed: { [unowned self] threadID in
            AsyncStream { continuation in
                self.feeds[threadID] = continuation
                continuation.yield(self.history(threadID))
            }
        })
        center.resumeAudio = { [unowned self] in self.resumed += 1 }
    }

    private func history(_ threadID: String) -> [Message] {
        let desk = ThreadID.desk(ofDirect: threadID) ?? threadID
        return [message("old-1", from: desk, "Yesterday's news.")]
    }

    private func message(_ id: String, from desk: String, _ text: String) -> Message {
        Message(id: id, cursor: id, threadID: "direct:\(desk)", author: desk, role: .agent, sentAt: Date(), text: text)
    }

    private func settle() async {
        for _ in 0..<30 { await Task.yield() }
    }

    // MARK: - The audio session, chosen per platform

    func testTheMacConfiguresNoAudioSession() {
        XCTAssertNil(CallAudioSessionPlan.forCall(on: .mac, speaker: true),
                     "the Mac has no AVAudioSession: its call audio stays exactly as it was")
    }

    func testThePhoneTalksOnAVoiceChatSessionSoItsOwnVoiceIsCancelled() throws {
        let plan = try XCTUnwrap(CallAudioSessionPlan.forCall(on: .phone, speaker: true))
        XCTAssertEqual(plan.category, .playAndRecord)
        XCTAssertEqual(plan.mode, .voiceChat, "voiceChat turns on the phone's echo canceller")
        XCTAssertTrue(plan.options.contains(.allowBluetooth), "AirPods carry the call, mic and all")
        XCTAssertTrue(plan.options.contains(.defaultToSpeaker))
        XCTAssertEqual(plan.output, .speaker)
    }

    func testEarpieceDropsTheSpeakerDefault() throws {
        let plan = try XCTUnwrap(CallAudioSessionPlan.forCall(on: .phone, speaker: false))
        XCTAssertFalse(plan.options.contains(.defaultToSpeaker), "with it set, the earpiece is unreachable")
        XCTAssertEqual(plan.output, .receiver)
        XCTAssertEqual(plan.mode, .voiceChat)
    }

    func testThisBuildPicksItsOwnPlatform() {
        #if os(iOS)
        XCTAssertEqual(CallPlatform.current, .phone)
        #else
        XCTAssertEqual(CallPlatform.current, .mac)
        #endif
    }

    // MARK: - Dialling

    func testCallingActivatesTheSessionBeforeTheMicOpens() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        XCTAssertEqual(audioSession.activations.first?.mode, .voiceChat)
        XCTAssertTrue(audio.started)
        XCTAssertEqual(audioSession.log.prefix(1), ["activate"], "the mic opens on a configured session")
        XCTAssertNotNil(center.session.realtime)
        XCTAssertTrue(center.isCallScreenShown, "a new call opens the full-screen call view")
    }

    func testHistoryIsNotReadOutButNewLinesAreRelayed() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        let before = transport.sent.count
        feeds["direct:atlas"]?.yield(history("direct:atlas"))
        await settle()
        XCTAssertEqual(transport.sent.count, before, "yesterday's lines are not news on the call")
        feeds["direct:atlas"]?.yield(history("direct:atlas") + [message("new-1", from: "atlas", "Tests are green.")])
        await settle()
        XCTAssertTrue(transport.sent.contains { "\($0)".contains("Tests are green.") },
                      "the desk's progress reaches the voice while he talks")
    }

    func testAFailedDialReleasesTheAudioSession() async {
        calls.failStart = true
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        XCTAssertNil(center.session.call)
        XCTAssertEqual(audioSession.log.last, "deactivate", "other apps get their audio back")
        XCTAssertNotNil(center.notice, "and he is told why")
        XCTAssertFalse(center.isCallScreenShown)
    }

    func testHangingUpReleasesTheAudioSessionAndTheScreen() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.hangUp()
        await settle()
        XCTAssertNil(center.session.call)
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertEqual(audioSession.log.last, "deactivate")
        XCTAssertFalse(center.isCallScreenShown)
    }

    func testTheSpeakerToggleReappliesTheSession() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        center.toggleSpeaker()
        XCTAssertFalse(center.speakerOn)
        XCTAssertEqual(audioSession.activations.last?.output, .receiver)
        center.toggleSpeaker()
        XCTAssertEqual(audioSession.activations.last?.output, .speaker)
    }

    // MARK: - One call at a time

    func testCallingTheSameDeskAgainJustShowsTheCall() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        center.isCallScreenShown = false
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        XCTAssertEqual(calls.started, ["atlas"], "no second call")
        XCTAssertTrue(center.isCallScreenShown)
        XCTAssertNil(center.pendingSwitch)
    }

    func testCallingAnotherDeskAsksFirst() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.call(desk: "acme", name: "Acme", voice: nil)
        XCTAssertEqual(center.pendingSwitch?.question, "End call with Atlas and call Acme?")
        XCTAssertEqual(calls.started, ["atlas"], "nothing happens until he says yes")
        XCTAssertEqual(center.session.call?.desk, "atlas")
    }

    func testNoKeepsTheCallHeIsOn() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.call(desk: "acme", name: "Acme", voice: nil)
        center.cancelSwitch()
        XCTAssertNil(center.pendingSwitch)
        XCTAssertEqual(center.session.call?.desk, "atlas")
        XCTAssertTrue(calls.ended.isEmpty)
    }

    func testYesEndsOneAndCallsTheOther() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.call(desk: "acme", name: "Acme", voice: nil)
        await center.confirmSwitch()
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertEqual(calls.started, ["atlas", "acme"])
        XCTAssertEqual(center.session.call?.desk, "acme")
        XCTAssertEqual(center.session.call?.name, "Acme")
    }

    func testTheDecisionIsPure() {
        let open = VoiceSession.Call(id: "c", threadID: "direct:atlas", desk: "atlas", startedAt: Date(),
                                     handsFree: true, displayName: "Atlas")
        XCTAssertEqual(PhoneCallDecision.decide(current: nil, desk: "atlas", name: "Atlas"), .dial)
        XCTAssertEqual(PhoneCallDecision.decide(current: open, desk: "atlas", name: "Atlas"), .showCall)
        XCTAssertEqual(PhoneCallDecision.decide(current: open, desk: "acme", name: "Acme"),
                       .askFirst(VoiceSession.CallSwitch(from: "Atlas", to: "Acme")))
    }

    // MARK: - The pill follows him

    func testThePillShowsWheneverTheCallScreenIsDown() async {
        XCTAssertNil(center.pill, "no call, no pill")
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        XCTAssertNil(center.pill, "the full-screen call is up")
        center.isCallScreenShown = false
        XCTAssertEqual(center.pill?.desk, "atlas", "minimised: the pill carries the call")
        XCTAssertEqual(center.pill?.name, "Atlas")
    }

    func testNavigatingNeverTouchesTheCall() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        center.isCallScreenShown = false
        // He opens Acme's thread, then goes back to the roster: on the phone the
        // screens never sync the voice session, the call's own feed does.
        feeds["direct:atlas"]?.yield(history("direct:atlas"))
        await settle()
        XCTAssertEqual(center.session.call?.desk, "atlas")
        XCTAssertEqual(center.pill?.desk, "atlas")
        XCTAssertFalse(transport.closed)
        XCTAssertTrue(audio.started && !audio.stopped)
    }

    func testMuteFromThePillReachesTheLiveVoice() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        center.isCallScreenShown = false
        center.toggleMute()
        XCTAssertEqual(center.pill?.isMuted, true)
        XCTAssertEqual(center.session.realtime?.isMuted, true)
    }

    // MARK: - Interruptions: a phone call, Siri, the audio restarting

    func testAPhoneCallHoldsTheCallAndItResumesAfter() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        let t0 = Date()
        await center.handle(.began, at: t0)
        XCTAssertTrue(center.isOnHold)
        XCTAssertNotNil(center.session.call, "a phone call does not end the agent's call")
        await center.handle(.ended(shouldResume: true), at: t0.addingTimeInterval(20))
        XCTAssertFalse(center.isOnHold)
        XCTAssertEqual(resumed, 1, "the engine is restarted")
        XCTAssertEqual(audioSession.log.filter { $0 == "activate" }.count, 2, "and the session reactivated")
        XCTAssertNotNil(center.session.call)
    }

    func testAHoldLongerThanTheLimitEndsTheCall() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        let t0 = Date()
        await center.handle(.began, at: t0)
        await center.handle(.ended(shouldResume: true), at: t0.addingTimeInterval(CallInterruptionPolicy.maxHold + 1))
        XCTAssertNil(center.session.call)
        XCTAssertEqual(calls.ended, ["call_1"])
        XCTAssertNotNil(center.notice)
    }

    func testAnotherAppKeepingTheAudioEndsTheCall() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.handle(.began, at: Date())
        await center.handle(.ended(shouldResume: false), at: Date())
        XCTAssertNil(center.session.call)
        XCTAssertEqual(resumed, 0)
    }

    func testTheAudioRestartingEndsTheCallCleanly() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.handle(.mediaServicesReset, at: Date())
        XCTAssertNil(center.session.call)
        XCTAssertEqual(audioSession.log.last, "deactivate")
    }

    func testUnpluggingHeadphonesKeepsTheCall() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        await center.handle(.routeLost, at: Date())
        XCTAssertNotNil(center.session.call)
        XCTAssertFalse(center.isOnHold)
    }

    func testInterruptionsWithNoCallAreIgnored() async {
        await center.handle(.began, at: Date())
        XCTAssertFalse(center.isOnHold)
        XCTAssertTrue(audioSession.log.isEmpty)
    }

    func testTheCallDroppingOnItsOwnReleasesEverything() async {
        await center.call(desk: "atlas", name: "Atlas", voice: nil)
        transport.drop()
        await settle()
        XCTAssertNil(center.session.call)
        XCTAssertNotNil(center.notice, "he is told why the line went quiet")
        XCTAssertEqual(audioSession.log.last, "deactivate")
        XCTAssertFalse(center.isCallScreenShown)
    }
}

final class FakeAudioSession: CallAudioSessionControl {
    private(set) var log: [String] = []
    private(set) var activations: [CallAudioSessionPlan] = []

    func activate(_ plan: CallAudioSessionPlan) throws {
        log.append("activate")
        activations.append(plan)
    }

    func deactivate() { log.append("deactivate") }
}
