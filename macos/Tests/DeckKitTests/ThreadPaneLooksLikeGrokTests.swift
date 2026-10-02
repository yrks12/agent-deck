import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The thread pane as he sees it (D-1/D-2).**
///
/// Drawn off-screen (borderless window parked 40,000pt away, activation policy
/// `.prohibited`) from the fixture deck. No text recognition: Vision in this
/// suite's process has deadlocked on an exhausted dispatch pool, so words are
/// checked on the values the pane draws and pixels only where colour is the
/// point.
@MainActor
final class ThreadPaneLooksLikeGrokTests: XCTestCase {

    private func openChief() async -> DeckStore {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await waitFor { if case .loaded = store.thread { return true } else { return false } }
        return store
    }

    /// Routine fires and deck notices reach the pane as captions — never as
    /// bubbles — and the box says who it is to.
    func testTheFixtureThreadDrawsCaptionsAndNamesItsDesk() async throws {
        let store = await openChief()
        guard case .loaded(let screen) = store.thread else { return XCTFail("thread never opened") }
        let items = TranscriptList(entries: screen.entries, desk: "chief", newFrom: screen.newFrom)
            .timeline(opened: [])
        let captions = items.compactMap { item -> String? in
            if case .caption(let caption) = item { return caption.text }
            return nil
        }
        XCTAssertTrue(captions.contains { $0.hasPrefix("Routine ·") }, "no routine caption: \(captions)")
        XCTAssertTrue(captions.contains { $0.hasPrefix("Deck ·") }, "no deck caption: \(captions)")
        XCTAssertTrue(items.contains { if case .rollup = $0 { return true } else { return false } },
                      "no folded peer traffic")
        XCTAssertEqual(screen.presentation.composer, .enabled(placeholder: "Message Chief"))
    }

    /// He asked for the "Return to send · Shift-Return…" line and the "Idle —
    /// its session is up" strip to go. The rule stays the field's tooltip and
    /// VoiceOver hint; the strip only draws what `showsOnConversation` allows.
    func testTheHintIsNotDrawnAndTheStripIsGated() throws {
        let source = try String(contentsOf: Self.threadView, encoding: .utf8)
        XCTAssertFalse(source.contains("Text(ComposerBox.sendHint)"), "the Return hint is drawn again")
        XCTAssertTrue(source.contains("status.showsOnConversation"),
                      "the status strip draws for every state, Idle included")
    }

    /// His own bubble is grey, on the right — not accent blue.
    func testHisBubbleIsGrey() async throws {
        let bubble = MessageBubble(message: Message(
            id: "y1", cursor: "1", threadID: "direct:chief", author: DeckOwner.name,
            role: .owner, sentAt: Date(), text: "any updates? any updates? any updates?"))
        let size = CGSize(width: 420, height: 60)
        let (host, window) = RightPaneFixture.host(bubble.frame(width: 420, height: 60), size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: 300_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        // Just inside the bubble's right-hand edge, level with the text.
        let scale = CGFloat(rep.pixelsWide) / size.width
        let inside = try XCTUnwrap(rep.colorAt(x: Int((size.width - 6) * scale), y: Int(30 * scale))?
            .usingColorSpace(.deviceRGB))
        let outside = try XCTUnwrap(rep.colorAt(x: 2, y: 2)?.usingColorSpace(.deviceRGB))
        XCTAssertLessThan(inside.saturationComponent, 0.15, "his bubble is tinted: \(inside)")
        XCTAssertGreaterThan(abs(inside.brightnessComponent - outside.brightnessComponent), 0.04,
                             "no bubble drawn at the right-hand edge — nothing to judge")
    }

    /// D-2 / ours-04: at the narrowest column the window allows, the whole pane
    /// fits the width it is offered — a pane that needs more is centred and
    /// cut at both edges.
    func testThePaneFitsTheNarrowestColumn() async throws {
        let store = await openChief()
        for column: CGFloat in [380, 520] {
            let asked = NSHostingController(rootView: ThreadView(store: store))
                .sizeThatFits(in: CGSize(width: column, height: 900))
            XCTAssertLessThanOrEqual(asked.width, column + 0.5,
                                     "the pane needs \(asked.width)pt in a \(column)pt column")
        }
        // Calibration: a pane that genuinely needs more must be seen.
        let wide = NSHostingController(rootView: HStack { Color.red.frame(width: 900, height: 10) })
            .sizeThatFits(in: CGSize(width: 380, height: 900))
        XCTAssertGreaterThan(wide.width, 380, "this measurement cannot see an overflow")
    }

    /// The store hands the pane what it needs: where unread starts, and the
    /// desk's character while it works.
    func testTheStoreMarksUnreadAndTheWorkingCharacter() async throws {
        let store = await openChief()
        guard case .loaded(let screen) = store.thread else { return XCTFail("thread never opened") }
        // The fixture's chief has two unread: the last two of its own lines.
        XCTAssertNotNil(screen.newFrom, "unread was dropped before the divider could be placed")
        XCTAssertEqual(store.workingLook, AvatarLook.forName("chief"),
                       "chief is WORKING and no character is drawn under its last line")
    }

    static let threadView = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Sources/DeckUI/ThreadView.swift")

    private func waitFor(_ condition: @escaping () -> Bool) async {
        let began = Date()
        while !condition() && Date().timeIntervalSince(began) < 5 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }
}
