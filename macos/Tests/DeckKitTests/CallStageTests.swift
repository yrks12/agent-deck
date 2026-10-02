import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **C4: "i dont have animations when im talking."** The call screen's
/// character listens to him, talks with the agent's voice and shows when the
/// desk is working — and stays a still picture when nobody is making a sound.
/// Captures go to `UITests/Artifacts/overhaul/c4-*.png` when `DECK_CAPTURE=1`.
@MainActor
final class CallStageTests: XCTestCase {
    private var capturing: Bool { ProcessInfo.processInfo.environment["DECK_CAPTURE"] == "1" }

    private var log: VoiceLog!
    private var calls: FakeCalls!
    private var input: FakeSpeechInput!
    private var transport: FakeRealtimeTransport!
    private var audio: FakeCallAudio!
    private var session: VoiceSession!

    override func setUp() async throws {
        log = VoiceLog()
        calls = FakeCalls(log: log)
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
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
    }

    private func liveCall() async -> RealtimeCallSession {
        calls.realtime = RealtimeOffer(wsURL: URL(string: "wss://api.openai.com/v1/realtime")!, clientSecret: "ek_x")
        await session.startCall()
        return session.realtime!
    }

    // MARK: the rules

    func testListeningFollowsHisVoice() async {
        let live = await liveCall()
        audio.onMicLevel?(0.7)
        let stage = CallStage.make(session)
        XCTAssertEqual(stage.mode, .listening)
        XCTAssertEqual(stage.level, 0.7)
        XCTAssertTrue(stage.isLive)
        _ = live
    }

    func testSpeakingFollowsTheAgentsVoice() async {
        let live = await liveCall()
        live.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.2]).base64EncodedString())"}"#)
        let stage = CallStage.make(session)
        XCTAssertEqual(stage.mode, .speaking)
        XCTAssertEqual(stage.level, 0.6)
    }

    func testTheDeskWorkingIsVisible() async {
        let live = await liveCall()
        live.handle(#"{"type":"input_audio_buffer.speech_stopped"}"#)
        live.handle(#"{"type":"response.function_call_arguments.done","call_id":"f","name":"send_to_desk","arguments":"{\"text\":\"go\"}"}"#)
        XCTAssertEqual(CallStage.make(session).mode, .working)
    }

    func testMutedIsStillAndSaysSo() async {
        _ = await liveCall()
        session.toggleMute()
        audio.onMicLevel?(0.9)
        let stage = CallStage.make(session)
        XCTAssertEqual(stage.mode, .muted)
        XCTAssertEqual(stage.level, 0)
    }

    func testTheOnMacSpeechCallAnimatesToo() async {
        await session.startCall()   // no offer: Apple speech
        input.onLevel?(0.5)
        XCTAssertEqual(CallStage.make(session).mode, .listening)
        XCTAssertEqual(CallStage.make(session).level, 0.5)
        XCTAssertFalse(CallStage.make(session).isLive)
    }

    /// Motion comes only from audio levels. The traps this app has paid for:
    /// a forever loop, a display-rate timeline, an implicit animation.
    func testNothingOnTheCallScreenAnimatesByItself() throws {
        for file in ["Sources/DeckUI/CallStageView.swift", "Sources/DeckUI/VoiceNoteViews.swift"] {
            let source = try VoiceMicFeedTests.source(file)
                .split(separator: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("///") }
                .joined(separator: "\n")
            for banned in ["repeatForever", "TimelineView", ".animation(", "withAnimation"] {
                XCTAssertFalse(source.contains(banned), "\(file) uses \(banned)")
            }
        }
    }

    // MARK: captures

    private func render<V: View>(_ view: V, size: CGSize) async throws -> NSBitmapImageRep {
        let (host, window) = RightPaneFixture.host(view.frame(width: size.width, height: size.height), size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: 500_000_000)
        return try XCTUnwrap(RightPaneFixture.render(host))
    }

    private func words(_ rep: NSBitmapImageRep) -> String {
        RightPaneFixture.readText(rep).map(\.text).joined(separator: " ")
    }

    private func face(_ mode: CallStage.Mode, _ level: Float, _ status: String, caption: String = "") -> some View {
        CallStageFace(stage: CallStage(mode: mode, level: level, status: status, isLive: true),
                      agent: nil, desk: "atlas", caption: caption)
            .background(Color(white: 0.12))
    }

    func testCaptureTheCharacterInEachState() async throws {
        let shots: [(String, CallStage.Mode, Float, String, String)] = [
            ("c4-call-listening-quiet.png", .listening, 0, "Listening…", ""),
            ("c4-call-hearing-him.png", .hearing, 0.75, "Hearing you…", ""),
            ("c4-call-agent-speaking.png", .speaking, 0.85, "Atlas is speaking",
             "The acme build failed on a missing env var — I've asked the desk to fix it."),
            ("c4-call-desk-working.png", .working, 0.2, "The desk is on it — keep talking", ""),
        ]
        var painted: [String: Int] = [:]
        var text = ""
        for (name, mode, level, status, caption) in shots {
            let rep = try await render(face(mode, level, status, caption: caption), size: CGSize(width: 440, height: 300))
            painted[name] = Self.paintedPixels(rep)
            text += " " + words(rep)
            if capturing { try RightPaneFixture.saveCapture(rep, named: name) }
        }
        XCTAssertTrue(text.contains("Atlas is speaking"), text)
        XCTAssertTrue(text.contains("desk is on it"), text)
        // The level really moves the picture: his voice at 0.75 reaches much
        // further out than the quiet line.
        let quiet = try XCTUnwrap(painted["c4-call-listening-quiet.png"])
        let loud = try XCTUnwrap(painted["c4-call-hearing-him.png"])
        XCTAssertGreaterThan(Double(loud), Double(quiet) * 1.2, "quiet \(quiet) vs loud \(loud)")
    }

    /// Pixels that differ from the background corner.
    private static func paintedPixels(_ rep: NSBitmapImageRep) -> Int {
        guard let corner = rep.colorAt(x: 1, y: 1)?.usingColorSpace(.sRGB) else { return 0 }
        var n = 0
        for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
            for x in stride(from: 0, to: rep.pixelsWide, by: 2) {
                guard let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB) else { continue }
                let d = abs(c.redComponent - corner.redComponent) + abs(c.greenComponent - corner.greenComponent)
                    + abs(c.blueComponent - corner.blueComponent)
                if d > 0.06 { n += 1 }
            }
        }
        return n
    }

    func testCaptureTheWholeCallBarOnALiveCall() async throws {
        let live = await liveCall()
        live.handle(#"{"type":"response.output_audio.delta","delta":"\#(PCM16.encode([0.2]).base64EncodedString())"}"#)
        let rep = try await render(
            VStack(spacing: 0) { CallBarView(session: session, agent: nil); Spacer() }
                .background(Color(white: 0.1)),
            size: CGSize(width: 440, height: 360))
        let text = words(rep)
        XCTAssertTrue(text.contains("On a call with Atlas"), text)
        XCTAssertTrue(text.contains("Live voice"), text)
        if capturing { try RightPaneFixture.saveCapture(rep, named: "c4-call-bar-live.png") }
    }
}
