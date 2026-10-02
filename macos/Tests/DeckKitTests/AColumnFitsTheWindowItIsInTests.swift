import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"open it i still have nothing" — a window whose columns are bigger than
/// the window, on both axes, and centred inside it.**
///
/// The Accessibility tree of the running app, on his display, after the first
/// round of this fix had landed:
///
/// ```
/// AXWindow        @(304,  33)  1208 x  949
///  └ AXSplitGroup @(304,  33)  1208 x  949   <- height, fixed
///     ├ AXGroup   @(242,-473)   300 x 2013   <- roster column, still over
///     │   └ AXScrollArea @(242,-284) 300 x 1732
///     └ AXGroup   @(234,-481)  1347 x 2029   <- detail column, still over
/// ```
///
/// Read the **x** coordinates. The window starts at 304 and both columns start
/// at 242 and 234 — *left of the window they are in*. A column wider than its
/// column is centred in it exactly as a column taller than the window is
/// centred in the window, so it hangs off both edges: which is why he could see
/// approval cards in the middle and the inspector was clipped past the right
/// edge of the screen entirely.
///
/// ## The mechanism, one sentence
///
/// Each column of a `NavigationSplitView` is hosted in its own `NSHostingView`
/// inside an `NSSplitViewItem`, and that hosting view publishes SwiftUI's
/// **ideal size** to AppKit as its `intrinsicContentSize`. Auto Layout treats an
/// intrinsic size as a requirement, grows the enclosing `NSSplitView` until it
/// is satisfied, and centres the oversized result. Nothing about that is
/// specific to height.
///
/// ## Why the first version of this file passed over a broken build
///
/// It measured `TranscriptList` and `SettingsPanelView` **in isolation**, on one
/// axis, and reported 622 tests / 0 failures against a build he could not use. A
/// check that passed on the parts had not checked the whole. So this file now
/// measures the **column roots as `DeckRootView` actually composes them**, on
/// both axes, over a store loaded the way the app loads it. Measured on
/// unmodified `main`, verbatim:
///
/// ```
/// the inspector column: ideal width 721pt in a 260pt column
/// the roster column:    ideal width 265pt in a 260pt column
/// ```
///
/// The 721 is one sentence. `NotificationDelivery.caveat` — *"Saved on the
/// deck, but nothing delivers notifications yet — there is no push service."* —
/// is a `Text`, and a `Text` answers "how wide would you like to be?" with the
/// width of the **whole sentence on one line**: 661pt, which the enclosing
/// `Form` rounds up to 721. `.fixedSize(horizontal: false, vertical: true)`
/// pins the *vertical* axis and leaves that ideal width exactly where it was.
/// It is not one bad view — every wrapping sentence in either column does this,
/// which is why the fix is at the boundary rather than at any one of them.
///
/// ## The good signal comes first
///
/// A column measured over a spinner fits beautifully. `testTheDeckUnderTestIs…`
/// and the guards at the top of each case fail loudly if the roster or the
/// thread never loaded, so a green here cannot mean "there was nothing to
/// measure".
///
/// ## Calibrated, both ways, on both axes
///
/// A view built to over-report must be caught; a view content to fill whatever
/// it is given must not be failed. Without both, a green here would mean
/// nothing.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless windows
/// parked 40,000pt off every display, never ordered front, every wait bounded
/// at 0.4s.
@MainActor
final class AColumnFitsTheWindowItIsInTests: XCTestCase {

    /// His window was 949 tall. `DeckAppMain` lets him make it 560. A column
    /// that only fits the tall one empties the window when he resizes it.
    static let windowHeights: [CGFloat] = [949, 560]

    /// His conversation with `atlas` on the run he screenshotted.
    static let messageCount = 52
    /// His deck: seven desks over two sections.
    static let deskNames = ["chief", "atlas", "scribe", "ledger", "sentry", "forge", "beacon"]

    // MARK: - calibration — the probe can see the fault, and does not invent it

    func testTheProbeSeesAColumnThatDeliberatelyOverReportsItsHeight() {
        let tall = ScrollView { Color.clear.frame(height: 8_000) }
        let ideal = idealSize(AnyView(tall), inColumnOfWidth: 630)

        XCTAssertGreaterThan(
            ideal.height, 949,
            "a column whose content is 8,000pt tall reported \(Int(ideal.height))pt of "
            + "ideal height, so this probe cannot see an over-tall column at all and "
            + "every height assertion in this file is decoration.")
    }

    func testTheProbeSeesAColumnThatDeliberatelyOverReportsItsWidth() {
        let wide = VStack { Color.clear.frame(width: 3_000, height: 10) }
        let ideal = idealSize(AnyView(wide), inColumnOfWidth: 300)

        XCTAssertGreaterThan(
            ideal.width, 300,
            "a column whose content is 3,000pt wide reported \(Int(ideal.width))pt of "
            + "ideal width, so this probe cannot see an over-wide column at all and "
            + "every width assertion in this file is decoration — which is the axis "
            + "that clipped his inspector off the right edge of the screen.")
    }

    func testTheProbeDoesNotCryWolfOverAColumnThatFits() {
        let healthy = VStack { Text("a column that takes what it is given") }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        let ideal = idealSize(AnyView(healthy), inColumnOfWidth: 300)

        XCTAssertLessThanOrEqual(
            ideal.height, 560,
            "a healthy column reported \(Int(ideal.height))pt of ideal height, so this "
            + "probe fails healthy columns and would have to be ignored.")
        XCTAssertLessThanOrEqual(
            ideal.width, 300,
            "a healthy column reported \(Int(ideal.width))pt of ideal width in a 300pt "
            + "column, so this probe fails healthy columns and would have to be ignored.")
    }

    // MARK: - the good signal

    func testTheDeckUnderTestIsTheOneHeIsLookingAt() async {
        let store = await hisDeck()

        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("the roster never loaded, so every column measured in this "
                           + "file is measuring a spinner and fits for the wrong reason")
        }
        XCTAssertEqual(snapshot.allRows.count, Self.deskNames.count,
                       "his deck has \(Self.deskNames.count) desks and this one has "
                       + "\(snapshot.allRows.count) — the roster column below is not "
                       + "being asked the question he asked it")
        XCTAssertGreaterThan(snapshot.sections.count, 1,
                             "one section is not a roster with section headers in it")
        guard case .loaded(let screen) = store.thread else {
            return XCTFail("the thread never loaded, so the conversation column below "
                           + "is measuring an empty pane")
        }
        XCTAssertEqual(screen.rows.count, Self.messageCount,
                       "the conversation under test is \(screen.rows.count) messages "
                       + "long, not \(Self.messageCount) — the height of this column is "
                       + "content-dependent, which is the whole defect")
        XCTAssertNotNil(store.settingsAgent,
                        "no desk is selected, so the inspector column below is drawing "
                        + "'No agent selected' rather than the panel he sees")
    }

    // MARK: - the guard on this file's own harness

    /// Twenty-seven hosted windows are made by the column tests below. None may
    /// leave a thread behind: 64 of them once hung the whole suite.
    func testHostingManyColumnsLeavesNoThreadsOrWindowsBehind() {
        ThreadLeakGuard.assertNoLeak {
            for _ in 0..<12 {
                _ = idealSize(AnyView(Text("a column")), inColumnOfWidth: 300)
            }
        }
    }

    // MARK: - the defect, at the boundary that publishes it

    /// **The three roots that each get an `NSHostingView` in an
    /// `NSSplitViewItem`**, exactly as `DeckRootView` composes them: the
    /// sidebar closure, the detail closure with its inspector attached, and the
    /// inspector's own content, which is a column in its own right.
    func testEveryColumnRootFitsTheWindowItIsPlacedIn() async {
        let store = await hisDeck()
        guard case .loaded(let snapshot) = store.roster, snapshot.allRows.count == Self.deskNames.count,
              case .loaded(let screen) = store.thread, screen.rows.count == Self.messageCount else {
            return XCTFail("the deck under test never loaded — see "
                           + "testTheDeckUnderTestIsTheOneHeIsLookingAt")
        }

        for column in Self.columnRoots(store: store) {
            for columnWidth in column.widths {
                let ideal = idealSize(column.root, inColumnOfWidth: columnWidth)

                XCTAssertLessThanOrEqual(
                    ideal.width, columnWidth,
                    "\(column.name) asks for \(Int(ideal.width))pt of width in a "
                    + "\(Int(columnWidth))pt column. AppKit publishes that as the hosted "
                    + "column's intrinsic content size, Auto Layout treats it as required "
                    + "and centres the oversized column in the space it has — which is why "
                    + "his columns began at x=242 and x=234 inside a window that begins at "
                    + "x=304, and why the inspector was clipped off the right edge of the "
                    + "screen.")

                for windowHeight in Self.windowHeights {
                    XCTAssertLessThanOrEqual(
                        ideal.height, windowHeight,
                        "\(column.name) asks for \(Int(ideal.height))pt of height in a "
                        + "\(Int(windowHeight))pt window. A column that reports more than it "
                        + "was given is published to AppKit as a required intrinsic height, "
                        + "the split view grows past the window to satisfy it and centres the "
                        + "result — every row of this column then sits off the top or the "
                        + "bottom of the screen and he sees an empty app.")
                }
            }
        }
    }

    /// **The detail column has a second occupant.** The unsaved "+" thread owns
    /// the pane while it exists, and it is a `ScrollView` full of wrapping
    /// sentences — 650pt of ideal width, measured. It is checked *through the
    /// column root* rather than on its own, because on its own it is not a
    /// column, and what a view reports in isolation is not what AppKit is told.
    func testTheColumnStillFitsWhileTheHiringPaneOwnsIt() async {
        let store = await hisDeck()
        store.beginNewAgent()
        guard store.pending != nil else {
            return XCTFail("the hiring pane never opened, so this is measuring the "
                           + "ordinary conversation a second time")
        }

        let column = AnyView(DeckRootView.conversationColumn(
            store: store, inspector: .constant(true)))
        for columnWidth in Self.detailWidths {
            let ideal = idealSize(column, inColumnOfWidth: columnWidth)
            XCTAssertLessThanOrEqual(
                ideal.width, columnWidth,
                "with the hiring pane open the conversation column asks for "
                + "\(Int(ideal.width))pt of width in a \(Int(columnWidth))pt column")
            for windowHeight in Self.windowHeights {
                XCTAssertLessThanOrEqual(
                    ideal.height, windowHeight,
                    "with the hiring pane open the conversation column asks for "
                    + "\(Int(ideal.height))pt of height in a \(Int(windowHeight))pt window")
            }
        }
    }

    // MARK: - the columns, and the widths they are actually given

    struct ColumnRoot {
        let name: String
        let root: AnyView
        /// Every width the user can put this column at. A column that only fits
        /// at its ideal width empties itself the moment he drags the divider.
        let widths: [CGFloat]
    }

    /// The sidebar is 260…360 by `Theme.sidebarWidth`; the inspector 260…340 by
    /// `Theme.inspectorWidth`. The narrowest is the one that matters — that is
    /// the width the column is *given*, and anything wider hangs off it.
    static func columnWidths(_ range: ClosedRange<CGFloat>, ideal: CGFloat) -> [CGFloat] {
        [range.lowerBound, ideal, range.upperBound]
    }

    /// The detail column gets what the other two leave it. Narrowest first:
    /// the app's own 900pt window floor with both neighbours dragged to their
    /// widest. That is the width at which an over-reporting detail column bites
    /// soonest, and it is reachable with two drags.
    static let detailWidths: [CGFloat] = [
        900 - Theme.sidebarWidth.upperBound - Theme.inspectorWidth.upperBound,   // 200
        900 - Theme.sidebarWidth.lowerBound - Theme.inspectorWidth.lowerBound,   // 380
        1208 - 300 - 300,                                                        // 608, his
    ]

    /// **`DeckRootView`'s own column roots**, called rather than rebuilt. The
    /// previous version of this file assembled its own copy of the columns, so
    /// it could only ever check the assembly it had written down; these are the
    /// three expressions the app hands to `NavigationSplitView`.
    static func columnRoots(store: DeckStore) -> [ColumnRoot] {
        [
            ColumnRoot(
                name: "the roster column",
                root: AnyView(DeckRootView.rosterColumn(store: store)),
                widths: columnWidths(Theme.sidebarWidth, ideal: 300)),
            ColumnRoot(
                name: "the conversation column",
                root: AnyView(DeckRootView.conversationColumn(
                    store: store, inspector: .constant(true))),
                widths: detailWidths),
            ColumnRoot(
                name: "the inspector column",
                root: AnyView(DeckRootView.inspectorColumn(store: store)),
                widths: columnWidths(Theme.inspectorWidth, ideal: 300)),
        ]
    }

    // MARK: - harness

    /// **The ideal size SwiftUI reports for a view hosted as a window column** —
    /// the two numbers AppKit takes as that column's intrinsic content size.
    ///
    /// Arithmetic, not drawn: a screenless window never begins a graph
    /// transaction, so anything that waits for pixels reads the same over a
    /// healthy build and a broken one. `fittingSize` and `intrinsicContentSize`
    /// are worked out, and they answer identically off-display.
    ///
    /// Height is read with the width **pinned by a constraint**, because that is
    /// how a column is placed: told its width, asked its height. Width is read
    /// from `intrinsicContentSize`, which is unclamped by that constraint and is
    /// literally the value published to Auto Layout — `fittingSize.width` would
    /// only ever hand back the constant it was given and could never fail.
    private func idealSize(_ view: AnyView, inColumnOfWidth width: CGFloat) -> CGSize {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.translatesAutoresizingMaskIntoConstraints = false
        let window = NSWindow.offscreenForTest(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: 949))
        let container = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 949))
        window.contentView = container
        container.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            host.topAnchor.constraint(equalTo: container.topAnchor),
            host.widthAnchor.constraint(equalToConstant: width),
        ])
        window.orderBack(nil)
        container.layoutSubtreeIfNeeded()
        // Bounded, and deliberately so: an unbounded pump in this suite once
        // held the SwiftPM lock for an hour and stopped ten other files.
        let began = Date()
        while Date().timeIntervalSince(began) < 0.4 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        container.layoutSubtreeIfNeeded()
        let intrinsic = host.intrinsicContentSize
        let fitting = host.fittingSize
        window.orderOut(nil)
        window.contentView = nil
        window.close()
        // `noIntrinsicMetric` (−1) is the healthy answer: it means the column
        // asked AppKit for nothing at all, which is the whole point of the fix.
        // Reported as 0 so it can never read as "one point over".
        return CGSize(
            width: max(0, intrinsic.width),
            height: max(max(0, intrinsic.height), fitting.height))
    }

    // MARK: - his deck, as the app receives it

    private func hisConversation() -> [Message] {
        (1...Self.messageCount).map { index in
            makeMessage(
                "m\(index)", cursor: String(format: "%04d", index),
                text: "transcript line \(index), long enough to wrap across more than "
                    + "one line in a thousand-point pane",
                thread: "direct:chief")
        }
    }

    /// Seven desks over two sections, and a 52-message conversation open in the
    /// first of them — the deck he photographed.
    private func hisDeck() async -> DeckStore {
        let client = ScriptedDeckClient()
        var agents: [Agent] = []
        var threads: [ThreadSummary] = []
        for (index, name) in Self.deskNames.enumerated() {
            var agent = makeAgent(name, section: index < 4 ? "Work" : "Ops")
            agent.state = .working
            agents.append(agent)
            threads.append(makeThread("direct:\(name)", agent: name, at: Double(index)))
        }
        client.rosterPayload = RosterPayload(
            agents: agents, threads: threads, sectionOrder: ["Work", "Ops"])
        client.pages = [
            MessagePage(
                threadID: "direct:chief", messages: hisConversation(),
                isReadOnly: false, participants: ["owner", "chief"])
        ]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        return store
    }
}
