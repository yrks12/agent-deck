import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The Mac window, laid out like the iPhone (owner, 2026-09-30).** A roster
/// on the left drawn like the phone's roster list — the chief's card, the
/// pulse line, "N things need your attention", then Pinned and the sections
/// with every desk once — and the conversation on the right drawn like the
/// phone's thread: the canvas, a header with the face and a status line and
/// the screen and call buttons, and a composer whose button is his voice when
/// the box is empty and the send arrow when it is not. The side panel is
/// secondary: closed until he opens it.
final class TheMacIsLaidOutLikeThePhoneTests: XCTestCase {

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func source(_ file: String) throws -> String {
        try String(contentsOf: packageRoot.appendingPathComponent("Sources/DeckUI/\(file)"), encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces).hasPrefix("//") ? "" : String($0) }
            .joined(separator: "\n")
    }

    private func row(_ name: String, section: String = "Work") -> SidebarRow {
        SidebarRow(agent: Agent(name: name, title: "", section: section), threadID: "direct:\(name)",
                   threads: [], preview: ThreadPreview(text: "hi"), timestamp: nil, unreadCount: 0)
    }

    // MARK: the roster list

    /// The phone draws each desk once: the chief in its card, pinned desks
    /// under Pinned, and the sections without either. The Mac drew pinned desks
    /// twice (a tile and a row), which a `List` keyed by desk cannot hold once
    /// both are rows.
    func testTheListedSectionsShowEveryDeskOnceWithPinnedFirst() {
        let chief = row("chief")
        let snapshot = SidebarSnapshot(
            pinned: [row("hemingway"), row("seeker")],
            sections: [
                SidebarSection(name: "Work", rows: [row("hemingway"), row("seeker"), row("ledger")],
                               overflowUnreadCount: 2),
                SidebarSection(name: "Pals", rows: [row("seeker", section: "Pals")], overflowUnreadCount: 0),
                SidebarSection(name: "Personal", rows: [row("travel scout", section: "Personal")],
                               overflowUnreadCount: 0),
            ],
            totalUnread: 0, chief: chief)
        let listed = snapshot.listedSections
        XCTAssertEqual(listed.map(\.name), ["Pinned", "Work", "Personal"], "an emptied section is dropped")
        XCTAssertEqual(listed.map { $0.rows.map(\.agent.name) },
                       [["hemingway", "seeker"], ["ledger"], ["travel scout"]])
        XCTAssertEqual(listed[1].overflowUnreadCount, 2, "the overflow pill survives the filtering")
        let names = listed.flatMap { $0.rows.map(\.agent.name) }
        XCTAssertEqual(names.count, Set(names).count, "a desk is listed twice")
        XCTAssertFalse(names.contains("chief"), "the chief is its card, not a row")
    }

    func testTheSidebarIsBuiltFromTheSharedPhonePieces() throws {
        let sidebar = try source("SidebarView.swift")
        for piece in ["ChiefHero(", "RosterRowContent(", "RosterPulse(", "AttentionBanner(", "listedSections",
                      "DeckPalette.canvas"] {
            XCTAssertTrue(sidebar.contains(piece), "the Mac roster does not draw \(piece)")
        }
        XCTAssertFalse(sidebar.contains("FavouritesRow("), "pinned desks are tiles again, not the phone's rows")
    }

    // MARK: the conversation

    func testTheThreadIsThePhonesThreadOnTheMac() throws {
        let thread = try source("ThreadView.swift")
        XCTAssertTrue(thread.contains("ComposerMode.make("), "the composer button is not the shared rule")
        XCTAssertTrue(thread.contains("DeckPalette.canvas"), "the conversation is not on the phone's canvas")
        XCTAssertTrue(thread.contains("CallButton("), "no call button in the header")
        XCTAssertTrue(thread.contains("focus: .screen"), "no screen button in the header")
        let header = try source("ThreadHeader.swift")
        XCTAssertTrue(header.contains("ThreadHeaderStatus.line("), "the header has no status line")
        XCTAssertTrue(header.contains("AvatarView("), "the header has no face")
    }

    /// Pixels, not source: in the dark the conversation sits on black, as on
    /// the phone, where it used to sit on the window's grey.
    @MainActor
    func testTheConversationIsDrawnOnTheCanvasInTheDark() async throws {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "hemingway", threadID: "direct:hemingway")
        for _ in 0..<100 {
            if case .loaded = store.thread { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        let size = CGSize(width: 640, height: 700)
        let (host, window) = RightPaneFixture.host(ThreadView(store: store).frame(width: 640, height: 700),
                                                   size: size)
        defer { host.rootView = AnyView(EmptyView()); window.contentView = nil; window.close() }
        try await Task.sleep(nanoseconds: 500_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        let scale = CGFloat(rep.pixelsWide) / size.width
        // The left margin of the transcript, between the header and the box.
        let probe = try XCTUnwrap(rep.colorAt(x: Int(4 * scale), y: Int(size.height * 0.5 * scale))?
            .usingColorSpace(.genericGamma22Gray))
        XCTAssertLessThan(probe.whiteComponent, 0.03, "the conversation is not on the black canvas: \(probe)")
    }

    // MARK: the side panel is secondary

    func testTheSidePanelOpensOnlyWhenAsked() throws {
        let root = try source("DeckRootView.swift")
        XCTAssertTrue(root.contains("showInspector = false"), "the side panel opens by itself again")
        XCTAssertTrue(root.contains("SettingsPanelView(store: store)"), "the side panel is gone, not secondary")
        XCTAssertTrue(root.contains("showAttention:"), "the attention banner cannot open the side panel")
    }
}
