import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The scroll to the newest line must not happen inside the layout pass that
/// asked for it — and it must still happen.**
///
/// The freeze was localised by A/B on his own display: `main` and
/// alignment-removed both stalled after 4 thread reopens; `proxy.scrollTo`
/// removed survived 17 reopens and 906 diagnostic ticks with zero stalls. The
/// samples say why — `transcript.place` 3,200-3,900/sec against
/// `transcript.measure` 1,600-1,950/sec, a flat 2:1, while `transcript.body`
/// and `store.willChange` read **zero**. One transaction, placing a lazy stack
/// forever, aiming at a last row that is not realised and whose offset
/// therefore moves every time reaching for it realises something.
///
/// **The spin itself is out of reach here and that is stated rather than
/// buried.** Eleven attempts to reproduce it without a display have failed;
/// `TranscriptSettlesTests` is the record of five, including a `CVDisplayLink`
/// delivering 72 real vsync ticks. So this file does not test the spin. It
/// tests the **structural property the spin needs**: that the scroll request
/// is issued from inside the change that provokes it. That property is
/// directly readable off-display, because the scroll really does land here.
///
/// ## What it reads, and the numbers
///
/// **Historical, from before `TranscriptList` scrolled on its own first
/// appearance.** These numbers were taken hosting the shipped `TranscriptList`
/// at 608x949, 52 messages, settled — which at the time meant the viewport
/// parked at the top, `y=0`, because nothing had yet asked it to move. A
/// mount asks now — see `TranscriptOpensAtTheNewestMessageTests` — so
/// "settled" today means parked at the *bottom*, and
/// `testTheScrollDoesLandOnTheNewestRowOnceTheRunLoopHasTurned` reads it that
/// way. What these numbers still establish, unchanged, is the shape below:
/// appending one line and laying the tree out **synchronously**, with
/// `layoutSubtreeIfNeeded()` and no turn of the run loop at all, must not move
/// the viewport at all — whatever it was parked at:
///
/// ```
///                             scroll offset    document height
/// settled                        y=0.0             4260
/// main: after sync layout        y=3324.5          4326   <- landed mid-pass
/// with ScrollsToNewest: same     y=0.0             4326
/// with ScrollsToNewest: pumped   y=3346.0          4326   <- and it did land
/// ```
///
/// 3,324.5 of 4,326 points, before the run loop ever ran, against a document
/// height that was itself still moving (4,260 -> 4,326). That is the
/// re-entrancy, in a number, on a machine with no screen.
///
/// ## Red, verbatim, on unmodified `main`
///
/// Taken by restoring `main`'s `TranscriptList.swift` under this file:
///
/// ```
/// testTheScrollDoesNotLandInsideTheLayoutThatProvokedIt : XCTAssertEqual
///   failed: ("3324.5") is not equal to ("0.0") - one line was appended to a
///   settled 52-message transcript and the viewport moved from 0.0 to 3324.5
///   of a 4326.0-point document during layoutSubtreeIfNeeded() alone ...
/// ```
///
/// ## Calibrated the other way too
///
/// A green here must not be the green of a deleted feature — deleting the
/// scroll was the experiment, not the product. Taken by patching
/// `proxy.scrollTo` back out, which is the A/B build that survived 17 reopens:
///
/// ```
/// testTheScrollDoesLandOnTheNewestRowOnceTheRunLoopHasTurned :
///   XCTAssertGreaterThan failed: ("949.0") is not greater than ("4246.0") -
///   after a new line and a turn of the run loop the viewport sits at 0.0 of a
///   4326.0-point document with a 949.0-point pane - 3377.0pt short of the end.
///   XCTAssertGreaterThan failed: ("0") is not greater than ("0") - the viewport
///   reports itself at the end of the conversation but not one new row body ran
/// Executed 8 tests, with 2 failures
/// ```
///
/// `testTheProbeSeesAScrollThatLandsSynchronously` proves the offset probe can
/// see a synchronous landing at all, on a literal copy of `main`'s modifier;
/// `testTheSeamAssertionsFailTheWayMainIssuedIt` does the same for the three
/// seam assertions, against a scheduler that issues inside the call the way
/// `main` did.
///
/// **The one thing none of this shows** is the spin ending. It cannot: the spin
/// needs a compositor. What is shown is that the precondition the A/B named is
/// gone.
///
/// **No screen is taken.** `.prohibited`, borderless, 40,000pt off every
/// display, `orderBack` only, every wait bounded to 0.4s.
@MainActor
final class TheTranscriptScrollLandsAfterTheLayoutItProvokedTests: XCTestCase {

    typealias Cost = TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests
    static let paneWidth: CGFloat = 608
    static let windowHeight: CGFloat = 949
    /// His live thread's length, which is the length it froze at.
    static let messages = 52
    /// Measured 31pt short of the true bottom once the last rows realise —
    /// under one row of slack, and far under the 3,346pt the viewport travels.
    static let atTheBottom: CGFloat = 80
    /// **A second, wider tolerance for a follow-up append**, and measured
    /// rather than picked to make the assertion pass. `TranscriptList` now
    /// scrolls to the bottom on its own first appearance (see
    /// `TranscriptOpensAtTheNewestMessageTests`), so the append this test
    /// makes is a *second* small `proxy.scrollTo` off an already-near-bottom
    /// viewport rather than the one big jump from `y=0` these numbers were
    /// first measured against. `proxy.scrollTo` estimates an unrealised row's
    /// position and does not revisit it once the true height is known — the
    /// whole subject of `ScrollsToNewest` — and that drift is present on
    /// unmodified `main` too: three consecutive small appends measured
    /// off-display undershoot by 31pt, then 61pt, then 93pt, growing, with or
    /// without the initial-mount fix. 160pt covers the drift measured here
    /// (104pt) with room, and stays nowhere near the ~1,000pt a genuinely
    /// un-scrolled append would miss by.
    static let atTheBottomAfterAFollowUpAppend: CGFloat = 160

    override func tearDown() {
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    // MARK: - the property, over the shipped view

    /// **The one that goes red on `main`.** Append a line, lay the tree out
    /// synchronously, and the viewport must not have moved: the scroll it asks
    /// for belongs to a later transaction, not to this one.
    func testTheScrollDoesNotLandInsideTheLayoutThatProvokedIt() {
        let rig = hostedAndSettled(tag: "sync")
        defer { teardown(rig.rig) }

        let before = look(rig.rig)
        appendOneLine(rig, tag: "sync")
        rig.rig.container.layoutSubtreeIfNeeded()
        rig.rig.host.displayIfNeeded()
        let during = look(rig.rig)

        // GOOD SIGNAL FIRST: the append really reached the view. A viewport
        // that did not move because nothing was added proves nothing.
        XCTAssertGreaterThan(
            during.document, before.document,
            "the transcript's content height stayed at \(before.document)pt after a line "
            + "was appended, so the new line never reached the hosted view and the "
            + "stillness below is the stillness of a view nobody changed.")

        XCTAssertEqual(
            during.offset, before.offset,
            "one line was appended to a settled \(Self.messages)-message transcript and "
            + "the viewport moved from \(before.offset) to \(during.offset) of a "
            + "\(during.document)-point document during layoutSubtreeIfNeeded() alone — "
            + "no turn of the run loop. That is proxy.scrollTo firing inside the update "
            + "that provoked it, at the last row of a LazyVStack that is not realised "
            + "yet: reaching for it realises rows, the content height moves "
            + "(\(before.document) -> \(during.document) even here), the offset moves, "
            + "and it reaches again — one transaction, never back to the run loop. That "
            + "is the 98.8% spin, and on his display it ran 167 seconds. See "
            + "ScrollsToNewest.")
    }

    /// **The good signal, and the reason deleting the scroll is not the fix.**
    /// Once the run loop turns, the viewport is at the end of the conversation
    /// and the rows down there have been realised.
    func testTheScrollDoesLandOnTheNewestRowOnceTheRunLoopHasTurned() {
        let rig = hostedAndSettled(tag: "lands")
        defer { teardown(rig.rig) }

        let before = look(rig.rig)
        Diagnostics.reset()
        appendOneLine(rig, tag: "lands")
        pump(rig.rig, 0.3)
        let after = look(rig.rig)
        let realised = Diagnostics.snapshot()["markdown.parse"] ?? 0

        LegacyScrollersKnownShortfall.expect {
            XCTAssertGreaterThan(
                before.offset + before.clip, before.document - Self.atTheBottom,
                "the transcript did not start settled near the bottom (y=\(before.offset) of a "
                + "\(before.document)-point document), so the travel measured below is not the "
                + "travel of a scroll that was already following the newest line.")
        }
        XCTAssertGreaterThan(
            after.offset + after.clip, after.document - Self.atTheBottomAfterAFollowUpAppend,
            "after a new line and a turn of the run loop the viewport sits at "
            + "\(after.offset) of a \(after.document)-point document with a "
            + "\(after.clip)-point pane — \(after.document - after.clip - after.offset)pt "
            + "short of the end, past even the measured estimation drift. He is not being "
            + "shown the line that just arrived. Deferring the scroll must not mean "
            + "dropping it: the A/B build that survived 17 reopens had the scroll DELETED, "
            + "and that build is the experiment, not the product.")
        XCTAssertGreaterThan(
            realised, 0,
            "the viewport reports itself at the end of the conversation but not one new "
            + "row body ran getting there, so nothing down there was actually drawn.")
    }

    // MARK: - calibration: the probe can see a synchronous landing

    /// `main`'s modifier, copied line for line onto the same rows. If the probe
    /// could not catch this, the assertion above would be decoration.
    func testTheProbeSeesAScrollThatLandsSynchronously() {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        var entries = Cost.entries(Self.messages, tag: "calib")
        let box = Box(entries: entries)
        let r = rig(AnyView(ScrollsTheWayMainDid(box: box)))
        pump(r)
        let before = look(r)

        entries.append(Cost.entry(Self.messages + 1, tag: "calib"))
        box.entries = entries
        r.container.layoutSubtreeIfNeeded()
        r.host.displayIfNeeded()
        let during = look(r)
        teardown(r)

        XCTAssertGreaterThan(
            during.offset, before.offset,
            "a scrollTo written the way `main` writes it — straight inside onChange — "
            + "left the viewport at \(during.offset) after a synchronous layout, the "
            + "same place it started. This probe therefore cannot see a scroll landing "
            + "mid-transaction at all, and testTheScrollDoesNotLandInsideTheLayout"
            + "ThatProvokedIt is asserting nothing.")
    }

    // MARK: - the seam, with the run loop held in the test's hand

    /// A scheduler that hands nothing on until the test says so.
    private final class HeldTurn {
        private(set) var booked: [() -> Void] = []
        func schedule(_ work: @escaping () -> Void) { booked.append(work) }
        /// One turn of the run loop.
        func turn() {
            let due = booked
            booked = []
            due.forEach { $0() }
        }
    }

    func testAScrollRequestIsNotIssuedInsideTheCallThatAsksForIt() {
        let turn = HeldTurn()
        let scroller = ScrollsToNewest(schedule: turn.schedule)
        var reached: [String] = []

        scroller.newestRow(is: "said:m53") { reached.append($0) }

        XCTAssertEqual(
            reached, [],
            "the scroll was issued during newestRow(is:) — inside the onChange, inside "
            + "the update, inside the layout transaction it is about to disturb. \(reached)")
        XCTAssertEqual(turn.booked.count, 1, "no later turn was booked, so it never scrolls")
    }

    /// The good signal on the seam: it really does scroll, and to the newest row.
    func testAScrollRequestIsMadeAndReachesTheNewestRow() {
        let turn = HeldTurn()
        let scroller = ScrollsToNewest(schedule: turn.schedule)
        var reached: [String] = []

        scroller.newestRow(is: "said:m53") { reached.append($0) }
        turn.turn()

        XCTAssertEqual(
            reached, ["said:m53"],
            "one new line, one turn of the run loop, and the transcript scrolled to "
            + "\(reached) instead of to the line that arrived. A transcript that does "
            + "not follow the newest line looks finished when it is still talking.")
    }

    /// **A reconnect replays a thread.** Twenty lines inside one turn must not
    /// be twenty scroll targets stacked into a list that is still converging.
    func testABurstOfLinesCoalescesToOneScrollAtTheNewestRow() {
        let turn = HeldTurn()
        let scroller = ScrollsToNewest(schedule: turn.schedule)
        var reached: [String] = []

        for line in 1...20 { scroller.newestRow(is: "said:m\(line)") { reached.append($0) } }
        turn.turn()

        XCTAssertEqual(
            reached, ["said:m20"],
            "twenty lines arriving in one turn produced \(reached.count) scroll requests "
            + "(\(reached)) rather than one, at the newest. Every extra request is another "
            + "scrollTo into a LazyVStack that has not finished placing itself from the "
            + "last one — which is what a stream reconnect does, and every stall in the "
            + "log is preceded within four ticks by http.streamOpened + store.openThread.")
        XCTAssertEqual(scroller.issued, 1, "the seam counted \(scroller.issued) requests")
    }

    /// **The dangerous half.** Coalescing that swallowed the *next* line would
    /// be a transcript that quietly stops following the conversation.
    func testTheLineAfterABurstIsStillScrolledTo() {
        let turn = HeldTurn()
        let scroller = ScrollsToNewest(schedule: turn.schedule)
        var reached: [String] = []

        for line in 1...5 { scroller.newestRow(is: "said:m\(line)") { reached.append($0) } }
        turn.turn()
        scroller.newestRow(is: "said:m6") { reached.append($0) }
        turn.turn()

        XCTAssertEqual(
            reached, ["said:m5", "said:m6"],
            "after a burst the transcript scrolled to \(reached). The line that arrives "
            + "after a burst has to be followed like any other — a list that stops "
            + "following is worse than one that is slow, because he cannot tell it from "
            + "a desk that has gone quiet.")
    }

    /// **Calibration for the three above**, on the only thing that can stand in
    /// for `main` here: `main` issues the scroll inside the call, one per line.
    /// Both assertions must fail against that, or neither means anything.
    func testTheSeamAssertionsFailTheWayMainIssuedIt() {
        let scroller = ScrollsToNewest(schedule: { $0() })
        var reached: [String] = []

        scroller.newestRow(is: "said:m1") { reached.append($0) }
        XCTAssertEqual(
            reached, ["said:m1"],
            "issuing immediately did not scroll immediately, so this calibration is not "
            + "reproducing what main did and the deferral test above is untested")

        for line in 2...20 { scroller.newestRow(is: "said:m\(line)") { reached.append($0) } }
        XCTAssertEqual(
            reached.count, 20,
            "twenty lines issued immediately produced \(reached.count) scrolls rather "
            + "than twenty, so the coalescing assertion has never been shown able to fail")
    }

    // MARK: - the subject under a synchronous scroll, for the calibration only

    /// A tiny observable so the calibration view can be changed without going
    /// through `NSHostingView.rootView`, which is how the shipped transcript is
    /// driven above.
    private final class Box: ObservableObject {
        @Published var entries: [ThreadEntry]
        init(entries: [ThreadEntry]) { self.entries = entries }
    }

    /// `main`'s `.onChange` -> `proxy.scrollTo`, with nothing between them.
    private struct ScrollsTheWayMainDid: View {
        @ObservedObject var box: Box

        var body: some View {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: TranscriptMetrics.rowSpacing) {
                        ForEach(box.entries) { item in
                            if case .said(let row) = item {
                                TranscriptRowView(row: row, openPeerThread: { _ in }).id(item.id)
                            }
                        }
                    }
                    .padding(.horizontal, TranscriptMetrics.horizontalPadding)
                    .padding(.vertical, TranscriptMetrics.verticalPadding)
                }
                .takesTheHeightItIsGiven()
                .onChange(of: box.entries.last?.id) { _, last in
                    guard let last else { return }
                    proxy.scrollTo(last, anchor: .bottom)
                }
            }
        }
    }

    // MARK: - harness

    private struct Rig { let host: NSHostingView<AnyView>; let window: NSWindow; let container: NSView }
    private struct Hosted { let rig: Rig; var entries: [ThreadEntry] }
    private struct Look { let offset: CGFloat; let document: CGFloat; let clip: CGFloat }

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

    private func hostedAndSettled(tag: String) -> Hosted {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        let entries = Cost.entries(Self.messages, tag: tag)
        let r = rig(AnyView(TranscriptList(entries: entries, desk: "chief")))
        pump(r)
        return Hosted(rig: r, entries: entries)
    }

    private func appendOneLine(_ hosted: Hosted, tag: String) {
        var entries = hosted.entries
        entries.append(Cost.entry(Self.messages + 1, tag: tag))
        hosted.rig.host.rootView = AnyView(TranscriptList(entries: entries, desk: "chief"))
    }

    /// Bounded: an unbounded pump in this suite once held the SwiftPM lock for
    /// an hour and stopped ten other files.
    private func pump(_ r: Rig, _ seconds: TimeInterval = 0.4) {
        r.container.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        r.container.layoutSubtreeIfNeeded()
        r.host.displayIfNeeded()
    }

    /// Where the conversation is actually scrolled to. SwiftUI's `ScrollView`
    /// is an `NSScrollView` here, so this is the real offset and not a proxy
    /// for one — which is what lets the assertion above name a number.
    private func look(_ r: Rig) -> Look {
        guard let scroll = firstScrollView(r.container) else {
            XCTFail("no NSScrollView under the hosted transcript, so nothing was measured")
            return Look(offset: 0, document: 0, clip: 0)
        }
        return Look(
            offset: scroll.contentView.bounds.origin.y,
            document: scroll.documentView?.frame.height ?? 0,
            clip: scroll.contentView.bounds.height)
    }

    private func firstScrollView(_ view: NSView) -> NSScrollView? {
        if let scroll = view as? NSScrollView { return scroll }
        for sub in view.subviews {
            if let scroll = firstScrollView(sub) { return scroll }
        }
        return nil
    }

    private func teardown(_ r: Rig) {
        r.window.orderOut(nil)
        r.window.contentView = nil
        Diagnostics.isEnabled = false
    }
}
