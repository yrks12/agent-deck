import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The transcript must place its subviews a finite number of times and then
/// stop.**
///
/// Taken live, 2026-09-06, while the owner had the app at 98.8% CPU and said
/// *"still stuck"* — a 5-second `sample`, ~4095 samples, one continuous stack
/// on the main thread:
///
/// ```
/// 4201  (in SwiftUICore)
///  344  (in AttributeGraph)
///  280  (in SwiftUI)
///   89  (in Agent Deck)        <- our own code is NOISE
///
/// NSHostingView.beginTransaction()
///  -> GraphHost.flushTransactions()                       4081
///    -> AG::Subgraph::update()                            3600
///      -> LazySubviewPlacements.updateValue()              934
///        -> LazySubviewPlacements.placeSubviews(...)       869
///          -> _LazyLayoutViewCache.withMutableCacheState   868
///            -> LazyStack<>.place(subviews:context:cache:in:)  832
///              -> _ViewList_Node.applyNodes                830
///                -> ForEachList.applyNodes                 813
///                  -> ForEachState.forEachItem             684
///                    -> DynamicViewList.WrappedList.applyNodes  645 (…30+ deep)
/// ```
///
/// ## Why every detector this repo already had stayed green through it
///
/// `ListChurnTests`, `SidebarChurnTests` and `InspectorIsQuietTests` count
/// **body evaluations**. `LayoutSettlesTests` counts **root layout passes**.
/// Eighty-nine samples out of four thousand are in our binary and the root's
/// `layout()` runs once — the spin is SwiftUI *placing the subviews of a lazy
/// stack*, over and over, inside a graph transaction that never converges.
/// Neither counter can see one pass of that, let alone thousands. **So this
/// file counts placements**: a `Layout` wrapped around each row, inside the
/// real container, counting every time SwiftUI asks it to place itself.
///
/// ---
/// # READ THIS BEFORE TRUSTING A GREEN HERE
///
/// **The 98.8% spin does not reproduce in this harness, and the numbers below
/// are the record of trying.** Measured 2026-09-06 on this branch, before any
/// fix, in a never-shown window, with the counter above:
///
/// ```
/// static, 20 and 60 rows, 380 / 520 / 900pt column
///     placements to settle   5 / 6 / 9      then 0 over a 1.0s watch
///
/// 200 lines seeded, then 20 lines appended one at a time (the streaming case)
///     ScrollView+LazyVStack   1 placement per line, 0 once it stops
///     List (.plain)           2-4 placements per line, 0 once it stops
///
/// cost of one placement pass, forced by alternating the column width
///     rows        25       100      400
///     LazyVStack  23.88ms  22.57ms  21.85ms   (20ms of that is the pump)
///     List        25.58ms  25.37ms  25.11ms
///
/// 40 display cycles driven by hand (setNeedsDisplay + displayIfNeeded)
///     LazyVStack  0 placements per cycle, all 40
///     List        0 placements per cycle, all 40
///
/// the whole app view, DeckRootView over a 200-line thread, 1.0s watch
///     1-2% of one core, 0 root layout passes
/// ```
///
/// Four ways of asking, and the answer is the same every time: **hosted with no
/// screen, this transcript settles.** The calibration tests below prove the
/// harness can see a view that does not, so that green is a real green — it
/// just is not the green that matters.
///
/// ## The fifth way: a display cycle, driven by hand. It does not work either.
///
/// The sample's first frame is `NSHostingView.beginTransaction`, which AppKit
/// calls from the **display cycle**, so the obvious missing instrument is a
/// headless one. Measured 2026-09-07 on a hosted 200-row `LazyVStack` with a
/// counting `Layout` around every row, 0.6s watch each:
///
/// ```
/// window.screen                                    nil   (it really is off every display)
/// pump only, nothing driving it                      0 placements
/// setNeedsDisplay + displayIfNeeded, 60 cycles       0 placements
/// CATransaction begin/commit/flush, 60 cycles        0 placements
/// CVDisplayLink on the active displays               0 placements
///     ...which delivered 72 real vsync ticks in that 0.6s
/// ```
///
/// The last line is the one that closes the question. **The driver worked** —
/// a `CVDisplayLink` runs perfectly well in this process and fired 72 times at
/// the panel's own refresh rate, each tick calling `setNeedsDisplay`,
/// `displayIfNeeded` and `CATransaction.flush` on the main thread. The counter
/// worked too: the same counter reads 11,490 placements for a list that is
/// being re-placed. An `NSHostingView` in a window whose `screen` is `nil`
/// simply does not begin a graph transaction from any of it.
///
/// So option A — "give the harness a display cycle" — is **not a calibration
/// problem, it is a dead end**, and it is written down here with a positive
/// control so nobody spends another night on it. Reproducing the spin needs a
/// real window on a real display in the real app, and `YOS_SCREEN_IS_FREE` is
/// not set, so that was not run and must not be.
///
/// ## What was shipped instead
///
/// `TranscriptList` now counts its own placement passes into `Diagnostics`, so
/// the instrument lives where the screen is — in the build he runs:
///
/// ```
/// DECK_DIAGNOSE=1 "$HOME/Projects/deck-app/dist/Agent Deck.app/Contents/MacOS/Agent Deck"
/// ```
///
/// The binary inside the bundle, not `open` — the counters go to standard error
/// and `open` would detach the process from the terminal.
///
/// One line a second. A settled transcript never names `transcript.place`; a
/// spinning one puts it first with a number in the hundreds.
/// `testTheShippedCounterSaysWhetherTheTranscriptIsSpinning` below is that
/// counter's calibration — 660 placements over 0.6s for a container being
/// re-placed, exactly 0 for the same container settled.
///
/// So this file is honest about what it is: **the finite-and-stops property,
/// pinned, with a counter that can see a violation.** It did not catch the
/// defect. It catches the day someone gives this list a width it has to derive
/// from its own content, which is the half of the fault that *is* measurable —
/// see `TranscriptFitsItsColumnTests`, which was red on this branch before the
/// fix and names the same `placeSubviews` chain.
///
/// **No screen is taken.** Activation policy is `.prohibited`, the window is
/// borderless, parked 40,000pt off every display, and never ordered front.
@MainActor
final class TranscriptSettlesTests: XCTestCase {

    // MARK: the placement counter

    /// Placements and measurements of one row, counted. Not `@Published`, not
    /// state: writing state from inside a layout is the very feedback loop this
    /// file is looking for.
    final class PlacementTally: @unchecked Sendable {
        private(set) var placements = 0
        private(set) var measurements = 0
        func placed() { placements += 1 }
        func measured() { measurements += 1 }
        func reset() { placements = 0; measurements = 0 }
    }

    /// A pass-through `Layout`: it proposes exactly what it was proposed and
    /// places its child exactly where it was placed, so wrapping a row in it
    /// changes no pixel. All it does is count.
    struct CountsItsPlacements: Layout {
        let tally: PlacementTally

        func sizeThatFits(
            proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
        ) -> CGSize {
            tally.measured()
            return subviews.first?.sizeThatFits(proposal) ?? .zero
        }

        func placeSubviews(
            in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
        ) {
            tally.placed()
            subviews.first?.place(at: bounds.origin, anchor: .topLeading, proposal: proposal)
        }
    }

    /// The transcript's container, with every row counted.
    ///
    /// It is the shape from `TranscriptList` — `ScrollViewReader` /
    /// `ScrollView` / `LazyVStack` / `ForEach`, the same spacing and padding,
    /// the same `scrollTo` on the last id — holding the app's **real** rows.
    /// A replica is what it costs to get a counter inside the list; the rows
    /// themselves, which is where the width is decided, are not replicated.
    private struct CountedTranscript: View {
        let entries: [ThreadEntry]
        let tally: PlacementTally
        /// The app's own value, 16, unless the calibration below is driving it.
        /// The spinning control and the settled control are then **the same
        /// view** — a control that changed the view as well as the stimulus
        /// would be comparing two different things and proving neither.
        var horizontalPadding: CGFloat = 16

        var body: some View {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(entries) { entry in
                            counted(entry).id(entry.id)
                        }
                    }
                    .padding(.horizontal, horizontalPadding)
                    .padding(.vertical, 14)
                }
                .onChange(of: entries.last?.id) { _, last in
                    guard let last else { return }
                    proxy.scrollTo(last, anchor: .bottom)
                }
            }
        }

        @ViewBuilder
        private func counted(_ entry: ThreadEntry) -> some View {
            if case .said(let row) = entry {
                CountsItsPlacements(tally: tally) {
                    TranscriptRowView(row: row, openPeerThread: { _ in })
                }
            }
        }
    }

    // MARK: the harness

    private final class LayoutProbe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() {
            passes += 1
            super.layout()
        }
    }

    struct Reading {
        /// Placements of the transcript's rows while nothing was happening.
        /// **The number this file is about.**
        var placementsAtRest: Int
        /// Placements it took to draw the conversation in the first place —
        /// the finite number that has to be greater than zero.
        var placementsToSettle: Int
        /// CPU seconds this process burned during the watch window.
        var cpu: Double
        /// Root layout passes in the same window, kept so the two can be
        /// compared and so it is on the record that this one does not move.
        var passes: Int
        /// The size the transcript **placed its rows in** — the scrolled
        /// content, not `fittingSize`. A column may no longer ask for more
        /// height than the window it is in, so a healthy transcript's ideal
        /// height is zero and only what it placed can prove it drew. See
        /// `laidOutSize` and `AColumnFitsTheWindowItIsInTests`.
        var laidOut: CGSize
        var watch: TimeInterval

        /// 1.0 means a whole core, which is what he measured.
        var cores: Double { cpu / watch }
    }

    /// User + system CPU seconds this process has consumed so far.
    private func cpuSeconds() -> Double {
        var usage = rusage()
        guard getrusage(RUSAGE_SELF, &usage) == 0 else { return 0 }
        func seconds(_ value: timeval) -> Double {
            Double(value.tv_sec) + Double(value.tv_usec) / 1_000_000
        }
        return seconds(usage.ru_utime) + seconds(usage.ru_stime)
    }

    private func reading<V: View>(
        _ view: V, tally: PlacementTally? = nil,
        width: CGFloat = 900, height: CGFloat = 700,
        settle: TimeInterval = 1.0, watch: TimeInterval = 1.0,
        beforeWatch: () -> Void = {}
    ) -> Reading {
        NSApplication.shared.setActivationPolicy(.prohibited)

        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: width, height: height)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()

        // `before:` is a *limit* date, not a sleep — the run loop returns the
        // moment an input source fires. A short limit therefore busy-spins the
        // test process and puts a floor under every CPU reading below; at 5ms
        // that floor was measured at 18% of a core, which is most of the way to
        // the threshold the readings are judged against. 50ms leaves the loop
        // free to block, and a view that has work to do still wakes it.
        func pump(_ seconds: TimeInterval) {
            let began = Date()
            while Date().timeIntervalSince(began) < seconds {
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
            }
        }
        // Everything the first draw legitimately costs happens in here.
        pump(settle)
        let drew = tally?.placements ?? 0

        tally?.reset()
        // The same instant as the tally reset, so a counter kept somewhere else
        // — `Diagnostics`, which is the one he can run — measures the same
        // window as this one and the two numbers can be read side by side.
        beforeWatch()
        let cpuBefore = cpuSeconds()
        let passesBefore = probe.passes
        pump(watch)
        let result = Reading(
            placementsAtRest: tally?.placements ?? 0,
            placementsToSettle: drew,
            cpu: cpuSeconds() - cpuBefore,
            passes: probe.passes - passesBefore,
            laidOut: probe.laidOutSize(),
            watch: watch)

        window.orderOut(nil)
        window.contentView = nil
        return result
    }

    // MARK: THE detector — placements reach a finite number and stop

    /// **A day's work, at rest.** 200 lines, several of them walls of pasted
    /// text over the collapse threshold, at the width he runs the app at.
    /// Nothing is arriving, nothing is typed, no desk is working.
    ///
    /// `LazySubviewPlacements` is what the sample is inside, and placement is
    /// O(list) per pass — so length is the axis that turns a settling layout
    /// into one that cannot keep up.
    func testPlacingALongConversationTakesAFiniteNumberOfPassesAndThenStops() {
        let tally = PlacementTally()
        let entries = Self.conversation(of: 200)
        let reading = reading(
            CountedTranscript(entries: entries, tally: tally), tally: tally)

        assertPlacementStopped(reading, "a 200-line conversation at 900pt")
    }

    /// The narrowest column the window allows: 900pt of window, minus a 260pt
    /// sidebar and a 260pt inspector. **Width is what the placement pass
    /// derives**, so the narrow case is the interesting one — it is where a row
    /// that has to work its own width out overflows, and an overflowing row is
    /// a content size that feeds back into the container that proposed it.
    func testPlacementStopsAtTheNarrowestColumnTheWindowAllows() {
        let tally = PlacementTally()
        let entries = Self.conversation(of: 200)
        let reading = reading(
            CountedTranscript(entries: entries, tally: tally), tally: tally, width: 380)

        assertPlacementStopped(reading, "a 200-line conversation at a 380pt column")
    }

    /// **Mixed Hebrew and English in one bubble.** Every desk on this Mac
    /// writes like this, and bidirectional text is laid out by a different path
    /// in the text engine — a line that settles in English and not in Hebrew
    /// would be invisible to every other test in this repo.
    func testPlacementStopsForAConversationWrittenInHebrewAndEnglish() {
        let tally = PlacementTally()
        let entries = (1...80).map { Self.entry($0, text: Self.mixedDirection) }
        let reading = reading(
            CountedTranscript(entries: entries, tally: tally), tally: tally, width: 520)

        assertPlacementStopped(reading, "a conversation in Hebrew and English")
    }

    private func assertPlacementStopped(
        _ reading: Reading, _ what: String,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        // GOOD SIGNAL FIRST, TWICE OVER. A pane that never drew places nothing
        // and is perfectly still — it would sail through the assertion below
        // while showing him a blank window.
        XCTAssertGreaterThan(
            reading.laidOut.height, 100,
            "\(what) never laid out to a real size (\(reading.laidOut)), so the "
            + "stillness below is the stillness of a blank window",
            file: file, line: line)
        XCTAssertGreaterThan(
            reading.placementsToSettle, 0,
            "\(what) was never placed at all, so the counter is reading nothing "
            + "and the assertion below cannot fail",
            file: file, line: line)

        XCTAssertEqual(
            reading.placementsAtRest, 0,
            "\(what) placed its rows \(reading.placementsAtRest) more times over "
            + "\(reading.watch)s with nothing happening — no message arriving, "
            + "nothing typed, no desk working. It took "
            + "\(reading.placementsToSettle) placements to draw and then never "
            + "stopped. The live sample at 98.8% says where that goes: "
            + "GraphHost.flushTransactions -> Subgraph::update -> "
            + "LazySubviewPlacements.placeSubviews -> LazyStack.place -> "
            + "ForEachList.applyNodes, ~30 wrapper nodes deep, with only 89 of "
            + "4095 samples in our own binary. From his side that is the app "
            + "being stuck.",
            file: file, line: line)
    }

    // MARK: calibration — this file must be able to fail

    /// **The known-bad stimulus, and the switch that turns it off.**
    ///
    /// It re-enqueues itself on the main queue instead of using a timer. That
    /// is not a style choice — it is the whole reason this file used to be
    /// skipped.
    ///
    /// The fixture this replaced drove itself with
    /// `Timer.publish(every: 0.001, on: .main, in: .common).autoconnect()`,
    /// which has **no cancellation**. Once one had been hosted, that timer went
    /// on waking the run loop a thousand times a second for the rest of the
    /// process — through the remaining tests in this file and through the other
    /// six hundred in the suite. The calibration below measures the busy view
    /// first and the still one second, so the still reading was **the busy
    /// fixture's own exhaust**. Measured on this branch, CPU seconds over a
    /// 0.6s watch:
    ///
    /// ```
    /// a static view, cold                              0.0045
    /// the timer fixture, running                       0.2021
    /// the same static view, right after it             0.0245   <- 5x
    /// the same static view, once more                  0.0423   <- 9x, no decay
    ///
    /// the stoppable fixture below, in a clean process
    /// a static view, cold                              0.0045
    /// the fixture, running                             0.4144
    /// the same static view, after stop()               0.0066   <- flat
    /// ```
    ///
    /// A `4x + 0.02` bar measured against a baseline the measurement itself
    /// inflated five- to ninefold cannot be met by anything. That is what
    /// `("0.117") is not greater than ("0.160")` was, and it is why the file
    /// was excluded whole rather than being wrong quietly.
    ///
    /// **Bounded by construction.** Nothing here is waited on: the block is
    /// enqueued and the run loop is free, the reading's pump is bounded by wall
    /// clock, and `stop()` ends it on the next turn.
    private final class Spin: ObservableObject {
        @Published private(set) var tick = 0
        private var running = false

        func start() {
            guard !running else { return }
            running = true
            step()
        }

        func stop() { running = false }

        private func step() {
            guard running else { return }
            tick &+= 1
            DispatchQueue.main.async { [weak self] in self?.step() }
        }
    }

    /// **The defect, written small: a long lazy list that is placed again on
    /// every turn of the run loop.**
    ///
    /// The fixture this replaced was one five-word `Text` re-padded on a timer.
    /// That is not the shape of the fault — the sample is inside
    /// `LazySubviewPlacements.placeSubviews -> LazyStack.place ->
    /// ForEachList.applyNodes`, which is a lazy stack placing *many* rows — and
    /// it did not cost what the fault costs: 0.34 of a core against the 0.90
    /// this one reaches, close enough to his measured 98.8% to be the same
    /// thing.
    private struct SpinningTranscript: View {
        @ObservedObject var spin: Spin
        let entries: [ThreadEntry]
        let tally: PlacementTally

        var body: some View {
            CountedTranscript(
                entries: entries, tally: tally,
                horizontalPadding: CGFloat(16 + spin.tick % 7))
        }
    }

    /// The conversation both controls are measured over. Long enough that one
    /// placement pass is real work, short enough that 0.6s settles it.
    private static let calibration = conversation(of: 60)

    /// The settled control. **Taken first, always** — see `Spin`.
    private func settledReading() -> Reading {
        let tally = PlacementTally()
        return reading(
            CountedTranscript(entries: Self.calibration, tally: tally),
            tally: tally, settle: 0.6, watch: 0.6)
    }

    /// The known-bad control, switched off however this returns.
    private func spinningReading() -> Reading {
        let spin = Spin()
        let tally = PlacementTally()
        // `defer` and not a trailing call: a fixture left running poisons every
        // later reading in this process, which is the failure this whole
        // section is a record of.
        defer { spin.stop() }
        spin.start()
        return reading(
            SpinningTranscript(spin: spin, entries: Self.calibration, tally: tally),
            tally: tally, settle: 0.6, watch: 0.6)
    }

    /// **The counter has to be able to see a container that never settles.**
    /// Without this, every green above is worth nothing and this file is
    /// decoration over a spinning app.
    func testThePlacementCounterCanSeeAViewThatNeverSettles() {
        let still = settledReading()
        let restless = spinningReading()

        XCTAssertGreaterThan(
            restless.placementsAtRest, 1_000,
            "the counter saw only \(restless.placementsAtRest) placements for a "
            + "60-row list that is re-placed on every turn of the run loop. It "
            + "is not counting anything, and every assertion in this file is "
            + "worthless.")
        XCTAssertEqual(
            still.placementsAtRest, 0,
            "the same list, with nothing driving it, was placed "
            + "\(still.placementsAtRest) times over \(still.watch)s. Zero is "
            + "then not reachable on this machine and the assertions above "
            + "cannot mean what they say.")
    }

    /// The same calibration for the CPU reading kept below it, because the two
    /// numbers answer different questions and the outage was found with this
    /// one first.
    ///
    /// **The still reading is taken before the busy one and that ordering is
    /// the fix.** The threshold is untouched.
    func testTheHarnessCanSeeAViewThatBurnsACore() {
        let still = settledReading()
        let busy = spinningReading()

        XCTAssertGreaterThan(
            busy.cpu, still.cpu * 4 + 0.02,
            String(format:
                "the harness cannot tell a view that never stops working (%.3f CPU "
                + "seconds) from one that is doing nothing (%.3f). Every assertion "
                + "in this file is then worthless, and it says so here rather than "
                + "passing green over a spinning app.", busy.cpu, still.cpu))
        XCTAssertLessThan(
            still.cores, 0.2,
            String(format:
                "a settled list burned %.0f%% of a core doing nothing, so the "
                + "threshold below cannot mean anything on this machine",
                still.cores * 100))
    }

    // MARK: the counter HE can run, on his own machine, in one minute

    /// The real `TranscriptList`, with something outside it changing on every
    /// turn of the run loop — a window being resized forever, which is the
    /// cheapest honest way to make the shipped container churn without
    /// replicating it.
    private struct SpinningRealTranscript: View {
        @ObservedObject var spin: Spin
        let entries: [ThreadEntry]

        var body: some View {
            TranscriptList(entries: entries)
                .frame(width: 900 - CGFloat(spin.tick % 7))
        }
    }

    /// **`DECK_DIAGNOSE=1` must say, on his machine, whether the transcript is
    /// spinning.**
    ///
    /// This file cannot reproduce the fault — the section above is the record
    /// of four ways of failing to, and the one below adds a fifth. What it can
    /// do is put the counter that *would* have been unmistakable into the build
    /// he runs, and then prove here that the counter works: that it moves for a
    /// container which is being re-placed, and reads exactly zero for the same
    /// container settled.
    ///
    /// Without both halves this is the same mistake again — a detector nobody
    /// showed a known-bad view to.
    func testTheShippedCounterSaysWhetherTheTranscriptIsSpinning() {
        let wasEnabled = Diagnostics.isEnabled
        Diagnostics.isEnabled = true
        defer { Diagnostics.isEnabled = wasEnabled; Diagnostics.reset() }

        let entries = Self.conversation(of: 60)
        func counted() -> Int {
            Diagnostics.snapshot()[TranscriptList.placementCounter] ?? 0
        }

        // Settled first, for the same reason the calibration above is.
        Diagnostics.reset()
        var whileDrawing = 0
        let still = reading(
            TranscriptList(entries: entries).frame(width: 900),
            settle: 0.6, watch: 0.6,
            beforeWatch: { whileDrawing = counted(); Diagnostics.reset() })
        let stillCount = counted()

        let spin = Spin()
        defer { spin.stop() }
        spin.start()
        Diagnostics.reset()
        _ = reading(
            SpinningRealTranscript(spin: spin, entries: entries),
            settle: 0.6, watch: 0.6,
            beforeWatch: { Diagnostics.reset() })
        let spinningCount = counted()
        spin.stop()

        // The counter must cost nothing and move nothing when it is off. Same
        // list, same width, switch down: if the height differs, the diagnostic
        // is changing the thing it is supposed to be reporting on.
        Diagnostics.isEnabled = false
        let uncounted = reading(
            TranscriptList(entries: entries).frame(width: 900),
            settle: 0.6, watch: 0.1)
        Diagnostics.isEnabled = true

        // GOOD SIGNAL FIRST. A counter that is not wired up reads zero for
        // everything, and zero-at-rest is exactly what this test wants to
        // believe — so prove it fired while the list was being drawn.
        XCTAssertGreaterThan(
            whileDrawing, 0,
            "the transcript drew 60 rows and `\(TranscriptList.placementCounter)` "
            + "never moved, so it is not wired into the list at all and the zero "
            + "below means nothing")
        XCTAssertGreaterThan(
            still.laidOut.height, 100,
            "the counted transcript never laid out to a real size "
            + "(\(still.laidOut)), so this is measuring a blank window")

        // THE calibration: a container being re-placed is unmistakable.
        XCTAssertGreaterThan(
            spinningCount, 100,
            "a transcript whose container is re-placed on every turn of the run "
            + "loop reported only \(spinningCount) placements over 0.6s. The "
            + "counter cannot see a spin, so DECK_DIAGNOSE=1 would have shown "
            + "him a quiet line through the 98.8% he measured.")
        XCTAssertEqual(
            stillCount, 0,
            "a settled transcript reported \(stillCount) placements over "
            + "\(still.watch)s with nothing happening. Zero is then not the "
            + "resting value and the line he reads cannot be trusted either way.")

        XCTAssertEqual(
            still.laidOut.height, uncounted.laidOut.height, accuracy: 0.5,
            "the transcript lays out to \(still.laidOut.height) with the "
            + "diagnostic on and \(uncounted.laidOut.height) with it off — the "
            + "counter is changing the layout it exists to measure")
    }

    // MARK: the conversation he actually has, whole

    /// **The real pane, real store, real rows** — everything `CountedTranscript`
    /// leaves out. No counter can reach inside it, so this one is measured the
    /// way the last outage was: how much CPU the process burns at rest.
    func testTheOpenConversationStopsWorkingOnceItIsDrawn() async {
        let store = await loadedStore()
        let reading = reading(ThreadView(store: store).frame(width: 900, height: 700))

        assertQuiet(reading, "the open conversation")
    }

    func testALongOpenConversationStopsWorkingToo() async {
        let store = await loadedStore(messages: 200)
        let reading = reading(ThreadView(store: store).frame(width: 900, height: 700))

        assertQuiet(reading, "a 200-line conversation")
    }

    private func assertQuiet(
        _ reading: Reading, _ what: String,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertGreaterThan(
            reading.laidOut.height, 100,
            "\(what) never laid out to a real size (\(reading.laidOut)), so the "
            + "quiet below is the quiet of a blank window",
            file: file, line: line)

        XCTAssertLessThan(
            reading.cores, 0.25,
            String(format:
                "%@ burned %.0f%% of a CPU core over %.1fs with nothing "
                + "happening (%.3f CPU seconds, %d root layout passes). He "
                + "measured 98.8%% on the shipped build and said \"still stuck\".",
                what, reading.cores * 100, reading.watch, reading.cpu, reading.passes),
            file: file, line: line)
    }

    // MARK: the conversation under test

    /// A thread shaped like his: mostly ordinary lines, several walls of pasted
    /// text over the collapse threshold, Markdown, a URL, and Hebrew and
    /// English in the same bubble — plus §6.2 traffic, which is the row with
    /// the least width to work with and the first one to overflow.
    static func conversation(of count: Int) -> [ThreadEntry] {
        (1...count).map { entry($0, text: line($0)) }
    }

    static func entry(_ index: Int, text: String) -> ThreadEntry {
        let relayed = index % 7 == 0
        let mine = index % 4 == 0
        let thread = relayed ? "peer:chief|acme" : "direct:chief"
        let message = Message(
            id: "m\(index)", cursor: String(format: "%04d", index), threadID: thread,
            author: mine ? DeckOwner.name : "chief", role: mine ? .owner : .agent,
            sentAt: Date(timeIntervalSince1970: 1_756_000_000 + Double(index) * 60),
            text: text)
        let attribution: RelayAttribution =
            relayed ? .dispatch(to: "Acme", threadID: "peer:chief|acme") : .ordinary
        return .said(TranscriptRow(message: message, attribution: attribution))
    }

    static let mixedDirection =
        "מוכן ל-E2E של UX #15 — `studio-preview-pr15` עלה, אבל הלייב עדיין קפוא "
        + "אחרי crash-loop. Bake בודק שוב, ואני מעדכן ב-PASS/FAIL מחר ב-21:00. "
        + "The order is set by distance-to-revenue, not by which lane is loudest."

    static func line(_ index: Int) -> String {
        switch index % 5 {
        case 0:
            return "on it"
        case 1:
            return "**\(index).** מוכן ל-E2E של UX #15 (`studio-preview-`) — "
                + "לייב עדיין קפוא. פורמט: https://github.com/acme/studio/pull/15"
        case 2:
            return String(repeating:
                "The order is set by distance-to-revenue, not by which lane is "
                + "loudest, because the product already exists and the only gap "
                + "is selling it. ", count: 14)
        default:
            return "Portfolio line \(index) — long enough to wrap across the "
                + "column at every width this window can be, which is the whole "
                + "point of measuring it."
        }
    }

    private func loadedStore(messages: Int = 60) async -> DeckStore {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .idle
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(
            threadID: "direct:chief",
            messages: (1...messages).map { index in
                makeMessage(
                    "m\(index)", cursor: String(format: "%04d", index),
                    text: Self.line(index), thread: "direct:chief",
                    author: index % 4 == 0 ? DeckOwner.name : "chief",
                    role: index % 4 == 0 ? .owner : .agent)
            },
            isReadOnly: false, participants: [DeckOwner.name, "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        return store
    }
}
