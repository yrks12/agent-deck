import XCTest
import SwiftUI
import UIKit
import AVFoundation
import DeckKit
@testable import AgentDeckPhone

/// **The call, drawn off-screen into `ios/Artifacts/call-*.png`**, plus the
/// iPhone-only mapping of the OS's audio notifications. Nothing here opens the
/// mic, plays a sound or takes the audio session (`AudioGate`).
@MainActor
final class CallRenderTests: XCTestCase {
    private static let artifacts = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Artifacts")

    private var windows: [UIWindow] = []

    override func tearDown() {
        windows.forEach { $0.isHidden = true }
        windows = []
    }

    private func render<V: View>(_ view: V, name: String, style: UIUserInterfaceStyle = .dark) throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.overrideUserInterfaceStyle = style
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        windows.append(window)
        RunLoop.main.run(until: Date().addingTimeInterval(1.2))
        let format = UIGraphicsImageRendererFormat()
        format.scale = window.screen.scale
        let image = UIGraphicsImageRenderer(bounds: window.bounds, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        try FileManager.default.createDirectory(at: Self.artifacts, withIntermediateDirectories: true)
        try XCTUnwrap(image.pngData()).write(to: Self.artifacts.appendingPathComponent("\(name).png"))
    }

    private var call: CallScreenContent.Line {
        .init(desk: "chief", name: "Chief", startedAt: Date().addingTimeInterval(-94))
    }

    private func agent() async throws -> Agent? {
        try await FixtureDeckClient().roster().agents.first { $0.name == "chief" }
    }

    func testSpeaking() async throws {
        let stage = CallStage(mode: .speaking, level: 0.7, status: "Chief is speaking — the desk is still working",
                              isLive: true)
        try render(CallScreenContent(stage: stage, call: call, agent: try await agent(),
                                     caption: "The tests are green. I'm pushing the branch now.",
                                     isMuted: false, speakerOn: true, isOnHold: false),
                   name: "call-01-speaking-dark")
    }

    func testListeningWhileTheDeskWorks() async throws {
        let stage = CallStage(mode: .working, level: 0.35, status: "The desk is on it — keep talking", isLive: true)
        try render(CallScreenContent(stage: stage, call: call, agent: try await agent(), caption: "",
                                     isMuted: false, speakerOn: false, isOnHold: false),
                   name: "call-02-working-light", style: .light)
    }

    func testOnHold() async throws {
        let stage = CallStage(mode: .listening, level: 0, status: "Listening…", isLive: true)
        try render(CallScreenContent(stage: stage, call: call, agent: try await agent(), caption: "",
                                     isMuted: true, speakerOn: true, isOnHold: true),
                   name: "call-03-on-hold-dark")
    }

    func testThePillOverTheRoster() async throws {
        let fixture = FixtureDeckClient()
        let payload = try await fixture.roster()
        let store = PhoneStore(preview: SidebarSnapshot.build(from: payload), agents: payload.agents,
                               attention: [], usage: nil)
        let pill = CallPillBar.Pill(desk: "chief", name: "Chief", startedAt: call.startedAt, isMuted: false)
        let view = RootView()
            .safeAreaInset(edge: .top, spacing: 0) { CallPillBar(pill: pill) }
            .environmentObject(store)
        try render(view, name: "call-04-pill-dark")
    }

    // MARK: - The OS's notifications, as the call understands them

    func testAPhoneCallRingingIsAnInterruption() {
        let began = PhoneAudioSession.interruption(
            name: AVAudioSession.interruptionNotification,
            userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.began.rawValue])
        XCTAssertEqual(began, .began)
        let ended = PhoneAudioSession.interruption(
            name: AVAudioSession.interruptionNotification,
            userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.ended.rawValue,
                       AVAudioSessionInterruptionOptionKey: AVAudioSession.InterruptionOptions.shouldResume.rawValue])
        XCTAssertEqual(ended, .ended(shouldResume: true))
        let kept = PhoneAudioSession.interruption(
            name: AVAudioSession.interruptionNotification,
            userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.ended.rawValue])
        XCTAssertEqual(kept, .ended(shouldResume: false))
    }

    func testHeadphonesGoneIsARouteLossAndOtherRouteChangesAreNot() {
        XCTAssertEqual(PhoneAudioSession.interruption(
            name: AVAudioSession.routeChangeNotification,
            userInfo: [AVAudioSessionRouteChangeReasonKey: AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue]),
                       .routeLost)
        XCTAssertNil(PhoneAudioSession.interruption(
            name: AVAudioSession.routeChangeNotification,
            userInfo: [AVAudioSessionRouteChangeReasonKey: AVAudioSession.RouteChangeReason.newDeviceAvailable.rawValue]))
        XCTAssertEqual(PhoneAudioSession.interruption(name: AVAudioSession.mediaServicesWereResetNotification,
                                                      userInfo: nil), .mediaServicesReset)
    }

    func testTheTestProcessNeverTakesTheAudioSession() throws {
        XCTAssertTrue(AudioGate.isSilenced)
        let before = AVAudioSession.sharedInstance().category
        try PhoneAudioSession().activate(XCTUnwrap(CallAudioSessionPlan.forCall(on: .phone, speaker: true)))
        XCTAssertEqual(AVAudioSession.sharedInstance().category, before)
    }

    func testThisBuildIsThePhone() {
        XCTAssertEqual(CallPlatform.current, .phone)
        XCTAssertNotNil(CallAudioSessionPlan.forCall(on: CallPlatform.current, speaker: true))
    }
}
