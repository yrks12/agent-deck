import XCTest
import SwiftUI
import UIKit
import DeckKit
@testable import AgentDeckPhone

/// **The iPhone pictures in README.md and docs/quickstart.md**, drawn off-screen
/// into `docs/images/iphone-*.png`. Fixture data only: `FixtureDeckClient`, the
/// made-up desks (Chief, Hemingway, Seeker, Ledger), `example.com` addresses and
/// (the agent-screen picture is a simulator screenshot of `DECK_SCREEN_DEMO=1`, see docs/images/README note in the commit). No network, no sound, no microphone.
@MainActor
final class DocsShotsTests: XCTestCase {
    private static let images = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("docs/images")

    private var windows: [UIWindow] = []

    override func tearDown() {
        windows.forEach { $0.isHidden = true }
        windows = []
    }

    private func store() async throws -> PhoneStore {
        let fixture = FixtureDeckClient()
        let payload = try await fixture.roster()
        let snapshot = SidebarSnapshot.build(from: payload)
        let byName = Dictionary(payload.agents.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        let attention = AttentionItem.queue(
            approvals: try await fixture.approvals().approvals,
            handoffs: try await fixture.handoffs().handoffs,
            agents: byName).filter { if case .known = $0.desk { return true } else { return false } }
        let usage = ClaudeUsage(available: true, plan: "max", windows: [
            .init(key: "session", label: "5-hour window", percent: 38, resetsAt: Date().addingTimeInterval(2 * 3600 + 900)),
            .init(key: "weekly", label: "Weekly", percent: 72, resetsAt: Date().addingTimeInterval(3 * 86400)),
        ])
        return PhoneStore(preview: snapshot, agents: payload.agents, attention: attention, usage: usage)
    }

    private func messages() -> [Message] {
        let t = "direct:chief"
        let base = Date().addingTimeInterval(-3600)
        var n = 0
        func msg(_ role: MessageRole, _ author: String, _ text: String, _ m: Double,
                 decision: Decision? = nil, attachments: [Attachment] = [], voice: VoiceNoteRef? = nil) -> Message {
            n += 1
            return Message(id: "m\(n)", cursor: String(format: "%06d", n), threadID: t, author: author, role: role,
                           sentAt: base.addingTimeInterval(m * 60), text: text, attachments: attachments,
                           kind: decision == nil ? .text : .decision, decision: decision, voiceNote: voice)
        }
        return [
            msg(.owner, "owner", "Can you check the launch plan before I send it?", 0,
                voice: VoiceNoteRef(id: "vn1", url: "/v1/voice/vn1")),
            msg(.agent, "chief", "Heard you. Hemingway is tightening the intro.", 1),
            msg(.owner, "owner", "Here is the draft.", 5,
                attachments: [Attachment(kind: .file, value: "launch-plan.pdf", url: "/v1/attachments/a1/launch-plan.pdf")]),
            msg(.agent, "chief", "Send the launch note today, or wait for the sources?", 6.5, decision: Decision(
                id: "dec_1", prompt: "Send the launch note today, or wait for the sources?",
                help: "Seeker has one source left to check.",
                options: [
                    DecisionOption(label: "Send today", value: "send", style: .primary),
                    DecisionOption(label: "Wait for sources", value: "wait"),
                ],
                allowCustom: true, state: .open)),
        ]
    }

    private func render<V: View>(_ view: V, name: String, style: UIUserInterfaceStyle = .dark,
                                 settle: TimeInterval = 1.5) throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.overrideUserInterfaceStyle = style
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        windows.append(window)
        RunLoop.main.run(until: Date().addingTimeInterval(settle))
        let format = UIGraphicsImageRendererFormat()
        format.scale = window.screen.scale
        let image = UIGraphicsImageRenderer(bounds: window.bounds, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        try FileManager.default.createDirectory(at: Self.images, withIntermediateDirectories: true)
        try XCTUnwrap(image.pngData()).write(to: Self.images.appendingPathComponent("\(name).png"))
    }

    func testRoster() async throws {
        let s = try await store()
        try render(RootView().environmentObject(s), name: "iphone-roster-dark")
        try render(RootView().environmentObject(s), name: "iphone-roster-light", style: .light)
    }

    func testChatWithVoiceAndAttachment() async throws {
        let s = try await store()
        let model = ThreadModel(preview: "direct:chief", messages: messages())
        let view = NavigationStack {
            ThreadScreen(route: ThreadRoute(threadID: "direct:chief", agent: "chief"), model: model)
        }
        .environmentObject(s)
        .environment(\.phoneVoice, PhoneVoice(store: s))
        try render(view, name: "iphone-chat-dark")
        try render(view, name: "iphone-chat-light", style: .light)
    }

    func testCall() async throws {
        let agent = try await FixtureDeckClient().roster().agents.first { $0.name == "chief" }
        let stage = CallStage(mode: .speaking, level: 0.7, status: "Chief is speaking", isLive: true)
        let call = CallScreenContent.Line(desk: "chief", name: "Chief", startedAt: Date().addingTimeInterval(-94))
        try render(CallScreenContent(stage: stage, call: call, agent: agent,
                                     caption: "The tests are green. I'm pushing the branch now.",
                                     isMuted: false, speakerOn: true, isOnHold: false),
                   name: "iphone-call-dark")
    }

    func testAttention() async throws {
        let s = try await store()
        let view = TabView {
            NavigationStack { AttentionView(path: .constant(NavigationPath())) }
                .tabItem { Label("Attention", systemImage: "exclamationmark.bubble.fill") }
        }
        .tint(.primary)
        .environmentObject(s)
        try render(view, name: "iphone-attention-dark")
    }

    func testConnect() throws {
        let s = PhoneStore(preview: .empty, agents: [], attention: [], usage: nil)
        s.phase = .connect
        try render(RootView().environmentObject(s), name: "iphone-connect-dark")
    }
}
