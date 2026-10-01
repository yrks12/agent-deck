import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"open it i still have nothing" — the sidebar is empty and he cannot reach
/// a single desk.**
///
/// The whole product is picking a desk and talking to it. A roster he cannot
/// see is a dead app, and he has lost an evening to exactly that.
///
/// ## Why every detector already here is blind to it
///
/// They count **body evaluations**. On the run he screenshotted, the counters
/// read `sidebar.body=10`, `store.publishRoster=3` and `sidebar.row.body=5` —
/// and five is not a coincidence: his deck has six desks, one of which is the
/// chief and is drawn above the list, so **five is every row he owns**. The
/// data arrived, the rows were built, the bodies ran. He saw nothing. "Did the
/// view do work?" therefore cannot be the question.
///
/// ## What is asked instead
///
/// A row is only a row he can use if it was **realised inside the column and
/// left inside the visible rectangle of it**. `NSView.visibleRect` is the part
/// of a view no ancestor has clipped away, and a `List` on macOS only realises
/// the rows it is about to show — so a roster pushed below the fold, hidden,
/// or laid out into a column taller than the window shows reads zero here while
/// its body count reads exactly the same as a healthy one.
///
/// Calibrated below against two deliberately-broken rosters, so this cannot
/// quietly become decoration.
///
/// ## What this still cannot see, said plainly
///
/// **Ink.** A row can be correctly placed and drawn in nothing — the background
/// colour, zero opacity, or under an opaque layer. That was measured here and
/// abandoned rather than faked: an off-screen `NSHostingView` renders its
/// SwiftUI content through neither `cacheDisplay(in:to:)` nor
/// `CALayer.render(in:)` — a row's own rectangle comes back as **one** colour
/// whether the roster is healthy or at `opacity(0)`. Seeing ink needs a window
/// on a real display, which is his screen, which is not free. `AvatarLookTests`
/// and `SidebarContrastTests` check the colours a row *resolves*; nothing here
/// or anywhere else checks the pixels it *puts down*.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless windows
/// parked 40,000pt off every display, never ordered front, every wait bounded
/// at 0.4s.
@MainActor
final class TheRosterIsOnScreenTests: XCTestCase {

    /// The window's own floor is 560pt (`DeckAppMain`); his was 949. A roster
    /// that only survives a tall window is not one he can rely on.
    static let heights: [CGFloat] = [949, 800, 700, 620, 560]

    /// His deck in shape: six desks, one chief (`atlas` — the only desk with
    /// reports and no boss), nobody pinned. `SidebarSnapshot.build` lifts the
    /// chief out of the list, so five rows are expected: the five his own
    /// diagnostics counted on the run he could not use.
    static let expectedRows = 5

    // MARK: the good signal — every desk is inside the column he is looking at

    func testEveryDeskOnTheRosterIsInsideTheColumnItIsDrawnIn() async {
        let store = await hisDeck()
        for height in Self.heights {
            let hosted = host(roster(store), width: 1208, height: height)
            defer { hosted.close() }
            let rows = hosted.rowViews()

            XCTAssertGreaterThanOrEqual(
                rows.count, Self.expectedRows,
                "\(rows.count) of his \(Self.expectedRows) desks were realised in a "
                + "\(Int(height))pt window. A row the column never realised is a "
                + "row he cannot see or click — that is the empty sidebar.")

            for row in rows {
                let placed = row.convert(row.bounds, to: hosted.host)
                XCTAssertGreaterThanOrEqual(
                    row.visibleRect.height, 20,
                    "a desk was laid out at \(rect(placed)) in a \(Int(height))pt "
                    + "window and only \(String(format: "%.0f", row.visibleRect.height))pt "
                    + "of it survives its own column's clipping")
                XCTAssertTrue(
                    hosted.host.bounds.insetBy(dx: -1, dy: -1).contains(placed),
                    "a desk was laid out at \(rect(placed)), outside the "
                    + "\(rect(hosted.host.bounds)) the window actually shows")
            }
        }
    }

    /// He reported the search field gone as well, which is why this is asked of
    /// the header separately: it and the list are different children of the same
    /// stack, and losing both means the column failed, not the roster.
    func testTheSearchFieldIsInsideTheColumnItIsDrawnIn() async {
        let store = await hisDeck()
        for height in Self.heights {
            let hosted = host(roster(store), width: 1208, height: height)
            defer { hosted.close() }
            let fields = hosted.find { $0 is NSTextField }

            XCTAssertFalse(
                fields.isEmpty,
                "no search field was realised at all in a \(Int(height))pt window")
            for field in fields {
                XCTAssertGreaterThanOrEqual(
                    field.visibleRect.height, 8,
                    "the search field was laid out at "
                    + "\(rect(field.convert(field.bounds, to: hosted.host))) and is "
                    + "clipped out of the \(Int(height))pt column it is drawn in")
            }
        }
    }

    // MARK: calibration — the measurement has to be able to fail

    /// A roster pushed down out of its own column. Measured: the `List` stops
    /// realising rows entirely, which is the reading a healthy roster can never
    /// produce, and is what the assertion above is built on.
    func testTheProbeSeesARosterPushedOutOfItsColumn() async {
        let store = await hisDeck()
        let hosted = host(
            AnyView(NavigationSplitView {
                SidebarView(store: store).offset(y: 900)
            } detail: { Color.clear }),
            width: 1208, height: 949)
        defer { hosted.close() }

        XCTAssertLessThan(
            hosted.rowViews().count, Self.expectedRows,
            "a roster shoved 900pt below the top of a 949pt window still measured "
            + "as \(hosted.rowViews().count) desks on screen, so this probe cannot "
            + "see an off-column roster and the test above is worthless")
    }

    /// The other way a roster goes missing while its bodies still run: the
    /// column is there, the rows are built, and none of it is in the tree.
    func testTheProbeSeesARosterThatIsNotOnScreenAtAll() async {
        let store = await hisDeck()
        let hosted = host(
            AnyView(NavigationSplitView {
                SidebarView(store: store).hidden()
            } detail: { Color.clear }),
            width: 1208, height: 949)
        defer { hosted.close() }

        XCTAssertLessThan(
            hosted.rowViews().count, Self.expectedRows,
            "a hidden roster measured as \(hosted.rowViews().count) desks on "
            + "screen, so this probe cannot tell a drawn sidebar from one that "
            + "is not there")
    }

    // MARK: the sweep — the sidebar is one column of three

    /// The same question of the inspector, which is the other column that
    /// hosts real AppKit views: is anything it draws laid out outside its own
    /// visible rectangle?
    ///
    /// **The conversation column cannot be asked this, and that is a finding
    /// rather than an omission.** Measured here: an `NSHostingView` of
    /// `ThreadView` realises **zero** AppKit-backed subviews — the transcript
    /// is a `ScrollView` of SwiftUI text and the composer draws its own box, so
    /// there is nothing in the view tree to ask where it ended up. Nothing in
    /// this suite measures the vertical placement of a message;
    /// `TranscriptFitsItsColumnTests` reaches them through a `GeometryReader`
    /// and measures their **width** only. A message laid out below the bottom
    /// of the pane would look exactly like a message that has scrolled, and no
    /// test here can tell those apart.
    func testTheInspectorIsAlsoInsideTheColumnItIsDrawnIn() async {
        let store = await hisDeck()
        store.select(agent: "listing-closer", threadID: "direct:listing-closer")
        await store.settle()

        for height in [949.0 as CGFloat, 560] {
            let hosted = host(
                AnyView(SettingsPanelView(store: store).frame(width: 620, height: height)),
                width: 620, height: height)
            defer { hosted.close() }
            let realised = hosted.find {
                $0 is NSTextField || $0 is NSTextView || $0 is NSScrollView
            }

            XCTAssertFalse(
                realised.isEmpty,
                "the inspector realised nothing at all at a \(Int(height))pt "
                + "column, so the check below would pass on an empty panel")
            for view in realised {
                XCTAssertGreaterThan(
                    view.visibleRect.height, 0,
                    "the inspector: a \(type(of: view)) was laid out at "
                    + "\(rect(view.convert(view.bounds, to: hosted.host))) and is "
                    + "clipped entirely out of the \(Int(height))pt column it lives in")
            }
        }
    }

    // MARK: harness

    private func roster(_ store: DeckStore) -> AnyView {
        AnyView(NavigationSplitView { SidebarView(store: store) } detail: { Color.clear })
    }

    private func rect(_ r: NSRect) -> String {
        String(format: "(%.0f, %.0f) %.0fx%.0f", r.origin.x, r.origin.y, r.width, r.height)
    }

    /// His deck as `GET /v1/agents` returns it.
    private func hisDeck() async -> DeckStore {
        func desk(_ name: String, _ title: String, boss: String?, reports: [String] = []) -> Agent {
            var agent = Agent(name: name, title: title, detail: "", section: "Work")
            agent.boss = boss
            agent.reports = reports
            agent.threadID = "direct:\(name)"
            agent.preview = "the last thing this desk said"
            agent.lastActivityAt = Date(timeIntervalSince1970: 1_788_690_000)
            return agent
        }
        let client = ScriptedDeckClient()
        client.rosterPayload = AgentsResponse.roster(from: [
            desk("atlas", "COS", boss: nil, reports: ["listing-closer", "acme-lead"]),
            desk("listing-closer", "Sales", boss: "atlas"),
            desk("acme-lead", "Acme Lead", boss: "atlas"),
            desk("acme-growth", "Acme Growth", boss: "atlas"),
            desk("acme-product", "Acme Product", boss: "atlas"),
            desk("new-hire-82d9ab", "New", boss: nil),
        ])
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        return store
    }

    /// A real window nobody can see, laid out and settled. The wait is bounded:
    /// an unbounded pump here once held the SwiftPM lock for an hour and made
    /// every later build look like a slow compile.
    private func host(_ view: AnyView, width: CGFloat, height: CGFloat) -> Hosted {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: width, height: height)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < 0.4 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        host.layoutSubtreeIfNeeded()
        return Hosted(host: host, window: window)
    }

    struct Hosted {
        let host: NSHostingView<AnyView>
        let window: NSWindow

        func close() {
            window.orderOut(nil)
            window.contentView = nil
        }

        func find(_ matches: (NSView) -> Bool) -> [NSView] {
            var found: [NSView] = []
            func walk(_ view: NSView) {
                if matches(view) { found.append(view) }
                view.subviews.forEach(walk)
            }
            walk(host)
            return found
        }

        /// A `List` row on macOS is a real `NSTableCellView` subclass, which is
        /// the only reason a row can be found at all. If SwiftUI ever stops
        /// backing them the count drops to zero and the good-signal assertion
        /// fails loudly, rather than this quietly measuring nothing.
        func rowViews() -> [NSView] {
            find { String(describing: type(of: $0)).contains("ListTableCellView") }
        }
    }
}
