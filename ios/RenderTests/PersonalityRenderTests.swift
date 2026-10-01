import XCTest
import SwiftUI
import UIKit
import DeckKit
@testable import AgentDeckPhone

/// **The app's character moments, drawn into `ios/Artifacts/`.** The roster
/// pulse line and the empty / first-load states. A picture to look at, plus
/// the one piece of logic the pulse owns: the sentence it says.
@MainActor
final class PersonalityRenderTests: XCTestCase {

    private static let artifacts = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Artifacts")

    private var windows: [UIWindow] = []

    override func tearDown() {
        windows.forEach { $0.isHidden = true }
        windows = []
    }

    func testPulseLineOmitsZeroBuckets() async throws {
        let rows = SidebarSnapshot.build(from: try await FixtureDeckClient().roster()).allRows
        let summary = RosterPulse.Summary(rows: rows)
        XCTAssertFalse(summary.line.contains("0 "))
        XCTAssertEqual(RosterPulse.Summary(rows: []).line, "All quiet. Nothing is running right now.")
    }

    func testRosterWithPulse() async throws {
        let fixture = FixtureDeckClient()
        let payload = try await fixture.roster()
        let store = PhoneStore(preview: SidebarSnapshot.build(from: payload), agents: payload.agents,
                               attention: [], usage: nil)
        try render(NavigationStack { RosterView() }.environmentObject(store), name: "p1-roster-pulse")
    }

    func testEmptyStates() throws {
        let store = PhoneStore(preview: .empty, agents: [], attention: [], usage: nil)
        try render(NavigationStack { AttentionView(path: .constant(NavigationPath())) }.environmentObject(store),
                   name: "p2-attention-empty")
        try render(NavigationStack { RosterView() }.environmentObject(store), name: "p3-roster-empty", style: .light)
    }

    private func render<V: View>(_ view: V, name: String, style: UIUserInterfaceStyle = .dark) throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.overrideUserInterfaceStyle = style
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        windows.append(window)
        RunLoop.main.run(until: Date().addingTimeInterval(1.0))
        let format = UIGraphicsImageRendererFormat()
        format.scale = window.screen.scale
        let image = UIGraphicsImageRenderer(bounds: window.bounds, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        try FileManager.default.createDirectory(at: Self.artifacts, withIntermediateDirectories: true)
        try XCTUnwrap(image.pngData()).write(to: Self.artifacts.appendingPathComponent("\(name).png"))
    }
}
