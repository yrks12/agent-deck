import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The phone-style Mac froze on the owner's deck (2026-09-30, 20:21).**
///
/// Measured by `sample` on his Mac: the main thread at 100%, every sample in
/// `NSHostingView.beginTransaction → LazySubviewPlacements.placeSubviews` over
/// the transcript's `ForEach` — `TranscriptItem.id` and `ThreadEntry.id` the
/// only app frames. The conversation's lazy stack was being re-placed on every
/// transaction, for ever, with nobody touching anything.
///
/// The build had just put the desk's face in the conversation's header, and
/// the face's eyes move while the desk works (a periodic `TimelineView`). A
/// tick inside a *row* of the lazy stack redraws that row; a tick in a
/// *sibling* of the transcript, in the `VStack` that sizes it, is a new
/// transaction for the whole column and re-places the transcript. With his
/// desk working most of the time and a long thread, that never stops. The
/// rule `LayoutSettlesTests` states — no steady state of the thread pane may be
/// animated — had been broken from outside the transcript.
///
/// Off screen, AppKit does not suspend a timeline the way it suspends an
/// AppKit animation, so this one is measurable here: the conversation of a
/// working desk, with a long real-shaped thread, must stop laying out once it
/// has settled.
@MainActor
final class TheConversationStaysQuietWhileItsDeskWorksTests: XCTestCase {

    private final class LayoutProbe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() {
            passes += 1
            super.layout()
        }
    }

    /// A thread shaped like his: long answers, his short lines, deck notices,
    /// relayed traffic with another desk, over many days.
    private func longThread(_ count: Int) -> [Message] {
        let base: TimeInterval = 1_780_000_000
        return (0..<count).map { i in
            let ts = base + Double(i) * 1_800
            switch i % 7 {
            case 0:
                return makeMessage("m\(i)", text: "change the plan \(i)", thread: "direct:chief",
                                   author: DeckOwner.name, role: .owner, ts: ts)
            case 3:
                return makeMessage("m\(i)", text: "[Agent Deck] Hired: desk-\(i) now reports to you.",
                                   thread: "direct:chief", author: "deck", role: .system, ts: ts)
            default:
                return makeMessage("m\(i)", text: String(repeating: "A long considered answer, line \(i). ", count: 6),
                                   thread: "direct:chief", author: "chief", role: .agent, ts: ts)
            }
        }
    }

    private func workingDeskWithLongThread() async -> DeckStore {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief", title: "Chief of staff")
        chief.state = .working
        client.rosterPayload = RosterPayload(
            agents: [chief], threads: [makeThread("direct:chief", agent: "chief")], sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:chief", messages: longThread(2_100),
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()
        return store
    }

    func testTheConversationOfAWorkingDeskStopsLayingOutOnceSettled() async throws {
        let store = await workingDeskWithLongThread()
        guard case .loaded(let screen) = store.thread else { return XCTFail("the thread never opened") }
        XCTAssertGreaterThanOrEqual(screen.entries.count, 2_000, "not the long thread this is about")
        XCTAssertEqual(store.agents["chief"]?.state, .working, "the desk is not working, so nothing would move")

        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: ThreadView(store: store).frame(width: 900, height: 800))
        probe.frame = NSRect(x: 0, y: 0, width: 900, height: 800)
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: 900, height: 800),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        defer { probe.rootView = ThreadView(store: store).frame(width: 1, height: 1); window.contentView = nil; window.close() }
        probe.layoutSubtreeIfNeeded()

        func pump(_ seconds: TimeInterval) {
            let began = Date()
            while Date().timeIntervalSince(began) < seconds {
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
        }
        pump(1.5)
        let before = probe.passes
        pump(2.5)
        let during = probe.passes - before

        XCTAssertGreaterThan(probe.fittingSize.height, 1, "the pane never drew, so its quiet means nothing")
        XCTAssertLessThan(during, 2,
                          "the conversation of a working desk was laid out \(during) times in 2.5s with "
                          + "nothing happening — each pass re-places a 2,000-entry transcript. That is "
                          + "the 100% CPU freeze on his Mac.")
    }

    /// The roster column is its own hosting view, so a face moving there does
    /// not touch the conversation. The chief's eyes moving above the list
    /// have always cost one roster layout per tick (measured on the build he
    /// ran at 6% CPU: 3 passes in 2.5s); the redesign added the pulse line's
    /// faces and doubled it to 6. The pulse faces are still now, and the
    /// roster is held to one pass per eye tick.
    func testTheRosterOfAWorkingDeckLaysOutNoMoreThanOncePerEyeTick() async throws {
        let store = await workingDeskWithLongThread()
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: SidebarView(store: store).frame(width: 320, height: 900))
        probe.frame = NSRect(x: 0, y: 0, width: 320, height: 900)
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: 320, height: 900),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        defer { window.contentView = nil; window.close() }
        probe.layoutSubtreeIfNeeded()
        func pump(_ seconds: TimeInterval) {
            let began = Date()
            while Date().timeIntervalSince(began) < seconds {
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
        }
        pump(1.5)
        let before = probe.passes
        pump(2.5)
        let during = probe.passes - before
        XCTAssertGreaterThan(probe.fittingSize.height, 1, "the roster never drew")
        XCTAssertLessThanOrEqual(during, 4, "the roster was laid out \(during) times in 2.5s with nothing "
                                 + "happening — more than the chief's own eyes ticking (0.8s) explains")
    }

    /// Ids are what the lazy stack places by. Unique across the whole long
    /// thread, and the same on a second build of the same thread.
    func testEveryRowOfALongThreadHasAUniqueStableID() async throws {
        let store = await workingDeskWithLongThread()
        guard case .loaded(let screen) = store.thread else { return XCTFail("the thread never opened") }
        let list = TranscriptList(entries: screen.entries, desk: "chief", newFrom: screen.newFrom)
        let first = list.timeline(opened: []).map(\.id)
        let again = list.timeline(opened: []).map(\.id)
        XCTAssertGreaterThan(first.count, 2_000)
        XCTAssertEqual(Set(first).count, first.count, "two rows share an id")
        XCTAssertEqual(first, again, "the same thread built twice gives different ids")
    }
}
