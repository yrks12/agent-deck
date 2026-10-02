import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The inspector is on screen the whole time, and it was being rebuilt on
/// every keystroke.**
///
/// Three facts, each measured on this branch rather than argued:
///
/// 1. `DeckRootView` opened the inspector by default. Since 2026-09-30 it
///    opens closed (the Mac laid out like the phone), but once open it stays
///    beside the conversation for as long as he leaves it, typing included.
///    (The note in `BaselineFallbackTests` says the four `Form`s "live in the
///    settings inspector and two sheets; none was open during the four runs
///    above". That is true of the sheets and **wrong about the inspector**.)
///
/// 2. `DeckStore.composerDraft` is `@Published`, so **one character typed into
///    the conversation is one `objectWillChange` on the store**. Every view
///    holding that store as an `@ObservedObject` re-evaluates its body on it —
///    whether or not it shows anything that changed.
///
/// 3. Counted here, by `bodyEvaluations`, over 20 publishes:
///
///    | shape | body evaluations |
///    |---|---|
///    | a view that observes the store | **21** |
///    | the same content behind `.equatable()` on its values | **1** |
///
///    21 is one rebuild per keystroke plus the first draw. 1 is the first draw
///    and nothing after it. That is the same shape as
///    `DeckStoreTests.testAPollThatFindsNothingNewPublishesNothingAtAll`, which
///    asserts **0** emissions: assert the good signal, count it, and make the
///    harness prove it can see the bad case in the same run.
///
/// Rebuilding the inspector means rebuilding a `Form` of ten rows, its
/// sections, its text fields, its switch and the routines `ForEach` — new view
/// values, new generic metadata, new refcount traffic. That is what the live
/// sample is full of (`swift::RefCounts`, `_swift_getGenericMetadata`) and it
/// is why this only ever burns while he is *using* the app.
///
/// **What this file does not claim.** It does not say the app is quiet. No
/// headless probe in this repo has ever managed to say that — read the list of
/// seven blind ones at the top of `BaselineFallbackTests`. It says this
/// surface stops re-running when nothing it draws has changed, and it counts
/// that.
@MainActor
final class InspectorIsQuietTests: XCTestCase {

    // MARK: the harness

    private final class LayoutProbe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() { passes += 1; super.layout() }
    }

    /// A store with one desk selected, exactly as the inspector sees it.
    private func loadedStore() async -> DeckStore {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .working
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        store.select(agent: "chief", threadID: "direct:chief")
        return store
    }

    /// Hosts `view` in a real (never shown) window, types `publishes`
    /// characters into the composer, and returns how many times the counted
    /// body ran. Typing is the real driver: `composerDraft` is `@Published`.
    @discardableResult
    private func bodyEvaluations<V: View>(
        of view: V, whileTyping publishes: Int, into store: DeckStore, counter: BodyCounter
    ) -> Int {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 320, height: 800)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 320, height: 800),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        _ = probe.fittingSize
        for _ in 0..<publishes {
            store.composerDraft += "a"
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.002))
            probe.needsLayout = true
            probe.layoutSubtreeIfNeeded()
            _ = probe.fittingSize
        }
        window.orderOut(nil)
        window.contentView = nil
        return counter.value
    }

    /// Layout passes in `watch` seconds after `settle`, plus the size it
    /// reached — the same pair `LayoutSettlesTests` asserts on.
    private func settling<V: View>(
        _ view: V, settle: TimeInterval = 0.5, watch: TimeInterval = 0.5
    ) -> (passes: Int, fitting: CGSize) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 700, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 700, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
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

    // MARK: 1 — the count, and the harness proving it can see the bad shape

    /// **The calibration and the rule in one run.** The observing shape must
    /// read high or this probe measures nothing; the equatable shape must read
    /// exactly 1 — the first draw and not one rebuild after it.
    func testAKeystrokeRebuildsAnObservingViewAndNotAnEquatableOne() async {
        let store = await loadedStore()

        let observed = BodyCounter()
        let observing = bodyEvaluations(
            of: ObservingProbe(store: store, counter: observed),
            whileTyping: 20, into: store, counter: observed)

        let valued = BodyCounter()
        let equatable = bodyEvaluations(
            of: EquatableProbe(store: store, counter: valued),
            whileTyping: 20, into: store, counter: valued)

        XCTAssertGreaterThan(
            observing, 15,
            "a view that observes the store re-evaluated its body only "
            + "\(observing) times across 20 keystrokes, so this probe cannot see "
            + "the defect it was written for and every count below is worthless")
        XCTAssertEqual(
            equatable, 1,
            "the same content, given its values and wrapped in `.equatable()`, "
            + "ran its body \(equatable) times across 20 keystrokes instead of "
            + "once. SwiftUI is rebuilding a subtree whose inputs did not change.")
    }

    // MARK: 2 — the premise: this panel is not a sheet

    /// **Revisited, as this test asked to be (owner, 2026-09-30).** The Mac
    /// now opens like the iPhone, with the side panel closed until he opens
    /// it — so it is something he opens, not something always on screen. The
    /// counted rule above still holds and still matters: whenever it IS open
    /// (the toolbar, ⌥⌘I, the roster's attention card), it sits beside the
    /// conversation for as long as he leaves it, typing included.
    func testTheInspectorIsOnScreenWithoutAnybodyOpeningIt() throws {
        let root = try source(of: "Sources/DeckUI/DeckRootView.swift")

        XCTAssertTrue(
            root.contains("@State private var showInspector = false"),
            "the side panel opens by itself again — the Mac was laid out like "
            + "the phone, with the panel secondary (TheMacIsLaidOutLikeThePhoneTests)")
        XCTAssertTrue(
            root.contains("SettingsPanelView(store: store)"),
            "the inspector no longer draws SettingsPanelView, so this file is "
            + "guarding a panel that is not on screen")
    }

    // MARK: 3 — the real panel has the shape that stops the rebuild

    /// The counted rule above is worth nothing unless the shipped panel is the
    /// shape that was counted. This reads the real source.
    func testTheRealInspectorIsBuiltFromItsValuesAndNotFromTheStore() throws {
        let panel = try source(of: "Sources/DeckUI/SettingsPanelView.swift")

        // An @ObservedObject subscribes from INSIDE the subtree, so a single
        // one anywhere below the wrapper invalidates itself on every publish
        // and the skip above it buys nothing. Both of these are drawn in the
        // inspector; `NewRoutineSheet` is not, and is deliberately not swept.
        for (file, type) in [
            ("Sources/DeckUI/SettingsPanelView.swift", "struct SettingsPanelBody"),
            ("Sources/DeckUI/RoutinesPanelView.swift", "struct RoutinesPanelView"),
            // Pinned above the rest of the inspector, so it is on screen
            // whenever the panel is and pays the same rebuild if it observes.
            ("Sources/DeckUI/AttentionTrayView.swift", "struct AttentionTrayView"),
        ] {
            let declaration = try Self.declaration(of: type, in: source(of: file))
            XCTAssertFalse(
                declaration.isEmpty,
                "\(type) is not in \(file) any more, so this rule is passing over "
                + "a view that no longer exists")
            XCTAssertFalse(
                declaration.contains("@ObservedObject"),
                "\(type) observes DeckStore. It is drawn inside the always-open "
                + "inspector, and an @ObservedObject invalidates itself from "
                + "inside the subtree — so it re-runs on every publish, one per "
                + "character typed into the composer, whether or not the panel "
                + "above it was skipped. Take the values it draws instead.")
        }

        XCTAssertTrue(
            panel.contains(".equatable()"),
            "SettingsPanelView does not hand its Form to SwiftUI as an equatable "
            + "value, so every publish on DeckStore — every character typed into "
            + "the conversation composer — rebuilds the whole inspector. Measured "
            + "in this file: 21 body evaluations across 20 keystrokes, against 1 "
            + "for the equatable shape.")
    }

    /// **And the comparison has to see everything the panel draws.** This is
    /// the half that stops the fix becoming a worse defect: an `==` that always
    /// answers true would give a beautifully quiet inspector showing the wrong
    /// desk's title for ever. Every fact the panel renders is flipped here, one
    /// at a time, against the real type.
    func testTheInspectorsComparisonSeesEveryFactItDraws() async throws {
        let store = await loadedStore()
        let chief = makeAgent("chief")
        var renamed = chief
        renamed.title = "a different title"
        let routines = try DeckCoding.decoder.decode(
            RoutinesResponse.self,
            from: Data("""
                {"routines": [{"id": "r1", "agent": "chief", "prompt": "stand up",
                 "trigger": {"kind": "cron", "spec": "0 8 * * 1-5", "tz": "Europe/London"},
                 "enabled": true, "next_run_at": 1757000000}]}
                """.utf8)).routines

        let ask = try XCTUnwrap(
            DeckCoding.decoder.decode(ApprovalsPage.self, from: Data("""
                {"approvals":[{"id":"apr_1","ts":1756000000.0,"agent":"acme-growth",
                  "tool":"Bash","subject":"gh pr list","cwd":"/Users/y/Projects/acme",
                  "cwd_short":"~/Projects/acme","status":"pending","options":[
                    {"reply":"once","available":true,"rule":null,"summary":"just this time"}]}]}
                """.utf8)).approvals.first)
        let waiting = AttentionItem.make(approval: ask, agents: [:])

        func panel(
            agent: Agent? = nil, error: DeckError? = nil, routines: [Routine] = [],
            problem: String? = nil, empty: String? = nil, attention: [AttentionItem] = [],
            signIns: [String: MacSignInPhase] = [:]
        ) -> SettingsPanelBody {
            SettingsPanelBody(
                agent: agent ?? chief, settingsError: error, routines: routines,
                routinesProblem: problem, routinesEmptyMessage: empty,
                attention: attention, macSignIns: signIns, delivery: .current, store: store,
                // Both dependencies rather than drawn values, and both
                // deliberately out of `==` — this test is about what a publish
                // on the store costs, and neither of these changes on one.
                screens: nil, shells: nil)
        }

        XCTAssertEqual(
            panel(routines: routines), panel(routines: routines),
            "the same inspector does not compare equal to itself, so `.equatable()` "
            + "never skips anything and the panel is rebuilt on every publish "
            + "exactly as it was before")

        XCTAssertNotEqual(panel(), panel(agent: renamed),
                          "a renamed desk leaves the inspector showing the old title")
        XCTAssertNotEqual(panel(), panel(signIns: ["h1": .waitingForYou]),
                          "a sign-in on this Mac moving on never reaches the card")
        XCTAssertNotEqual(panel(), panel(error: .unauthorized),
                          "a settings failure never reaches the panel")
        XCTAssertNotEqual(panel(), panel(routines: routines),
                          "a routine that arrived is not drawn")
        XCTAssertNotEqual(panel(), panel(problem: "the deck refused"),
                          "a routines failure is swallowed")
        XCTAssertNotEqual(panel(), panel(empty: RoutinesModel.emptyText),
                          "an empty routines list still reads as loading")
        // The tray is the one thing in this panel that is about a desk he is
        // NOT looking at, so a comparison that misses it leaves a blocked
        // session off the only screen that would have shown it.
        XCTAssertNotEqual(
            panel(), panel(attention: [waiting]),
            "an ask that arrived on another desk never reaches the tray — the "
            + "inspector is quiet and the session stays blocked, unseen")
    }

    // MARK: 4 — what a Form row costs, measured, before anyone rewrites one

    /// **The `Form` question, answered with numbers instead of a guess.**
    ///
    /// Counted by `alignmentQueries` (the calibrated probe from
    /// `BaselineFallbackTests`), 30 layout passes, marker planted as the row's
    /// content:
    ///
    /// | shape | `firstTextBaseline` queries |
    /// |---|---|
    /// | the row outside a `Form`, in a `VStack` or a `ScrollView` | **0** |
    /// | a plain row in `Form { … }.formStyle(.grouped)` | **2** |
    /// | that row holding a `Toggle` with its label hidden | **6** |
    /// | that row holding a `TextField` | **6** |
    /// | that row holding a `Button` | **2** |
    /// | `LabeledContent` | **12** |
    ///
    /// So the cost is not the `Form` on its own and it is not the stack's
    /// alignment — `.top` and `.center` read the same 6. It is a **control that
    /// carries a text baseline of its own** sitting in a grouped row.
    ///
    /// What that buys the reader: ripping `Form` out of the two sheets is worth
    /// nothing (they are not on screen), and ripping it out of the inspector
    /// buys ~2-6 queries per row per layout pass — real, but two orders below
    /// the rebuild-per-keystroke above, and it costs an appearance-changing
    /// rewrite of a 300pt panel. Fix the rebuild first, then measure live.
    func testAControlInAGroupedFormRowPaysForABaselineAndTheSameRowOutsideOneDoesNot() {
        let outside = alignmentQueries { marker in
            VStack { HStack(alignment: .top, spacing: 8) {
                marker
                Spacer(minLength: 0)
                Toggle("", isOn: .constant(true)).labelsHidden().toggleStyle(.switch)
            } }.frame(width: 300)
        }
        let plainRow = alignmentQueries { marker in
            Form { Section("S") { marker } }.formStyle(.grouped).frame(width: 300)
        }
        let rowWithAControl = alignmentQueries { marker in
            Form { Section("S") { HStack(alignment: .top, spacing: 8) {
                marker
                Spacer(minLength: 0)
                Toggle("", isOn: .constant(true)).labelsHidden().toggleStyle(.switch)
            } } }.formStyle(.grouped).frame(width: 300)
        }

        XCTAssertEqual(
            outside, 0,
            "the identical row outside a Form now asks for \(outside) text "
            + "baselines, so the comparison this whole table rests on is gone")
        XCTAssertGreaterThan(
            plainRow, 0,
            "a grouped Form row asks for no text baseline any more — the Form "
            + "suspect written up here is stale and should be deleted rather "
            + "than left misleading the next reader")
        XCTAssertGreaterThan(
            rowWithAControl, plainRow,
            "a control in a grouped row (\(rowWithAControl)) no longer costs "
            + "more than a plain one (\(plainRow)), so the reason given for "
            + "leaving these Forms alone is not the reason that was measured")
    }

    // MARK: 5 — nothing here animates while it waits on the deck

    /// **The class, not the case.** The outage was an indeterminate
    /// `ProgressView` left on the thread pane: an AppKit animation drives the
    /// window's display cycle at the refresh rate, and everything sharing that
    /// window is re-measured on every frame. `LayoutSettlesTests` carries that
    /// rule for the status strip, the approval card and the transcript.
    ///
    /// This is the same rule for the surfaces in the inspector, the sheets and
    /// the hiring pane — and the reason it is not "transient, therefore fine"
    /// is the timeout: `HTTPDeckClient` performs on `URLSession.shared` with no
    /// `timeoutIntervalForRequest` set, so the default **60 seconds** applies.
    /// A wait drawn as a spinner against a deck that is not answering is a
    /// spinner on screen for a minute — and a deck that is not answering is
    /// exactly the state this app is in when it looks wedged.
    func testNoSurfaceInThisRegionAnimatesWhileItWaitsOnTheDeck() throws {
        var offenders: [String] = []
        for name in Self.region {
            let body = try source(of: "Sources/DeckUI/\(name)")
            for hit in Self.spinners(in: body) {
                offenders.append("\(name):\(hit)")
            }
        }

        let why = "\nEvery wait on these surfaces is a request on "
            + "URLSession.shared with the default 60s timeout, so this animates "
            + "the window's display cycle for as long as the deck stays quiet. "
            + "Draw a still picture — a symbol and the sentence — the way the "
            + "status strip and the connection indicator already do."
        let report = "an indeterminate progress indicator is drawn in:\n"
            + offenders.joined(separator: "\n") + why
        XCTAssertEqual(offenders, [], report)
    }

    /// The rule has to be able to fail, and it has to read code rather than
    /// prose — a scanner with a typo in it is a green tick over the defect.
    func testTheSpinnerRuleCanActuallyFail() {
        let sample = """
        struct Bad: View {
            // ProgressView() in a comment must not count.
            var body: some View {
                ProgressView().controlSize(.small)
            }
        }
        """
        let flagged = Self.spinners(in: Self.stripComments(sample))
        XCTAssertEqual(flagged, [4], "expected exactly the one real spinner: \(flagged)")

        let innocent = """
            FlatLabel("Setting it up…", systemImage: "hourglass")
            Text(ProgressStatus.line)
            """
        let falseAlarms: [Int] = Self.spinners(in: innocent)
        XCTAssertEqual(
            falseAlarms, [],
            "the rule fires on a still picture or on a word that merely "
            + "contains \"Progress\", and will be switched off for crying wolf")
    }

    /// **The good signal.** A rule that only forbids passes over a deleted
    /// view. This says the replacement is really drawn: the pane that waits on
    /// the hire still says it is waiting, with a still symbol and the sentence
    /// `PendingThread` decided.
    func testThePaneThatWaitsStillSaysSoWithAStillPicture() throws {
        let pane = try source(of: "Sources/DeckUI/PendingThreadView.swift")

        XCTAssertTrue(
            pane.contains("systemImage: \"hourglass\""),
            "the hiring pane draws no still symbol for the wait, so removing "
            + "the spinner removed the only sign that anything is happening")
        XCTAssertTrue(
            pane.contains("pending.statusLine"),
            "the hiring pane no longer draws the status sentence at all — a "
            + "silent 60-second wait is worse than the spinner that was removed")
    }

    // MARK: 6 — and all of it still draws

    /// Absence is not a picture. Each surface is hosted for real and has to
    /// reach a size and then stop being laid out.
    func testTheseSurfacesDrawAndThenGoQuiet() async {
        let store = await loadedStore()

        let failure = settling(
            FailureView(failure: .make(.transport("the deck did not answer")), onRetry: {})
                .frame(width: 500, height: 400))
        XCTAssertGreaterThan(failure.fitting.height, 100,
                             "the failure screen never laid out to a real size")
        XCTAssertLessThan(
            failure.passes, 10,
            "the failure screen was laid out \(failure.passes) times in 0.5s with "
            + "nothing happening. It is what he stares at when the deck is away, "
            + "so it has to be the cheapest view in the app.")

        let inspector = settling(SettingsPanelView(store: store).frame(width: 320, height: 700))
        XCTAssertGreaterThan(inspector.fitting.height, 100,
                             "the inspector never laid out to a real size")
        XCTAssertLessThan(
            inspector.passes, 10,
            "the inspector was laid out \(inspector.passes) times in 0.5s with "
            + "nothing happening, and it is open by default")

        store.beginNewAgent()
        let hiring = settling(PendingThreadView(store: store,
                                                pending: PendingThread(typedText: "a scribe",
                                                                       isCreating: true))
            .frame(width: 700, height: 600))
        XCTAssertGreaterThan(hiring.fitting.height, 100,
                             "the hiring pane never laid out to a real size")
        XCTAssertLessThan(
            hiring.passes, 10,
            "the hiring pane was laid out \(hiring.passes) times in 0.5s while "
            + "waiting on the deck")
    }

    // MARK: the probes

    /// How many times SwiftUI asks a child for `firstTextBaseline` while
    /// laying `build` out. Same probe as `BaselineFallbackTests` — an
    /// `.alignmentGuide` closure **is** the `explicitAlignment` query.
    private func alignmentQueries<V: View>(_ build: (AnyView) -> V) -> Int {
        let counter = BodyCounter()
        let marker = AnyView(Text("x").alignmentGuide(.firstTextBaseline) { d in
            counter.bump()
            return d[.bottom]
        })
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: build(marker))
        probe.frame = NSRect(x: 0, y: 0, width: 900, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 900, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        for _ in 0..<30 {
            probe.needsLayout = true
            probe.layoutSubtreeIfNeeded()
            _ = probe.fittingSize
        }
        window.orderOut(nil)
        window.contentView = nil
        return counter.value
    }

    // MARK: the scanner

    /// The views this sweep owns.
    private static let region = [
        "ConnectionIndicator.swift", "FailureView.swift", "ManualAgentSheet.swift",
        "PendingThreadView.swift", "RoutinesPanelView.swift", "SettingsPanelView.swift",
        "Theme.swift",
    ]

    /// Lines drawing an indeterminate progress indicator. A determinate one
    /// (`ProgressView(value:)`) does not animate on its own and is not an
    /// offence.
    private static func spinners(in body: String) -> [Int] {
        let regex = try! NSRegularExpression(pattern: #"(?<![A-Za-z0-9_.])ProgressView\s*\((?!\s*value)"#)
        var found: [Int] = []
        for (index, line) in body.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
            let text = String(line)
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            if regex.firstMatch(in: text, range: range) != nil { found.append(index + 1) }
        }
        return found
    }

    /// One type's declaration, from its `struct` line to the next one at the
    /// top level of the file. Reading the file whole would let a rule about one
    /// view pass or fail on a different view's code.
    private static func declaration(of type: String, in body: String) throws -> String {
        let lines = body.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard let start = lines.firstIndex(where: { $0.hasPrefix(type) }) else { return "" }
        var end = lines.count
        for index in (start + 1)..<lines.count where lines[index].hasPrefix("struct ")
            || lines[index].hasPrefix("public struct ") {
            end = index
            break
        }
        return lines[start..<end].joined(separator: "\n")
    }

    private static func stripComments(_ body: String) -> String {
        body.split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces).hasPrefix("//") ? "" : String($0) }
            .joined(separator: "\n")
    }

    private func source(of relativePath: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return Self.stripComments(
            try String(contentsOf: root.appendingPathComponent(relativePath), encoding: .utf8))
    }
}

// MARK: - the counted shapes

/// Counts body evaluations across threads without pretending to be a view's
/// state — it is a reference the probe views hold, not something SwiftUI sees.
final class BodyCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func bump() { lock.lock(); n += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return n }
}

/// The shape the inspector had: it holds the store, so every publish on the
/// store re-runs this body.
private struct ObservingProbe: View {
    @ObservedObject var store: DeckStore
    let counter: BodyCounter
    var body: some View {
        counter.bump()
        return Text(store.settingsAgent?.name ?? "-").frame(width: 300, height: 400)
    }
}

/// The shape it has now: the values it draws, handed to SwiftUI as something
/// it can compare, so an unrelated publish stops at the wrapper.
private struct EquatableProbe: View {
    @ObservedObject var store: DeckStore
    let counter: BodyCounter

    private struct Content: View, Equatable {
        let name: String?
        let counter: BodyCounter
        static func == (a: Self, b: Self) -> Bool { a.name == b.name }
        var body: some View {
            counter.bump()
            return Text(name ?? "-").frame(width: 300, height: 400)
        }
    }

    var body: some View {
        Content(name: store.settingsAgent?.name, counter: counter).equatable()
    }
}
