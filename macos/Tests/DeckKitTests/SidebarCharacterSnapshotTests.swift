import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// Off-screen renders of the sidebar characters (acceptance D-1). Fixture
/// data, no network, no launched app. The PNGs land in
/// `UITests/Artifacts/overhaul/` so they can be looked at and reviewed.
@MainActor
final class SidebarCharacterSnapshotTests: XCTestCase {

    private static var outDir: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("UITests/Artifacts/overhaul")
    }

    private func render<V: View>(_ view: V, width: CGFloat, height: CGFloat, dark: Bool, to name: String) throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view.frame(width: width, height: height).environment(\.colorScheme, dark ? .dark : .light))
        host.frame = NSRect(x: 0, y: 0, width: width, height: height)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < 0.3 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
        }
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
        try FileManager.default.createDirectory(at: Self.outDir, withIntermediateDirectories: true)
        try png.write(to: Self.outDir.appendingPathComponent(name))
        window.orderOut(nil)
        window.contentView = nil
    }

    private func row(_ name: String, _ title: String, _ state: AgentState, _ preview: String,
                     unread: Int = 0, blocked: String? = nil, ago: TimeInterval = 600, chief: Bool = false) -> SidebarRow {
        let agent = Agent(
            name: name, title: title, state: state,
            blocked: blocked.map { Blocked(what: $0, reason: "dialog_unrelayed") })
        return SidebarRow(
            agent: agent, threadID: "direct:\(name)", threads: [],
            preview: ThreadPreview(text: preview),
            timestamp: Date().addingTimeInterval(-ago), unreadCount: unread)
    }

    private var roster: [SidebarRow] {
        [
            row("Initech UX", "UX", .working, "Found a blocker: one-off import…", ago: 300),
            row("Initech Product", "Product", .idle, "UX is on the #15 URL with the…", ago: 1200),
            row("Initech", "", .idle, "Message from Initech UX: …", unread: 2, ago: 1500),
            row("Acme Product", "", .idle, "", unread: 1, blocked: "Sign in to Google", ago: 2400),
            row("Acme", "", .idle, "Messaged Acme Product: COS…", ago: 3600 * 2),
            row("Acme Growth", "", .idle, "Holding until they confirm Ena…", ago: 3600 * 3),
            row("Venture", "", .working, "Working through the cutover plan", ago: 7200),
            row("Venture Product", "", .idle, "Message from Venture Craft: …", ago: 7300),
            row("Venture Craft", "", .offline, "Message from Venture: Ack. P…", ago: 86_400 * 2),
            row("Globex Sales", "", .idle, "Quiet until something real.", ago: 86_400 * 3),
        ]
    }

    private func sidebar(chiefSelected: Bool) -> some View {
        VStack(spacing: 0) {
            ChiefBlock(row: row("cos", "Chief of staff", .idle, "Hello", ago: 60), isSelected: chiefSelected) {}
            VStack(spacing: 0) {
                ForEach(roster) { AgentRow(row: $0).padding(.horizontal, 14) }
            }
            Spacer(minLength: 0)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    func testRenderTheSidebarWithTheHeroTile() throws {
        try render(sidebar(chiefSelected: true), width: 285, height: 962, dark: true, to: "d1-sidebar-dark.png")
        try render(sidebar(chiefSelected: false), width: 285, height: 962, dark: false, to: "d1-sidebar-light.png")
    }

    private var lineup: some View {
        let names = ["chief", "hemingway", "grok bot", "atlas", "larder", "Acme", "Initech UX", "Globex Sales",
                     "seeker", "ledger", "Venture Craft", "Acme Growth"]
        return VStack(alignment: .leading, spacing: 18) {
            ForEach([(RowAttention.quiet, false, 96.0), (.working, false, 96.0), (.waitingForYou, false, 96.0), (.quiet, true, 96.0)], id: \.2) { _ in EmptyView() }
            ForEach(Array([(RowAttention.quiet, false), (.working, false), (.waitingForYou, false), (.quiet, true)].enumerated()), id: \.offset) { _, kind in
                HStack(spacing: 14) {
                    ForEach(names, id: \.self) { name in
                        AvatarView(look: AvatarLook.forName(name), attention: kind.0, isDimmed: kind.1, localAvatarURL: nil, size: 64)
                    }
                }
            }
            HStack(spacing: 14) {
                ForEach(names, id: \.self) { name in
                    AvatarView(look: AvatarLook.forName(name), attention: .quiet, isDimmed: false, localAvatarURL: nil, size: 32)
                        .frame(width: 64)
                }
            }
        }
        .padding(24)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    func testRenderTheCharacterLineup() throws {
        try render(lineup, width: 12 * 78 + 48, height: 4 * 82 + 4 * 18 + 90, dark: true, to: "d1-characters-lineup.png")
    }
}
