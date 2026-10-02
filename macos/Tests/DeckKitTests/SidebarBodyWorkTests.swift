import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **What the sidebar recomputes every time anything on the deck publishes.**
///
/// `SidebarChurnTests` stops the roster model manufacturing rebuilds. This file
/// asks the next question: when a rebuild is *real*, what does one evaluation of
/// the sidebar's body cost that it should not?
///
/// The harness is `LayoutSettlesTests`' — a real `NSHostingView` in a real,
/// never-shown, never-fronted window, with the run loop pumped. That harness is
/// blind to AppKit animations (an off-screen window has no display cycle) and
/// says so on its face. It is **not** blind to this: a `@Published` change
/// invalidates the view and SwiftUI re-runs the body whether or not anything is
/// on a screen. What is counted here is arithmetic, not frames.
///
/// The good signal is asserted first and is the whole reason this can be
/// trusted: `sidebar.body` must actually move. A zero taken from a view that
/// never drew is what has fooled this hunt seven times, and it cannot happen
/// here without failing the first assertion.
@MainActor
final class SidebarBodyWorkTests: XCTestCase {

    override func setUp() {
        super.setUp()
        Diagnostics.isEnabled = true
        Diagnostics.reset()
    }

    override func tearDown() {
        Diagnostics.isEnabled = false
        Diagnostics.reset()
        super.tearDown()
    }

    private func roster(_ count: Int = 30) -> RosterPayload {
        var agents: [Agent] = []
        var threads: [ThreadSummary] = []
        for index in 0..<count {
            let name = "agent\(index)"
            var agent = makeAgent(name)
            agent.isPinned = index < 2
            agents.append(agent)
            threads.append(
                makeThread(
                    "direct:\(name)", agent: name, at: Double(index),
                    preview: ThreadPreview(text: "line \(index)"),
                    participants: [DeckOwner.name, name]
                )
            )
        }
        return RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"])
    }

    /// Hosts the sidebar, pumps the run loop, then drives `updates` real roster
    /// changes through the store, pumping after each. Returns the counters that
    /// moved while those updates were being drawn.
    private func countersWhileDriving(
        _ updates: Int,
        hosting: (DeckStore) -> AnyView = { AnyView(SidebarView(store: $0)) }
    ) async -> [String: Int] {
        NSApplication.shared.setActivationPolicy(.prohibited)

        let client = ScriptedDeckClient()
        client.rosterPayload = roster()
        let store = DeckStore(client: client)
        await store.loadRoster()

        let host = NSHostingView(rootView: hosting(store))
        host.frame = NSRect(x: 0, y: 0, width: 300, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 300, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()

        func pump(_ seconds: TimeInterval) {
            let began = Date()
            while Date().timeIntervalSince(began) < seconds {
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
        }
        pump(0.3)

        // The first draw is not the question when we are driving updates: the
        // eleventh is.
        if updates > 0 { _ = Diagnostics.drain() }
        for index in 0..<updates {
            // Typing in the search field, which is the interaction he actually
            // performs and a real change every time — so nothing here is
            // measuring the guard `SidebarChurnTests` put in. This is the cost
            // of a redraw that genuinely had to happen.
            store.search(index.isMultiple(of: 2) ? "agent" : "")
            pump(0.05)
        }
        host.layoutSubtreeIfNeeded()
        let counters = Diagnostics.drain()

        window.orderOut(nil)
        window.contentView = nil
        return counters
    }

    /// A plain stack of the real `AgentRow`, because a `List` in a window
    /// nobody can see does not realise its rows — the first version of this
    /// file read **0** date formats and it was the zero of rows that never
    /// drew, not of rows that stopped formatting.
    ///
    /// **An eighth blind spot, found here and written down rather than passed
    /// over:** an off-screen host does not re-run a row's body when the store
    /// publishes either. Measured — 30 `store.willChange` emissions produced
    /// `sidebar.row.body = 0` while the strip laid out to a real 900pt. So the
    /// two tests below do not count *re*-draws. They count the **first** draw,
    /// where the rule holds just as hard: drawing twenty rows must cost no
    /// hashes and no ICU trips, because both are decided by the roster and the
    /// roster is already walked once per change.
    private struct RowStrip: View {
        @ObservedObject var store: DeckStore

        var body: some View {
            VStack(spacing: 0) {
                if case .loaded(let snapshot) = store.roster {
                    ForEach(snapshot.sections.flatMap(\.rows).prefix(20)) { row in
                        AgentRow(row: row)
                    }
                }
            }
        }
    }

    private func countersWhileDrawingRows() async -> [String: Int] {
        await countersWhileDriving(0) { store in AnyView(RowStrip(store: store)) }
    }

    /// **The good signal, and the probe's own calibration.** If the body never
    /// re-ran, every "0" below is the zero of a view nobody drew — which is
    /// exactly the reading that has made seven previous probes worthless.
    func testTheProbeReallyDoesMakeTheSidebarRedrawItself() async {
        let counters = await countersWhileDriving(10)
        let bodies = counters["sidebar.body"] ?? 0

        XCTAssertGreaterThanOrEqual(
            bodies, 2,
            "the sidebar's body ran \(bodies) times while 10 real roster changes "
            + "were driven through the store. The probe is not redrawing anything, "
            + "so every count it reports is worthless.")
    }

    /// The footer is two rows of text and a face, all derived from one constant:
    /// the owner's name. It was being built inside `body` — an FNV hash over the
    /// name, a shape, a tint, an initials string — so it was recomputed every
    /// time anything at all on the deck published, and handed to the footer
    /// subtree as a freshly-made value each pass.
    func testTheFooterIsNotRebuiltEveryTimeTheSidebarDraws() async {
        let counters = await countersWhileDriving(10)
        let bodies = counters["sidebar.body"] ?? 0
        let makes = counters["sidebar.footer.make"] ?? 0

        XCTAssertGreaterThanOrEqual(bodies, 2, "the probe did not redraw — see the test above")
        XCTAssertEqual(
            makes, 0,
            "the sidebar footer was rebuilt \(makes) times across \(bodies) draws. "
            + "It is derived from the owner's name, which does not change while "
            + "the app is running.")
    }

    /// A face is a shape, a tint and two letters, all decided by the desk's
    /// name — the one field on an agent that cannot be edited. Deriving it in
    /// `body` is an FNV-1a hash over the name's bytes, a split and two string
    /// allocations, per row, per pass, forever, for an answer that is the same
    /// answer every launch.
    func testAFaceIsNotDerivedAfreshEveryTimeTheSidebarDraws() async {
        let counters = await countersWhileDrawingRows()
        let rows = counters["sidebar.row.body"] ?? 0
        let looks = counters["sidebar.avatar.look"] ?? 0

        XCTAssertGreaterThanOrEqual(
            rows, 20,
            "only \(rows) row bodies ran, so a low look count below would be the "
            + "count of rows that never drew")
        XCTAssertEqual(
            looks, 0,
            "\(looks) avatar looks were derived from scratch across \(rows) row "
            + "draws. The name a face is keyed on cannot change.")
    }

    /// `Date.formatted` is ICU. The row runs it twice — once for the short
    /// stamp on screen and once for the spoken one, which is built eagerly for
    /// VoiceOver whether or not anyone is listening — per row, per pass.
    func testARowDoesNotFormatItsDatesEveryTimeTheSidebarDraws() async {
        let counters = await countersWhileDrawingRows()
        let rows = counters["sidebar.row.body"] ?? 0
        let formats = counters["sidebar.row.dateformat"] ?? 0

        XCTAssertGreaterThanOrEqual(
            rows, 20,
            "only \(rows) row bodies ran, so a low format count below would be the "
            + "count of rows that never drew")
        XCTAssertEqual(
            formats, 0,
            "\(formats) date formats across \(rows) row draws. "
            + "Each one is an ICU trip, and the string it produces is decided by "
            + "the roster, which is already built once per change.")
    }
}
