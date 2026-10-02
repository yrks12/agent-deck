import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The conversation must go quiet when nothing is happening.**
///
/// Measured on the shipped release bundle, 2026-09-04: `Agent Deck` sat at
/// **99.3% CPU** on the main thread with a 2.4 GB footprint and had to be
/// killed. `sample` put 2544 of 2552 samples inside
/// `NSHostingView.beginTransaction → GraphHost.flushTransactions`: the entire
/// view graph re-running on every pass of the run loop, for ever, with the
/// owner doing nothing at all.
///
/// The cause was an **indeterminate `ProgressView` left permanently on the
/// thread pane** whenever a desk read WORKING — which, for him, is most of the
/// time. An indeterminate progress indicator is an AppKit animation: it drives
/// the window's display cycle at the refresh rate. Because the strip sits in
/// the same `VStack` that sizes the transcript's `ScrollView`, every one of
/// those frames re-measured the whole `LazyVStack` (`LazyStack.measureEstimates`
/// across every row) and re-ran every row's `SelectionOverlay`. Nothing else in
/// this app had ever put a spinner on screen and left it there; the two before
/// it — the connection indicator and "Opening thread" — are both transient.
///
/// ---
/// ## What this harness can and cannot see — read this before trusting it
///
/// It hosts a real view in a real (never-shown) window and counts the root's
/// layout passes while pumping the real run loop.
///
/// **It CAN see** work driven by state: a timer, a publisher, a `@Published`
/// feedback loop. `testTheHarnessCanSeeAViewThatNeverStopsWorking` proves that
/// on every run, so this file can never quietly become decoration.
///
/// **It CANNOT see the bug that caused this outage.** AppKit suspends
/// animations in a window that is not on screen, so the very thing that drove
/// the loop stops in the harness. Measured: a timer-driven view reports ~49
/// layout passes a second here, and an indeterminate `ProgressView` reports
/// **0**. Reproducing it needs a real, visible window in a real app process —
/// that is `UITests/DeckUITests/IdleCostTests.swift`, which is gated on the
/// owner's screen and has not been run.
///
/// So the guard against *this* defect is not a measurement, it is a **rule**:
/// no steady state of the thread pane may be animated. That rule is asserted
/// below, in DeckKit and against the view's own source, and it is what fails if
/// anyone puts a spinner back.
///
/// **No screen is taken here.** No app is launched, nothing is `open`ed, no
/// XCUITest runs. The window is never ordered front or made key and the
/// activation policy is pinned to `.prohibited`, so the process cannot show a
/// Dock icon, take the menu bar or steal focus even by accident.
@MainActor
final class LayoutSettlesTests: XCTestCase {

    // MARK: the harness

    private final class LayoutProbe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() {
            passes += 1
            super.layout()
        }
    }

    /// Layout passes the root was put through during `watch`, after `settle`.
    @discardableResult
    private func passes<V: View>(
        _ view: V, settle: TimeInterval = 0.8, watch: TimeInterval = 0.6
    ) -> (count: Int, fitting: CGSize) {
        NSApplication.shared.setActivationPolicy(.prohibited)

        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 1000, height: 700)
        // A window, because AppKit's display cycle is what re-enters SwiftUI.
        // Borderless, parked far off every display, and never ordered front.
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 1000, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()

        func pump(_ seconds: TimeInterval) {
            let began = Date()
            while Date().timeIntervalSince(began) < seconds {
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
        }
        pump(settle)
        let before = probe.passes
        pump(watch)
        let result = (probe.passes - before, probe.fittingSize)

        window.orderOut(nil)
        window.contentView = nil
        return result
    }

    private func assertSettles<V: View>(
        _ view: V, _ what: String, file: StaticString = #filePath, line: UInt = #line
    ) {
        let (count, fitting) = passes(view)

        // GOOD SIGNAL FIRST. A view that never drew is also perfectly quiet,
        // and would sail through the check below while showing a blank pane.
        XCTAssertGreaterThan(
            fitting.height, 1,
            "\(what) never laid out to a real size (\(fitting)) — the quiet below "
            + "would be the quiet of a blank window, not of a settled one",
            file: file, line: line)

        XCTAssertLessThan(
            count, 10,
            "\(what) was laid out \(count) times in 0.6s with nothing happening. "
            + "He measured 99% CPU on the shipped build and had to kill the app.",
            file: file, line: line)
    }

    // MARK: calibration — this file must not be able to become decoration

    private struct NeverStopsWorking: View {
        @State private var tick = 0
        var body: some View {
            Text(String(repeating: "x", count: 1 + tick % 12))
                .padding(CGFloat(4 + tick % 7))
                .onReceive(Timer.publish(every: 0.016, on: .main, in: .common).autoconnect()) { _ in
                    tick += 1
                }
        }
    }

    /// The harness has to be able to fail. If this ever reports a quiet view,
    /// every assertion in this file is worthless and says so here rather than
    /// passing green over a spinning app.
    func testTheHarnessCanSeeAViewThatNeverStopsWorking() {
        let busy = passes(NeverStopsWorking())
        let still = passes(Text("nothing is happening").padding())

        XCTAssertGreaterThan(
            busy.count, still.count + 3,
            "the harness saw \(busy.count) busy passes against \(still.count) static "
            + "passes — it is not driving the run loop and proves nothing")
        XCTAssertLessThan(
            still.count, 10,
            "the harness reports \(still.count) passes for a static Text, so its "
            + "floor is noise and the threshold above means nothing")
    }

    // MARK: THE rule — no steady state of the thread pane may be animated

    /// Every condition the strip can show has to be drawable as a **still**
    /// picture. This is the rule the outage bought: a permanently animating
    /// indicator on the pane costs a full re-measure of the transcript on every
    /// frame, for as long as it is on screen.
    ///
    /// Sweeps the class rather than the case that spun — `working` was the one
    /// that shipped, but `connecting`, `reconnecting` and `sending` were all
    /// written as spinners too and every one of them would have done it.
    func testEveryConditionTheStripCanShowHasAStillPicture() {
        var checked = 0
        for state in AgentState.allCases {
            var desk = makeAgent("chief")
            desk.state = state
            for connection: ConnectionState in [.live, .idle, .connecting, .reconnecting(attempt: 1)] {
                for sending in [false, true] {
                    guard let status = DeskStatus.make(
                        agent: desk, connection: connection, approvals: [], isSending: sending
                    ) else { continue }
                    checked += 1
                    XCTAssertFalse(
                        status.symbol.isEmpty,
                        "\(state.rawValue)/\(connection.label)/sending=\(sending) has no "
                        + "still symbol, so the strip has to animate to show it — that is "
                        + "the 99% CPU defect")
                }
            }
        }
        XCTAssertGreaterThan(checked, 20, "this swept almost nothing")
    }

    /// And the view has to actually obey it. Read the way `ScreenGateTests`
    /// reads the UI tests: the property worth protecting is that a spinner
    /// cannot be put back next month without a red.
    func testTheStatusStripDrawsNoSpinnerAndAsksForNoTextBaseline() throws {
        let strip = try threadViewSource(of: "DeskStatusStrip")

        XCTAssertFalse(
            strip.contains("ProgressView"),
            "DeskStatusStrip draws a ProgressView. An indeterminate progress "
            + "indicator is an AppKit animation, and this strip is on screen "
            + "permanently — it drives the display cycle at the refresh rate and "
            + "re-measures the whole transcript on every frame.")
        XCTAssertFalse(
            strip.contains("firstTextBaseline"),
            "DeskStatusStrip aligns on a text baseline while holding views that "
            + "have none, so SwiftUI resolves a fallback baseline through a hidden "
            + "NSTextField whose font it sets on every pass — which invalidates "
            + "the field's intrinsic size and re-dirties the window's constraints, "
            + "scheduling the next pass. That is the loop, in the sample, verbatim.")
        XCTAssertFalse(
            strip.contains(".animation("),
            "DeskStatusStrip animates. Its height is what sizes the transcript's "
            + "ScrollView, so animating it re-measures every row of the LazyVStack "
            + "for the length of the animation, on every state change.")
    }

    /// **THE class sweep, and the one that should have been written first.**
    ///
    /// Measured twice. First sample: `FallbackAlignmentProvider.update(in:axis:)`
    /// → `-[NSControl setFont:]` → `-[NSTextFieldCell _invalidateEffectiveFont]`
    /// → `-[NSTextField invalidateIntrinsicContentSize]` →
    /// `setNeedsUpdateConstraints` → `-[NSWindow _postWindowNeedsUpdateConstraints]`.
    /// Second sample, *after* the spinner was removed and animation frames were
    /// down to **zero**, still at 98.4% CPU: 51 frames of the same chain, with
    /// `UnaryLayoutEngine.explicitAlignment` and `LayoutEngineBox.explicitAlignment`
    /// among the hottest in the process.
    ///
    /// That is the loop, and the spinner was only ever an accelerant on it.
    ///
    /// **Why the rule is absolute.** A stack aligned on a *text baseline* has to
    /// ask every child where its first line of text sits. Children that have no
    /// text — a `Spacer`, an `Image`, a shape, a control — have no such answer,
    /// so SwiftUI resolves a *fallback* through a hidden `NSTextField`, and it
    /// sets that field's font on every pass. Setting a font invalidates the
    /// field's intrinsic content size, which dirties the window's constraints,
    /// which schedules another pass. It never converges.
    ///
    /// Judging each site by hand is what failed: `DeskStatusStrip` was fixed and
    /// the two older ones were left, one of them in the sidebar, which is on
    /// screen every second the app is open. So the rule is the whole class, and
    /// it reads every view file — a baseline stack added next month fails an
    /// ordinary `swift test`, whatever its children look like at the time.
    func testNoViewAlignsOnATextBaseline() throws {
        var offenders: [String] = []
        for file in try viewSources() {
            for (number, line) in file.body.split(separator: "\n", omittingEmptySubsequences: false).enumerated()
            where line.contains("firstTextBaseline") || line.contains("lastTextBaseline") {
                offenders.append("\(file.name):\(number + 1)")
            }
        }

        XCTAssertEqual(
            offenders, [],
            "these align on a text baseline: \(offenders.joined(separator: ", ")). "
            + "On macOS SwiftUI resolves a text baseline for a child that has "
            + "none — a Spacer, an Image, a control — through a hidden NSTextField "
            + "whose font it sets on every pass, which invalidates that field's "
            + "intrinsic size, dirties the window's constraints and schedules the "
            + "next pass. Measured twice on the owner's Mac at 98-99% CPU with the "
            + "app unusable. Use .center or .top and nudge with padding.")
    }

    /// Every SwiftUI file in the app, comments stripped.
    private func viewSources() throws -> [(name: String, body: String)] {
        let directory = repoRoot.appendingPathComponent("Sources/DeckUI")
        let files = try FileManager.default.contentsOfDirectory(at: directory,
                                                                includingPropertiesForKeys: nil)
        let swift = files.filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        XCTAssertFalse(swift.isEmpty, "no view sources found at \(directory.path) — "
                       + "this check would pass over anything")
        return try swift.map {
            ($0.lastPathComponent, try source(of: "Sources/DeckUI/\($0.lastPathComponent)"))
        }
    }

    /// **The sweep.** The strip was not the only permanently-animating thing on
    /// this screen, and the other one is older than this fix.
    ///
    /// `ConnectionIndicator` spins for `.connecting` and `.reconnecting`. It
    /// looked transient and was, while the live stream never delivered anything
    /// — the app effectively never noticed a drop. Now that the stream works,
    /// `.reconnecting` is a real resting state: the backoff caps at 30s and
    /// retries for ever, so a deck that is down leaves a spinner running in the
    /// toolbar indefinitely. That is the same engine that took his Mac, in a
    /// state he reaches whenever the box goes away.
    func testTheConnectionIndicatorDoesNotSpinWhileTheDeckIsAway() throws {
        let source = try source(of: "Sources/DeckUI/ConnectionIndicator.swift")

        XCTAssertFalse(
            source.contains("ProgressView"),
            "ConnectionIndicator animates. `.reconnecting` retries for ever with a "
            + "30s cap, so a deck that is down leaves this running in the toolbar "
            + "indefinitely — the same permanent AppKit animation that put the "
            + "owner's Mac at 99% CPU, on a screen he cannot get away from.")

        // Precautionary, not measured: this view lives in an NSToolbar, its
        // width changes between states (a 7pt dot, a glyph, four different
        // words), and `.reconnecting(attempt:)` is a NEW value on every retry.
        // An implicit animation there animates a size change through AppKit's
        // constraint machinery on every attempt, which is the same shape as the
        // strip animation already removed. The state is legible from the word
        // and the glyph without it.
        XCTAssertFalse(
            source.contains(".animation("),
            "ConnectionIndicator animates a size change inside the toolbar, and "
            + "`.reconnecting(attempt:)` hands it a new value on every retry.")
    }

    /// The `.loading` case still says a thread is opening. It says it in
    /// words now rather than with a spinner (D2): an indeterminate
    /// `ProgressView` holds a dispatch worker for as long as it animates, and
    /// off-screen ones in this suite exhausted the pool. What must never go is
    /// the sentence.
    func testATransientSpinnerIsStillAllowedWhileSomethingIsActuallyInFlight() throws {
        let source = try threadViewSource(of: nil)

        XCTAssertTrue(
            source.contains("Opening thread"),
            "the honest notice — shown only while a thread is actually opening — "
            + "has been removed as well, which would leave an open request with "
            + "nothing on screen to say it is in flight")
    }

    // MARK: the diagnostic must actually be wired to the real views

    /// Before anyone is asked to run the instrumented build against the box,
    /// prove the counters are attached to the views he will be looking at. A
    /// diagnostic that reports nothing is indistinguishable from an app that is
    /// doing nothing, and that is the exact confusion this whole hunt is stuck
    /// in.
    ///
    /// Asserts the presence of the good signal: hosting the real `ThreadView`
    /// moves `thread.body` and `store.willChange`, and the report names them.
    func testTheDiagnosticIsWiredToTheRealThreadPane() async throws {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        defer { Diagnostics.isEnabled = false; Diagnostics.reset() }

        let store = try await loadedStore(messages: 5)
        _ = passes(ThreadView(store: store).frame(width: 1000, height: 700),
                   settle: 0.3, watch: 0.1)

        let counted = Diagnostics.snapshot()
        XCTAssertGreaterThan(
            counted["thread.body"] ?? 0, 0,
            "the thread pane drew and the counter did not move, so a run against "
            + "the real deck would report nothing and prove nothing: \(counted)")
        XCTAssertGreaterThan(
            counted["store.willChange"] ?? 0, 0,
            "the store published and the counter did not move: \(counted)")
        XCTAssertNotNil(Diagnostics.report(), "nothing would be printed at all")
    }

    /// And it must stay silent in the build he actually runs.
    func testTheDiagnosticIsSilentAndFreeWhenItIsNotAskedFor() async throws {
        Diagnostics.isEnabled = false
        Diagnostics.reset()

        let store = try await loadedStore(messages: 5)
        _ = passes(ThreadView(store: store).frame(width: 1000, height: 700),
                   settle: 0.3, watch: 0.1)

        XCTAssertTrue(
            Diagnostics.snapshot().isEmpty,
            "the owner's ordinary build is paying for instrumentation it never asked for")
    }

    // MARK: what the harness CAN still guard

    func testTheOpenConversationSettles() async throws {
        assertSettles(ThreadView(store: try await loadedStore()).frame(width: 1000, height: 700),
                      "the open conversation")
    }

    /// **The transcript is now card-heavy, and that is where this regresses.**
    ///
    /// Tool calls used to be one welded block under the conversation; they are
    /// interleaved with the messages now, each one a bordered card with a
    /// disclosure group, a status pill and up to three buttons, inside the same
    /// `LazyVStack` whose re-measure cost is what took the owner's Mac. If any
    /// of that moves at rest, this is where it shows.
    func testAConversationFullOfToolCallCardsSettles() async throws {
        let store = try await loadedStore(messages: 20, toolCalls: 8)

        // GOOD SIGNAL FIRST, and it matters more here than anywhere: a pane
        // with no cards on it is perfectly quiet and would pass everything
        // below while proving nothing about the thing under test.
        XCTAssertEqual(store.approvals.count, 8, "the cards never reached the store")
        guard case .loaded(let screen) = store.thread else {
            return XCTFail("no conversation is open, so nothing card-shaped was drawn")
        }
        XCTAssertEqual(
            screen.entries.filter { if case .toolCall = $0 { return true } else { return false } }.count,
            8, "the cards are not in the list the transcript draws")

        assertSettles(ThreadView(store: store).frame(width: 1000, height: 700),
                      "a conversation with eight tool-call cards in it")
    }

    /// And the card has to obey the same rule as the strip: **every resting
    /// state is a still picture.** The pill is a `Text` in a `Capsule`, the
    /// symbol is a glyph, and nothing on the card animates — a settled card is
    /// on screen for the rest of the session, so anything that moved on one
    /// would move for ever.
    func testTheToolCallCardDrawsNoSpinnerAndNothingOnItAnimates() throws {
        let source = try self.source(of: "Sources/DeckUI/ApprovalCardView.swift")

        XCTAssertFalse(
            source.contains("ProgressView"),
            "the tool-call card draws a ProgressView. A settled card stays in the "
            + "conversation for the rest of the session, so an indeterminate "
            + "indicator on one is a permanent AppKit animation inside the "
            + "transcript's LazyVStack — the 99% CPU defect, on a screen he "
            + "cannot get away from.")
        XCTAssertFalse(
            source.contains(".animation("),
            "the tool-call card animates. Its height sizes rows of the "
            + "transcript, so animating it re-measures the whole LazyVStack for "
            + "the length of every state change — and a poll changes state.")
    }

    func testTheConversationSettlesWithALongPasteInTheComposer() async throws {
        let store = try await loadedStore()
        store.composerDraft = (1...20).map { "paragraph \($0) of the mandate" }
            .joined(separator: "\n")

        assertSettles(ThreadView(store: store).frame(width: 1000, height: 700),
                      "the conversation with a long paste in the box")
    }

    // MARK: helpers

    private func loadedStore(messages: Int = 30, toolCalls: Int = 0) async throws -> DeckStore {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .working
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        client.pages = [
            MessagePage(
                threadID: "direct:chief",
                messages: (1...messages).map {
                    makeMessage("m\($0)", cursor: String(format: "%04d", $0),
                                text: "transcript line \($0), long enough to wrap across "
                                    + "more than one line in a thousand-point pane",
                                thread: "direct:chief")
                },
                isReadOnly: false, participants: ["owner", "chief"]
            )
        ]
        client.feeds = [.emitThenFinish([])]
        if toolCalls > 0 {
            // Spread across the same seconds the messages carry, so the cards
            // land *between* the lines rather than all at one end.
            client.approvalsPage = try LiveDeckClient.approvals(
                (1...toolCalls).map { ("ask\($0)", "chief", 1_700_000_000 + Double($0) * 3) }
            )
        }
        // A poll interval longer than the whole measurement: this test is about
        // whether the pane is still when nothing is happening, and a request
        // landing mid-watch would be something happening.
        let store = DeckStore(client: client, approvalPollInterval: 60)
        await store.loadRoster()
        await store.settle()
        await store.loadApprovals()
        return store
    }

    /// The **code** of one struct in `ThreadView.swift`, or of the whole file
    /// when `name` is nil.
    ///
    /// Comments are stripped first. Every one of these rules is worth a comment
    /// saying why the thing is absent, and a check that read those comments
    /// would fail on the explanation for the fix it is checking for.
    /// Any source file in the package, comments stripped.
    private func source(of relativePath: String) throws -> String {
        let url = repoRoot.appendingPathComponent(relativePath)
        return try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
    }

    private var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func threadViewSource(of name: String?) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI/ThreadView.swift")
        let whole = try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
        guard let name else { return whole }
        guard let start = whole.range(of: "struct \(name)") else {
            XCTFail("\(name) is no longer in ThreadView.swift, so this check reads nothing")
            return ""
        }
        let rest = whole[start.lowerBound...]
        // Up to the next top-level declaration.
        if let end = rest.range(of: "\nstruct ", range: rest.index(after: rest.startIndex)..<rest.endIndex) {
            return String(rest[..<end.lowerBound])
        }
        return String(rest)
    }
}
