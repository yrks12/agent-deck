import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **How much layout the conversation costs, per message, and whether that
/// number grows.**
///
/// The 2026-09-07 freeze is not a view being rebuilt and it is not a body
/// running twice. MEASURED on the running process while it was stuck
/// (`dist/freeze-sample-1212.txt`): 4898 of 4898 main-thread samples inside one
/// `NSHostingView.beginTransaction`, 1087 frames in
/// `LayoutEngineBox.explicitAlignment`, `_FlexFrameLayout.placement` recursing
/// past the sampler's depth limit. What blew up is the number of **layout
/// operations for one transaction** — so that is what this file counts.
///
/// Every other detector in this repo counts something else and stayed green
/// through the whole outage: `ListChurnTests`, `SidebarChurnTests` and
/// `InspectorIsQuietTests` count **body evaluations**, `LayoutSettlesTests`
/// counts **root layout passes**, and `TranscriptList`'s own shipped counter
/// wraps the container rather than the rows — measured at 10 measures and 8
/// placements for a 10-message thread and *exactly the same* for a 52-message
/// one, which is a counter that cannot see message count at all.
///
/// ## The good signal comes first, and it is not optional here
///
/// A `LazyVStack` off a screen realises only the rows in view: measured, its
/// counter reads 33 operations for 10 messages, 30, 52 and 80 alike. A test
/// that measured that would read "flat" over a build that is quadratic and pass.
/// So the subject is a plain `VStack` — **every** row is realised and measured —
/// and `testEveryMessageWasActuallyLaidOut` fails loudly if the count of
/// operations ever falls below one per message. Zero is what nothing-ran looks
/// like, and it must never read as a pass.
///
/// ## …and that substitution is right for the cost-per-row question only
///
/// `testTopAlignmentCostsExactlyNothing` used to share it, and could not have.
/// Resolving an alignment guide on a child needs that child's `ViewDimensions`;
/// over a plain `VStack` every row is already measured, so the query is a cache
/// hit and free **by construction**, whatever is true of the shipped tree. Over
/// a `LazyVStack` it is the one thing that could become a `measureEstimates`
/// walk. That case now measures a real `LazyVStack` and reads the same answer
/// — 7 measures / 5 places either way at 10, 30, 52 and 120 — so the conclusion
/// stands on the tree that ships rather than on a stand-in. How much a
/// conversation costs as it grows, over the lazy tree, is
/// `TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests`.
///
/// ## What it found, and what it did not
///
/// Measured on this branch, one forced layout of the shipped `TranscriptRowView`
/// under the shipped wrapper chain:
///
/// ```
/// messages          10      30      52      80
/// layout ops       130     390     676    1040
/// per message    13.00   13.00   13.00   13.00
/// ```
///
/// Exactly linear, to the operation. **This file does not go red on `main`**,
/// and that is a finding rather than a formality: see
/// `testTopAlignmentCostsExactlyNothing` for the measurement that clears the
/// column fix, and `TheDiagnosticSpeaksWhileTheMainThreadIsWedgedTests` for the
/// detector that does go red, because the fault reaches a screen and this
/// harness cannot.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless windows
/// parked 40,000pt off every display, never ordered front, every wait bounded.
@MainActor
final class TheTranscriptsLayoutWorkIsLinearTests: XCTestCase {

    /// His live thread is 52. The others bracket it, because one number cannot
    /// tell a line from a curve.
    static let messageCounts = [10, 30, 52, 80]
    /// The detail column at his window width, with both neighbours out.
    static let paneWidth: CGFloat = 608
    static let windowHeight: CGFloat = 949

    /// Measured 13.00 flat on the shipped tree and 21.00 on the calibration
    /// tree below, so the bound sits between them with room on both sides: 38%
    /// of headroom over a healthy transcript, and it still catches a tree that
    /// costs 62% more per message than this one does.
    static let opsPerMessageBound = 18.0

    override func tearDown() {
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    // MARK: - the good signal

    func testEveryMessageWasActuallyLaidOut() {
        for count in Self.messageCounts {
            let ops = layoutOps(Self.shippedTranscript(count))
            XCTAssertGreaterThanOrEqual(
                ops, count,
                "\(count) messages produced \(ops) layout operations — fewer than one "
                + "each, so rows were never realised and every number in this file is "
                + "measuring an empty pane. That is what nothing-ran looks like and it "
                + "must never read as a pass.")
        }
    }

    // MARK: - the defect this file exists for

    /// **Linear in message count, or the conversation stops being usable as it
    /// grows** — which is the shape of the fault he lives with: it is fine on a
    /// short thread and it froze on his 52-message one.
    func testTheLayoutCostPerMessageDoesNotGrowWithTheConversation() {
        var perMessage: [(Int, Double)] = []
        for count in Self.messageCounts {
            let ops = layoutOps(Self.shippedTranscript(count))
            perMessage.append((count, Double(ops) / Double(count)))
        }

        let curve = perMessage
            .map { "\($0.0):\(String(format: "%.2f", $0.1))" }
            .joined(separator: "  ")

        for (count, cost) in perMessage {
            XCTAssertLessThanOrEqual(
                cost, Self.opsPerMessageBound,
                "at \(count) messages the transcript costs \(String(format: "%.2f", cost)) "
                + "layout operations per message, over the bound of "
                + "\(Self.opsPerMessageBound). Curve: \(curve)")
        }

        guard let smallest = perMessage.first, let largest = perMessage.last else {
            return XCTFail("nothing was measured")
        }
        XCTAssertLessThanOrEqual(
            largest.1, smallest.1 * 1.5,
            "the cost per message grew from \(String(format: "%.2f", smallest.1)) at "
            + "\(smallest.0) messages to \(String(format: "%.2f", largest.1)) at "
            + "\(largest.0) — a conversation that costs more per line the longer it "
            + "gets is one that freezes at some length, and his is 52. Curve: \(curve)")
    }

    /// **The measurement that must be read before anyone reverts the column
    /// fix — and it is made over a `LazyVStack`, deliberately, unlike every
    /// other case in this file.**
    ///
    /// `takesTheHeightItIsGiven` / `takesTheSizeItIsGiven` use a non-default
    /// `alignment:`, and `LayoutEngineBox.explicitAlignment` is 1087 frames of
    /// the freeze sample — so the frames look guilty.
    ///
    /// The first version of this case compared two `VStack`s and concluded
    /// "13.00 ops/message either way". **That comparison could not have
    /// settled the question.** Resolving an alignment guide on a child needs
    /// that child's `ViewDimensions`; over a plain `VStack` every row is
    /// already measured, so the query is a cache hit and free *by
    /// construction*, whatever the truth about the shipped tree. Over a
    /// `LazyVStack` it is the one thing that could turn into a
    /// `measureEstimates` walk of the whole `ForEach`. The substitution is
    /// right for the linearity cases above and wrong for this one, so this one
    /// now measures the lazy tree.
    ///
    /// Measured, off-display at 608x949, same content, only the alignment
    /// argument changed:
    ///
    /// ```
    ///                       10 msgs  30 msgs  52 msgs  120 msgs
    /// alignment: .top        7 / 5    7 / 5    7 / 5    7 / 5    measures/places
    /// alignment: default     7 / 5    7 / 5    7 / 5    7 / 5
    /// rows realised          10       14       14       14
    /// ```
    ///
    /// Identical to the operation, at every count. The conclusion stands and
    /// now rests on the tree that ships: removing the argument would cost him
    /// the fix — a column that fits the window it is in, after two days of a
    /// blank one — and buy nothing.
    func testTopAlignmentCostsExactlyNothing() {
        for count in Self.messageCounts {
            let aligned = lazyTranscriptCost(count, aligned: true)
            let centred = lazyTranscriptCost(count, aligned: false)

            // GOOD SIGNAL FIRST. A lazy stack that realised nothing costs the
            // same either way and would read as a pass over any tree at all.
            XCTAssertGreaterThan(
                aligned.rows, 0,
                "at \(count) messages not one row body ran, so this comparison is "
                + "between two empty panes and says nothing about alignment guides.")
            XCTAssertGreaterThan(
                aligned.ops, 0,
                "at \(count) messages the transcript was never laid out at all.")

            XCTAssertEqual(
                aligned.ops, centred.ops,
                "at \(count) messages the transcript costs \(aligned.ops) layout "
                + "operations with a top alignment and \(centred.ops) without one, over a "
                + "real LazyVStack. If those ever differ, the alignment guide really is "
                + "being resolved at a cost — a measureEstimates walk of the whole ForEach "
                + "on every placement — and the column fix needs a different way to pin "
                + "content to the top: a zero-spacing VStack with a Spacer(minLength: 0). "
                + "They are equal today.")
            XCTAssertEqual(
                aligned.rows, centred.rows,
                "at \(count) messages the top-aligned transcript realised \(aligned.rows) "
                + "row bodies and the centred one \(centred.rows). A difference here is "
                + "the alignment argument forcing the lazy stack to walk further into the "
                + "conversation than it needs to.")
        }
    }

    // MARK: - calibration: it catches a tree built to blow up, and only that

    /// Twelve flexible frames deep, each one asking its child where its top-left
    /// corner is. This is the shape the freeze sample recurses through, built on
    /// purpose, and it must be caught — otherwise the bound above is decoration.
    ///
    /// It is also the measurement that says how far this class *can* go before
    /// SwiftUI stops paying for it: 13.00 per message flat, 21.00 from four
    /// frames deep, and 21.00 at eight and at twelve. The cost of nesting
    /// flexible frames is real, bounded, and saturates — it does not compound.
    func testTheProbeCatchesATreeThatCostsMorePerMessage() {
        let count = 30
        let cost = Double(layoutOps(Self.shippedTranscript(count, extraFlexibleFrames: 12)))
            / Double(count)

        XCTAssertGreaterThan(
            cost, Self.opsPerMessageBound,
            "a transcript buried twelve flexible frames deep cost "
            + "\(String(format: "%.2f", cost)) layout operations per message, under the "
            + "bound of \(Self.opsPerMessageBound) — so this probe cannot see an "
            + "expensive tree at all and every assertion above is decoration.")
    }

    func testTheProbeDoesNotFailAHealthyTranscript() {
        let count = 52
        let cost = Double(layoutOps(Self.shippedTranscript(count))) / Double(count)

        XCTAssertLessThan(
            cost, Self.opsPerMessageBound,
            "the shipped transcript cost \(String(format: "%.2f", cost)) layout "
            + "operations per message and would be failed by this file's own bound, so "
            + "the bound is wrong and would have to be ignored.")
    }

    // MARK: - the subject: the app's own row view, under the app's own wrappers

    /// `nil` alignment means "leave the argument off", which is what the
    /// alignment comparison needs; `extraFlexibleFrames` is the calibration.
    static func shippedTranscript(
        _ count: Int, alignment: Alignment? = .top, extraFlexibleFrames: Int = 0
    ) -> AnyView {
        // A plain `VStack`, not the shipped `LazyVStack`, and deliberately: a
        // lazy stack off a screen realises only the rows in view, so its op
        // count is flat over message count for the wrong reason. This is the
        // conservative subject — every row is measured — and it is the app's own
        // `TranscriptRowView` inside it, not a stand-in.
        let list = ScrollView {
            VStack(spacing: TranscriptMetrics.rowSpacing) {
                ForEach(rows(count)) { row in
                    CountsPlacements(placements: placeCounter, measurements: measureCounter) {
                        TranscriptRowView(row: row, openPeerThread: { _ in })
                    }
                    .id(row.id)
                }
            }
            .padding(.horizontal, TranscriptMetrics.horizontalPadding)
            .padding(.vertical, TranscriptMetrics.verticalPadding)
        }

        var capped: AnyView
        if let alignment {
            capped = AnyView(list.frame(
                minHeight: 0, idealHeight: 0, maxHeight: .infinity, alignment: alignment))
        } else {
            capped = AnyView(list.frame(minHeight: 0, idealHeight: 0, maxHeight: .infinity))
        }
        for _ in 0..<extraFlexibleFrames {
            capped = AnyView(HStack(spacing: 0) {
                VStack(spacing: 0) { capped; Spacer(minLength: 0) }
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading))
        }
        return capped
    }

    static let placeCounter = "transcript.row.place"
    static let measureCounter = "transcript.row.measure"

    // MARK: - the lazy subject, for the one question a VStack cannot answer

    /// The transcript's content as the app actually builds it — a real
    /// `LazyVStack` — capped the way `TranscriptList` caps it, with or without
    /// the alignment argument.
    ///
    /// Row texts are tagged unique per case so `MarkdownBody`'s memo cannot
    /// hide a row that was realised: `markdown.parse` then counts exactly the
    /// row bodies that ran.
    static func lazyTranscript(_ count: Int, aligned: Bool, tag: String) -> AnyView {
        // `CountsPlacements` directly, not `countingPlacements(_:_:)`: that
        // modifier reads `Diagnostics.isEnabled` where it is *called*, and this
        // tree is built before the harness turns the switch on.
        let list = ScrollView {
            CountsPlacements(placements: placeCounter, measurements: measureCounter) {
                LazyVStack(spacing: TranscriptMetrics.rowSpacing) {
                    ForEach(taggedRows(count, tag: tag)) { row in
                        TranscriptRowView(row: row, openPeerThread: { _ in })
                            .id(row.id)
                    }
                }
                .padding(.horizontal, TranscriptMetrics.horizontalPadding)
                .padding(.vertical, TranscriptMetrics.verticalPadding)
            }
        }
        return aligned
            ? AnyView(list.frame(minHeight: 0, idealHeight: 0,
                                 maxHeight: .infinity, alignment: .top))
            : AnyView(list.frame(minHeight: 0, idealHeight: 0, maxHeight: .infinity))
    }

    static func taggedRows(_ count: Int, tag: String) -> [TranscriptRow] {
        rows(count).map { row in
            var tagged = row
            tagged.message.text = "[\(tag)] " + row.message.text
            return tagged
        }
    }

    /// His conversation: wrapping sentences, both directions, ninety seconds
    /// apart. The lengths matter — a row that fits on one line is not the row
    /// that was being re-measured.
    static func rows(_ count: Int) -> [TranscriptRow] {
        var out: [TranscriptRow] = []
        for index in 1...count {
            let mine = index % 3 == 0
            let text = "transcript line \(index) — the runner never got the branch it "
                + "was told to use, so every job it started measured the wrong tree, and "
                + "this sentence is long enough to wrap in the pane he reads it in."
            let message = makeMessage(
                "m\(index)", cursor: String(format: "%04d", index), text: text,
                thread: "direct:chief", author: mine ? "owner" : "chief",
                role: mine ? .owner : .agent, ts: 1_700_000_000 + Double(index) * 90)
            out.append(TranscriptRow(message: message, attribution: .ordinary))
        }
        return out
    }

    // MARK: - harness

    /// The lazy comparison's two numbers: layout operations, and how many row
    /// bodies the lazy stack actually realised.
    struct LazyCost {
        let ops: Int
        let rows: Int
    }

    private func lazyTranscriptCost(_ count: Int, aligned: Bool) -> LazyCost {
        let tag = "lazy-\(aligned ? "top" : "default")-\(count)"
        // `layoutOps` resets the counters on the way in and leaves them
        // standing on the way out, so what is there afterwards is this run's.
        let ops = layoutOps(Self.lazyTranscript(count, aligned: aligned, tag: tag))
        return LazyCost(ops: ops, rows: Diagnostics.snapshot()["markdown.parse"] ?? 0)
    }

    /// **Layout operations for one forced layout** — every measurement and every
    /// placement SwiftUI asked of a transcript row, counted by the app's own
    /// `CountsPlacements` with the diagnostics switch held down for the duration.
    private func layoutOps(_ view: AnyView) -> Int {
        NSApplication.shared.setActivationPolicy(.prohibited)
        Diagnostics.isEnabled = true
        Diagnostics.reset()

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
        container.layoutSubtreeIfNeeded()
        // Bounded, and deliberately so: an unbounded pump in this suite once
        // held the SwiftPM lock for an hour and stopped ten other files.
        let began = Date()
        while Date().timeIntervalSince(began) < 0.2 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        container.layoutSubtreeIfNeeded()
        _ = host.fittingSize
        host.displayIfNeeded()

        let snapshot = Diagnostics.snapshot()
        window.orderOut(nil)
        window.contentView = nil
        Diagnostics.isEnabled = false
        return (snapshot[Self.measureCounter] ?? 0) + (snapshot[Self.placeCounter] ?? 0)
    }
}
