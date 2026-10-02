import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **After a settled transcript takes one scroll-anchor change, how much of the
/// conversation gets measured — and does that number grow with the
/// conversation?**
///
/// This is the question the 2026-09-07 freeze made worth asking. The mechanism
/// proposed for it was specific and it is worth writing down, because it is
/// wrong and the numbers below are why:
///
/// > `takesTheHeightItIsGiven()` puts `alignment: .top` on a flexible frame
/// > around the `ScrollViewReader`. Resolving that guide needs the child's
/// > `ViewDimensions`; the child is a `ScrollView`, whose dimensions are its
/// > content's size; a `ScrollView` measures its content with a **nil** height
/// > proposal (`sizeChildrenIdeally` in the sample), so the `LazyVStack` cannot
/// > be lazy and runs `measureEstimates` over the whole `ForEach`. That pass
/// > writes (`updatingPosition:`), which invalidates the size that caused it —
/// > a closed loop, steady-state, and quadratic in message count.
///
/// Every link in that chain is real except the one that matters. MEASURED on
/// unmodified `main`, off-display at 608x949, over the **shipped**
/// `TranscriptList` — the real `LazyVStack`, the app's own rows:
///
/// ```
/// messages                              10      30      52     120
/// rows realised, settled                10      12      12      12
/// container measures, settled           10      10      10      10
/// container places, settled              8       8       8       8
/// one new message, steady state          1       1       1       1   rows
/// intrinsicContentSize                (172,-1) (172,-1) (172,-1) (172,-1)
/// measurements caused by that query      0       0       0       0
/// ```
///
/// Flat. The bare ideal query moves **nothing** at any length, and reports −1 —
/// `noIntrinsicMetric` — for height, which is `takesTheHeightItIsGiven()`
/// working exactly as it was written to.
///
/// ## The `.frame` is not a hint. Its `idealHeight: 0` is the barrier.
///
/// The argument for replacing the modifier with a one-child `Layout` was that a
/// `.frame` "only hints the ideal; it still forwards nil proposals and still
/// exposes the child's `ViewDimensions`". Measured, same content, same rows,
/// under a deliberate nil height proposal — the only difference is whether the
/// scroll view carries the cap:
///
/// ```
/// messages                        10      30      52     120
/// UNCAPPED, rows realised         10      30      52     120   <- one per message
/// CAPPED   , rows realised         2       2       2       2
/// ```
///
/// The walk is real, it is exactly linear, and `idealHeight: 0` already stops
/// it dead: `_FlexFrameLayout` answers a nil height proposal with the ideal it
/// was given and never asks the child. There is nothing left for a `Layout`
/// barrier to cut.
///
/// ## The good signal, and it is load-bearing here
///
/// A `LazyVStack` parked at the top of a 120-message list realises rows 1…12 and
/// would read beautifully flat while doing nothing. So the steady-state
/// assertion below is that **one new message realises exactly one new row** —
/// which can only happen if the viewport is at the *end* of the list, which can
/// only happen if `proxy.scrollTo(target, anchor: .bottom)` actually landed.
/// Independently confirmed by banding the rows: settled, only the first half is
/// ever measured; after one new message, only the second half is — at 52 and at
/// 120 alike, 14 rows either way.
///
/// ## What goes red, verbatim, and what does not
///
/// **This file is green on unmodified `main`, and that is the finding.** It was
/// calibrated by taking `.takesTheHeightItIsGiven()` off the shipped
/// `ScrollViewReader` and running it again:
///
/// ```
/// testTheIdealSizeQueryDoesNotMeasureTheConversationAtAll : XCTAssertLessThan
///   failed: ("896.0") is not less than ("0.0")  — a 10-message transcript …
///   failed: ("2502.0") …  — a 30-message transcript …
///   failed: ("4275.0") …  — a 52-message transcript …
///   failed: ("9594.0") …  — a 120-message transcript …
/// Executed 6 tests, with 4 failures
/// ```
///
/// 896 → 2,502 → 4,275 → 9,594 points of ideal height, straight-line in message
/// count. That is the real, reproducible, length-dependent fault in this view,
/// and the cap is what closes it.
///
/// **Two limits, stated rather than buried.** The two flatness assertions —
/// settled cost, and the cost of one new message — did **not** go red under
/// that calibration: hosted at a pinned 608x949 the transcript is never handed
/// a nil height proposal, so the lazy stack stays lazy either way. Their
/// instrument is calibrated (`testTheProbeSeesAScrollViewThatMeasuresEveryRow`
/// reads one row body per message on an uncapped tree, 10/30/52/120) but the
/// assertions themselves have no demonstrated red. They pin a property that
/// holds; they are not proof that a property which failed now passes.
///
/// And this harness has no screen. `TranscriptSettlesTests` is the record of
/// five attempts to reproduce the spin off-display, including a `CVDisplayLink`
/// delivering 72 real vsync ticks, and none of them moved a counter. Everything
/// above is arithmetic SwiftUI does without pixels; a fault that needs a
/// compositor is outside what any of it can see.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless windows
/// parked 40,000pt off every display, never ordered front, every wait bounded.
@MainActor
final class TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests: XCTestCase {

    /// His live thread is 52. 120 is where a quadratic would already be
    /// unmissable; 10 is the length at which everything is fine. 45 is here
    /// because `TranscriptList` now scrolls to the newest message on its own
    /// first appearance (see `TranscriptOpensAtTheNewestMessageTests`), and
    /// that changed this file's own curve: **measured** at 5-row steps from 5
    /// to 200, a settled transcript realises exactly its own row count up to
    /// 45 messages, then drops to a flat 24 for every length from 50 to 200.
    /// 45 is the peak of that curve — the worst case, not a guess — and is
    /// kept in the set so this file catches it rather than the four original
    /// points stepping past it.
    static let messageCounts = [10, 30, 45, 52, 120]
    /// The detail column at his window width, with both neighbours out.
    static let paneWidth: CGFloat = 608
    static let windowHeight: CGFloat = 949

    /// **Unchanged — the plateau itself did not move.** Measured at 12
    /// realised rows for every conversation from 30 messages up under the old
    /// top-parked "settled," and still a flat 24 (doubled, for a screenful and
    /// a bit of over-scan) for every length from 50 to 200 under the new
    /// scrolled-to-newest one. What changed is *below* the plateau: scrolling
    /// a completely fresh `LazyVStack` straight to its last row needs to
    /// realise the conversation it is crossing to converge on "bottom," and a
    /// short conversation IS its own screenful — so a 10- or 30-message thread
    /// realising all of itself once, at open, is the cost of opening it, not a
    /// list that stopped being lazy. `testASettledTranscriptRealisesAScreenful
    /// WhateverTheConversationsLength` checks each length against
    /// `max(count, realisedRowsBound)` for exactly that reason.
    static let realisedRowsBound = 24

    override func tearDown() {
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    // MARK: - the good signal comes first

    /// **A green below cannot mean "nothing ran".** The transcript was placed,
    /// rows were realised, and there were more messages than rows realised — so
    /// the list under measurement is a real one being lazily drawn.
    func testTheTranscriptUnderTestWasActuallyDrawn() {
        for count in Self.messageCounts {
            let settled = settle(count, tag: "good\(count)")
            XCTAssertGreaterThan(
                settled.places, 0,
                "at \(count) messages the transcript was placed \(settled.places) times, "
                + "so nothing was laid out at all and every number in this file is "
                + "measuring an empty pane.")
            XCTAssertGreaterThan(
                settled.rows, 0,
                "at \(count) messages not one row body ran, so the LazyVStack realised "
                + "nothing and the flatness below is the flatness of a blank column.")
        }
    }

    // MARK: - the property

    /// **What a settled conversation costs does not grow past the plateau.**
    ///
    /// "Settled" now means scrolled to the newest message (see
    /// `TranscriptOpensAtTheNewestMessageTests`), not merely mounted — so a
    /// short conversation legitimately realises all of itself converging on
    /// "bottom," which is a cost bounded by the conversation's own smallness,
    /// not a list that stopped being lazy. The per-length bound below is
    /// `max(count, realisedRowsBound)` for exactly that reason; the property
    /// this file actually exists to protect — that cost does not keep growing
    /// once a conversation is long enough to matter — is checked separately,
    /// between his real thread and four times past it.
    func testASettledTranscriptRealisesAScreenfulWhateverTheConversationsLength() {
        var curve: [(Int, Int)] = []
        for count in Self.messageCounts {
            curve.append((count, settle(count, tag: "settled\(count)").rows))
        }
        let shown = curve.map { "\($0.0):\($0.1)" }.joined(separator: "  ")

        for (count, rows) in curve {
            let bound = max(count, Self.realisedRowsBound)
            XCTAssertLessThanOrEqual(
                rows, bound,
                "a settled \(count)-message transcript realised \(rows) row bodies, over "
                + "the bound of \(bound) (its own length or \(Self.realisedRowsBound), "
                + "whichever is larger). A list that realises more than its own length, "
                + "or more than a screenful and change once it has one to spare, is a "
                + "list whose LazyVStack has stopped being lazy — which is "
                + "`measureEstimates` walking the whole ForEach, the shape the freeze "
                + "was blamed on. Curve: \(shown)")
        }

        // **The property this file exists for.** Not "short costs the same as
        // long" — a short conversation realising itself whole is the cost of
        // its own smallness, not a freeze risk. It is "a conversation past the
        // point where the cap engages does not keep costing more as it gets
        // longer," checked between his real thread and four times past it —
        // both already on the flat part of the measured curve.
        guard let his = curve.first(where: { $0.0 == 52 }),
              let farPastHis = curve.first(where: { $0.0 == 120 }) else {
            return XCTFail("the 52- and 120-message points were not measured. Curve: \(shown)")
        }
        XCTAssertEqual(
            farPastHis.1, his.1,
            "a settled transcript realised \(his.1) rows at his real 52-message thread and "
            + "\(farPastHis.1) at 120 — more than twice as long. A cost that keeps growing "
            + "past the point where the cap should have engaged is a conversation that "
            + "freezes eventually, just later than it used to. Curve: \(shown)")
    }

    /// **The one this file exists for: one scroll-anchor change, at two very
    /// different conversation lengths.**
    ///
    /// A new message arrives, `onChange` fires `proxy.scrollTo(anchor: .bottom)`
    /// into the lazy list, and the list re-places. That is the exact top of the
    /// 98.8% sample. What it costs must be the same on a 120-message thread as
    /// on a 10-message one.
    func testOneNewMessageCostsTheSameOnAShortThreadAndALongOne() {
        var curve: [(count: Int, rows: Int, ops: Int)] = []
        for count in Self.messageCounts {
            let steady = afterSettlingAtTheBottom(count, tag: "anchor\(count)")
            curve.append((count, steady.rows, steady.measures + steady.places))
        }
        let shown = curve
            .map { "\($0.count):\($0.rows)rows/\($0.ops)ops" }
            .joined(separator: "  ")

        // GOOD SIGNAL: the new row was realised, which is only possible if the
        // scroll anchor actually took the viewport to the end of the list.
        for step in curve {
            XCTAssertGreaterThan(
                step.rows, 0,
                "at \(step.count) messages a new line arrived and no new row body ran. "
                + "Either nothing was drawn, or the viewport is still parked at the top "
                + "of the conversation and proxy.scrollTo never landed — in which case "
                + "this file is measuring a list that is not doing the thing it froze "
                + "doing. Curve: \(shown)")
            XCTAssertGreaterThan(
                step.ops, 0,
                "at \(step.count) messages a new line cost 0 layout operations, so the "
                + "list was never re-placed. Curve: \(shown)")
        }

        guard let shortest = curve.first, let longest = curve.last else {
            return XCTFail("nothing was measured")
        }
        XCTAssertLessThanOrEqual(
            longest.rows, max(shortest.rows, 4) * 2,
            "one new message realised \(shortest.rows) rows on a \(shortest.count)-message "
            + "thread and \(longest.rows) on a \(longest.count)-message one. Every extra "
            + "row is `measureEstimates` walking further into the conversation on each "
            + "line the desk sends, and a desk mid-reply sends them faster than the walk "
            + "finishes. Curve: \(shown)")
        XCTAssertLessThanOrEqual(
            longest.ops, max(shortest.ops, 8) * 2,
            "one new message cost \(shortest.ops) layout operations on a "
            + "\(shortest.count)-message thread and \(longest.ops) on a "
            + "\(longest.count)-message one. Curve: \(shown)")
    }

    /// **The bare ideal-size query — the one AppKit makes on every hosted
    /// column — measures nothing, whatever the conversation's length.**
    ///
    /// This is the query the freeze hypothesis said walks the whole `ForEach`.
    /// It reads 0 at 10 messages and 0 at 120, and answers `noIntrinsicMetric`
    /// for height. Both facts are `takesTheHeightItIsGiven()`.
    func testTheIdealSizeQueryDoesNotMeasureTheConversationAtAll() {
        for count in Self.messageCounts {
            let asked = idealQueryCost(count, tag: "ideal\(count)")
            // One assertion, not two: the container's own measure counter reads
            // 0 with the cap on *and* with it off — the walk happens under the
            // lazy stack's estimate machinery, not through `CountsPlacements` —
            // so asserting on it separately would be an assertion that has
            // never been shown able to fail. It is reported here instead.
            XCTAssertLessThan(
                asked.idealHeight, 0,
                "a \(count)-message transcript reported an ideal height of "
                + "\(asked.idealHeight)pt instead of noIntrinsicMetric "
                + "(\(asked.measures) container measurements). That number is the "
                + "conversation: it grows with every message, AppKit publishes it to "
                + "Auto Layout as required, the split view grows past the window to "
                + "satisfy it and centres the result — which is the two days he spent "
                + "opening the app to an empty window. `takesTheHeightItIsGiven()` is "
                + "what answers a nil height proposal with an ideal of 0 and never asks "
                + "the scroll view. See AColumnFitsTheWindowItIsInTests.")
        }
    }

    // MARK: - calibration: it sees the walk, and it does not invent one

    /// **The walk is real and this probe can see it.** The same content, the
    /// same rows, handed a nil height proposal with the column cap taken off:
    /// exactly one row body per message, at every length. Without this the
    /// flatness above would mean nothing.
    func testTheProbeSeesAScrollViewThatMeasuresEveryRow() {
        var curve: [(Int, Int)] = []
        for count in Self.messageCounts {
            curve.append((count, contentRealised(count, tag: "uncapped\(count)", capped: false)))
        }
        let shown = curve.map { "\($0.0):\($0.1)" }.joined(separator: "  ")

        for (count, rows) in curve {
            XCTAssertGreaterThanOrEqual(
                rows, count,
                "a scroll view with no ideal-height cap, asked for its ideal height over "
                + "\(count) messages, realised \(rows) row bodies — fewer than one per "
                + "message. This probe therefore cannot see `measureEstimates` walking a "
                + "whole conversation at all, and every assertion above is decoration. "
                + "Curve: \(shown)")
        }
    }

    /// The other half: the cap that ships must read flat over the same content,
    /// or the bound above would fail a healthy transcript and be ignored.
    func testTheProbeDoesNotFailTheCapThatShips() {
        for count in Self.messageCounts {
            let rows = contentRealised(count, tag: "capped\(count)", capped: true)
            XCTAssertLessThanOrEqual(
                rows, Self.realisedRowsBound,
                "the shipped `takesTheHeightItIsGiven()` cap let \(rows) row bodies run "
                + "for \(count) messages under a nil height proposal, over this file's "
                + "own bound — so the bound is wrong and would have to be ignored.")
        }
    }

    // MARK: - the subject, and the instrument

    /// **`markdown.parse` is the rows-realised counter.** `MarkdownBody` memoises
    /// per distinct text, so a unique `tag` per measurement makes one count
    /// exactly one row body that actually ran. It needs no wrapper around each
    /// row — which matters, because a layout container per bubble is itself a
    /// change to the tree under test.
    static func entries(_ count: Int, tag: String) -> [ThreadEntry] {
        (1...count).map { index in
            entry(index, tag: tag)
        }
    }

    static func entry(_ index: Int, tag: String) -> ThreadEntry {
        let mine = index % 3 == 0
        let text = "[\(tag)] transcript line \(index) — the runner never got the branch "
            + "it was told to use, so every job it started measured the wrong tree, and "
            + "this sentence is long enough to wrap in the pane he reads it in."
        let message = makeMessage(
            "m\(index)", cursor: String(format: "%04d", index), text: text,
            thread: "direct:chief", author: mine ? "owner" : "chief",
            role: mine ? .owner : .agent, ts: 1_700_000_000 + Double(index) * 90)
        return .said(TranscriptRow(message: message, attribution: .ordinary))
    }

    /// The transcript's own content without `TranscriptList`'s column cap, so
    /// the cap can be put back on or left off by the calibration.
    static func content(_ count: Int, tag: String) -> some View {
        ScrollView {
            LazyVStack(spacing: TranscriptMetrics.rowSpacing) {
                ForEach(entries(count, tag: tag)) { item in
                    if case .said(let row) = item {
                        TranscriptRowView(row: row, openPeerThread: { _ in })
                            .id(item.id)
                    }
                }
            }
            .padding(.horizontal, TranscriptMetrics.horizontalPadding)
            .padding(.vertical, TranscriptMetrics.verticalPadding)
            .countingPlacements(TranscriptList.placementCounter,
                                TranscriptList.measurementCounter)
        }
    }

    // MARK: - harness

    private struct Reading {
        var rows = 0
        var measures = 0
        var places = 0
        var idealHeight: CGFloat = 0
    }

    private struct Rig {
        let host: NSHostingView<AnyView>
        let window: NSWindow
        let container: NSView
    }

    private func rig(_ view: AnyView) -> Rig {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.translatesAutoresizingMaskIntoConstraints = false
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000,
                                width: Self.paneWidth, height: Self.windowHeight),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let container = NSView(frame: NSRect(x: 0, y: 0,
                                             width: Self.paneWidth, height: Self.windowHeight))
        window.contentView = container
        container.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            host.topAnchor.constraint(equalTo: container.topAnchor),
            host.widthAnchor.constraint(equalToConstant: Self.paneWidth),
            host.heightAnchor.constraint(equalToConstant: Self.windowHeight),
        ])
        window.orderBack(nil)
        return Rig(host: host, window: window, container: container)
    }

    /// Bounded, and deliberately so: an unbounded pump in this suite once held
    /// the SwiftPM lock for an hour and stopped ten other files.
    private func pump(_ rig: Rig, _ seconds: TimeInterval = 0.4) {
        rig.container.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        rig.container.layoutSubtreeIfNeeded()
        rig.host.displayIfNeeded()
    }

    private func read() -> Reading {
        let snapshot = Diagnostics.snapshot()
        return Reading(
            rows: snapshot["markdown.parse"] ?? 0,
            measures: snapshot[TranscriptList.measurementCounter] ?? 0,
            places: snapshot[TranscriptList.placementCounter] ?? 0)
    }

    private func teardown(_ rig: Rig) {
        rig.window.orderOut(nil)
        rig.window.contentView = nil
        Diagnostics.isEnabled = false
    }

    /// The shipped transcript, hosted and left to settle.
    private func settle(_ count: Int, tag: String) -> Reading {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        let r = rig(AnyView(TranscriptList(entries: Self.entries(count, tag: tag),
                                           desk: "chief")))
        pump(r)
        let reading = read()
        teardown(r)
        return reading
    }

    /// **Steady state after the anchor has already taken the list to the
    /// bottom.** The *first* new message is the jump — it realises the screenful
    /// at the end of the conversation and is not what a streaming desk costs.
    /// The second is, so that is the one returned.
    private func afterSettlingAtTheBottom(_ count: Int, tag: String) -> Reading {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        var entries = Self.entries(count, tag: tag)
        let r = rig(AnyView(TranscriptList(entries: entries, desk: "chief")))
        pump(r)

        var reading = Reading()
        for round in 1...2 {
            entries.append(Self.entry(count + round, tag: tag))
            Diagnostics.reset()
            r.host.rootView = AnyView(TranscriptList(entries: entries, desk: "chief"))
            pump(r, 0.3)
            reading = read()
        }
        teardown(r)
        return reading
    }

    /// One bare ideal-size query on a settled transcript, and what it cost.
    private func idealQueryCost(_ count: Int, tag: String) -> Reading {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        let r = rig(AnyView(TranscriptList(entries: Self.entries(count, tag: tag),
                                           desk: "chief")))
        pump(r)
        Diagnostics.reset()
        r.host.invalidateIntrinsicContentSize()
        let intrinsic = r.host.intrinsicContentSize
        var reading = read()
        reading.idealHeight = intrinsic.height
        teardown(r)
        return reading
    }

    /// The calibration pair: the same content under a nil height proposal, with
    /// the column cap on or off. `.fixedSize(vertical:)` is the nil proposal —
    /// the same thing `sizeChildrenIdeally` does in the freeze sample.
    private func contentRealised(_ count: Int, tag: String, capped: Bool) -> Int {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        let inner = Self.content(count, tag: tag)
        let subject: AnyView = capped
            ? AnyView(VStack(spacing: 0) {
                inner.takesTheHeightItIsGiven().fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            })
            : AnyView(VStack(spacing: 0) {
                inner.fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            })
        let r = rig(subject)
        pump(r)
        let rows = read().rows
        teardown(r)
        return rows
    }
}
