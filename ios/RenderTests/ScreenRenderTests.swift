import XCTest
import SwiftUI
import UIKit
import DeckKit
@testable import AgentDeckPhone

/// **Every screen, drawn off-screen into `ios/Artifacts/*.png`.**
///
/// Not a pixel-diff gate — a picture to look at. Each screen is hosted in a
/// real `UIWindow` on the test host's scene (so navigation bars, large titles,
/// tab bars and safe areas lay out as on the device) and captured with
/// `drawHierarchy`. Data is `FixtureDeckClient`'s: no network, no sound, no
/// microphone — nothing here touches `AudioGate` or a deck.
@MainActor
final class ScreenRenderTests: XCTestCase {

    private static let artifacts = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Artifacts")

    private var windows: [UIWindow] = []

    override func tearDown() {
        windows.forEach { $0.isHidden = true }
        windows = []
    }

    // MARK: fixtures

    private func fixtureStore() async throws -> PhoneStore {
        let fixture = FixtureDeckClient()
        let payload = try await fixture.roster()
        let snapshot = SidebarSnapshot.build(from: payload)
        let agents = payload.agents
        let byName = Dictionary(agents.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        let attention = AttentionItem.queue(
            approvals: try await fixture.approvals().approvals,
            handoffs: try await fixture.handoffs().handoffs,
            agents: byName)
        let usage = ClaudeUsage(available: true, plan: "max", windows: [
            .init(key: "session", label: "5-hour window", percent: 38, resetsAt: Date().addingTimeInterval(2 * 3600 + 900)),
            .init(key: "weekly", label: "Weekly", percent: 72, resetsAt: Date().addingTimeInterval(3 * 86400)),
        ])
        return PhoneStore(preview: snapshot, agents: agents, attention: attention, usage: usage)
    }

    private func threadMessages() -> [Message] {
        let t = "direct:chief"
        let base = Date().addingTimeInterval(-3600)
        func at(_ m: Double) -> Date { base.addingTimeInterval(m * 60) }
        var n = 0
        func msg(_ role: MessageRole, _ author: String, _ text: String, _ m: Double, decision: Decision? = nil) -> Message {
            n += 1
            return Message(id: "m\(n)", cursor: String(format: "%06d", n), threadID: t, author: author, role: role,
                           sentAt: at(m), text: text, kind: decision == nil ? .text : .decision, decision: decision)
        }
        return [
            msg(.owner, "owner", "Morning. Where are we on the phone app?", 0),
            msg(.agent, "chief", "Morning. The shared core builds for iOS now — the Mac-only bits are fenced, nothing changed on the Mac.", 1),
            msg(.agent, "chief", "Hemingway is drafting the App Store copy; Seeker is checking what TestFlight needs.", 1.5),
            msg(.system, "routine", "Morning sweep ran: 2 desks idle, nothing stuck.", 20),
            msg(.owner, "owner", "Good. Keep it read-only for the demo.", 31),
            msg(.agent, "chief", "Will do. One call is yours:", 32),
            msg(.agent, "chief", "Ship v1 to TestFlight today, or wait for voice calls?", 32.5, decision: Decision(
                id: "dec_1", prompt: "Ship v1 to TestFlight today, or wait for voice calls?",
                help: "Voice is the v2 slice; v1 is text, decisions and approvals.",
                options: [
                    DecisionOption(label: "Ship v1 today", value: "ship", style: .primary),
                    DecisionOption(label: "Wait for voice", value: "wait"),
                ],
                allowCustom: true, state: .open)),
        ]
    }

    // MARK: rendering

    private func render<V: View>(_ view: V, name: String, style: UIUserInterfaceStyle = .dark,
                                 settle: TimeInterval = 1.0) throws {
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
        try FileManager.default.createDirectory(at: Self.artifacts, withIntermediateDirectories: true)
        let url = Self.artifacts.appendingPathComponent("\(name).png")
        try XCTUnwrap(image.pngData()).write(to: url)
        XCTAssertGreaterThan(image.size.width, 0)
    }

    func testConnectScreen() throws {
        let store = PhoneStore(preview: .empty, agents: [], attention: [], usage: nil)
        store.phase = .connect
        try render(RootView().environmentObject(store), name: "01-connect")
    }

    func testAgentList() async throws {
        let store = try await fixtureStore()
        try render(RootView().environmentObject(store), name: "02-agents-dark")
        try render(RootView().environmentObject(store), name: "03-agents-light", style: .light)
    }

    func testThread() async throws {
        let store = try await fixtureStore()
        let model = ThreadModel(preview: "direct:chief", messages: threadMessages())
        let view = NavigationStack {
            ThreadScreen(route: ThreadRoute(threadID: "direct:chief", agent: "chief"), model: model)
        }
        .environmentObject(store)
        try render(view, name: "04-thread-dark", settle: 1.5)
        try render(view, name: "05-thread-light", style: .light, settle: 1.5)
    }

    func testAttention() async throws {
        let store = try await fixtureStore()
        let view = TabView {
            NavigationStack { AttentionView(path: .constant(NavigationPath())) }
                .tabItem { Label("Attention", systemImage: "exclamationmark.bubble.fill") }
        }
        .tint(.primary)
        .environmentObject(store)
        try render(view, name: "06-attention-dark")
    }
}
