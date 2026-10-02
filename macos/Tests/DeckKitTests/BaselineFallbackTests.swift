import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The driver was `Label`, and this is what a detector for it can honestly
/// be.**
///
/// The live sample, taken while the pane sat at 99-100% CPU:
///
/// ```
/// FallbackAlignment / setFont / _invalidateEffectiveFont ......... 54
/// ScrollViewUtilities / LazyVStack / measureEstimates /
///   SelectionOverlay ............................................ 52
/// ```
///
/// `testNoViewAlignsOnATextBaseline` in `LayoutSettlesTests` greps this repo
/// for `firstTextBaseline`, and **the source is clean**. So the four
/// `DeckExperiment` switches (since deleted) were built to let one live minute name the driver
/// instead of another argument. Measured against the owner's real deck — the
/// app pegs on its own within 15 seconds with nobody touching it, so this
/// needed no scrolling and no UI test, just `ps -o pcpu` after a settle:
///
/// | run | build | CPU |
/// |---|---|---|
/// | 1 | baseline | 99.1% / 100.5% / 100.0% |
/// | 2 | `DECK_X_NO_SELECTION=1` | 100.7% / 99.5% / 100.0% |
/// | 3 | `DECK_X_NO_CHROME=1` | 19.1% / 3.4% / 77.0% |
/// | 4 | `DECK_X_PLAIN_LABELS=1` | **0.1% / 0.3% / 0.0%** |
///
/// Run 4 held for 110 seconds — `1.8, 0.0, 0.0, 0.3, 0.0, 1.8, 0.3` — past
/// every ramp that has fooled this hunt before, and a `sample` of it counted
/// **zero** `FallbackAlignment` / `setFont` / `_invalidateEffectiveFont`
/// frames where every previous sample counted 54-63.
///
/// ## THAT VERDICT WAS WRONG. READ THIS BEFORE TRUSTING A GREEN HERE.
///
/// `Label` was cut from every file in `Sources/DeckUI` on the strength of run
/// 4, and the fixed build was then measured against the real deck, hands-off,
/// with no switches set:
///
/// ```
/// t=15s 0.4%   t=30s 0.0%   t=45s 100.1%   t=60s 99.2%   …   t=120s 100.0%
/// sample: FallbackAlignment / setFont / _invalidateEffectiveFont = 56
/// ```
///
/// **Still 56, with this rule fully satisfied.** The driver was run 3 — the
/// window chrome — and `DECK_X_NO_CHROME` on that same build read
/// `1.2 / 1.2 / 0.0 / 3.4 / 0.0`. See `WindowChromeTests`, and
/// `Sources/DeckUI/ThreadHeader.swift` for the fix.
///
/// Run 4's zero was a coincidence of state. All four sites
/// `DECK_X_PLAIN_LABELS` switched are conditional — an approval notice, an
/// approval problem, a read-only footer, a pretrust banner — so on a run where
/// none of them is on screen that switch changes nothing at all, and a quiet
/// reading says nothing about `Label`.
///
/// **So what is this rule worth now?** It is *precautionary*, not measured.
/// `Label` has never been shown to cost anything on this app; what has been
/// shown is that a baseline-aligned stack makes SwiftUI resolve a baseline per
/// child per pass (`testBaselineAlignmentReallyDoesGenerateExplicitAlignment
/// Traffic`) and that `FlatLabel` draws the same picture, says the same words
/// to VoiceOver and asks for nothing. That is a cheap thing to keep and a
/// defensible default. It is **not** a guarantee that the app is quiet, and
/// nobody should read a green here as one — this file was green while the app
/// pegged a core.
///
/// The only thing that says the app is quiet is
/// `YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh <deck-url>`.
///
/// ## What a detector for this can be — and what it cannot
///
/// Six probes have now been built to see this headlessly and **all six are
/// blind**, each calibrated against a shape already proved to be the bug:
///
/// 1. **Re-dirty after settling** — `0/40` for the known-bad shape and `0/40`
///    for the known-good one.
/// 2. **Display-driven layout counting**, `window.display()` 60 times — `0`
///    for both.
/// 3. **Cost per forced re-layout**, 200 rows — `0.156ms` against `0.151ms`.
/// 4. **Swizzling `-[NSControl setFont:]`**, the exact frame in the sample —
///    `0` calls for the known-bad shape.
/// 5. **A titled window with a real `NSToolbar`** — still `0`.
/// 6. **`alignmentQueries` below**, which counts real alignment-guide traffic
///    by planting a marker child where a `Label`'s icon goes. It reads `0` for
///    `firstTextBaseline` inside a `Label` — SwiftUI resolves that baseline
///    somewhere the marker never sees. Pinned in
///    `testTheProbeCannotSeeInsideALabelAndSaysSoRatherThanPassingGreen`.
///
/// A headless green about `Label` would be worth nothing and this file does not
/// pretend otherwise.
///
/// **So the detector is a source rule, and it pins the rule rather than the
/// behaviour.** It would have caught this defect the day it was written:
/// `testNoViewLetsSwiftUIResolveAnAlignmentForIt` reads every file in
/// `Sources/DeckUI` with comments stripped and fails on the constructs where
/// SwiftUI, not us, decides how an icon lines up with a sentence. A rule is
/// only worth what its calibration is worth, so two things are asserted
/// alongside it:
///
/// - **the rule can fail** — `testTheRuleCanActuallyFail` feeds the same
///   scanner a synthetic file and requires it to flag the offending line and
///   ignore the identical one in a comment;
/// - **the replacement is really there** — the good signal. A rule that passes
///   because a directory is empty, or because every call site quietly went
///   away, is the failure this project has already shipped twice.
///
/// The one construct that *is* measurable headlessly is a `Form` row: see
/// `testAFormRowResolvesATextBaselineForTheRowItDraws`. That is a live suspect
/// this branch has **not** fixed and does not claim to have fixed — the four
/// `Form`s are in sheets and the inspector, none of which was open during the
/// measurements above.
@MainActor
final class BaselineFallbackTests: XCTestCase {

    private final class Probe<Content: View>: NSHostingView<Content> {}

    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func bump() { lock.lock(); n += 1; lock.unlock() }
        var value: Int { lock.lock(); defer { lock.unlock() }; return n }
    }

    /// How many times SwiftUI asks a child for `guide` while laying `build`
    /// out. An `.alignmentGuide` closure **is** the `explicitAlignment` query,
    /// so this counts the real traffic.
    private func alignmentQueries<V: View>(
        _ guide: VerticalAlignment = .firstTextBaseline, _ build: (AnyView) -> V
    ) -> Int {
        let counter = Counter()
        let marker = AnyView(Text("x").alignmentGuide(guide) { d in
            counter.bump()
            return d[.bottom]
        })
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = Probe(rootView: build(marker))
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

    /// **The calibration that works.** It is what makes the no-baseline rule a
    /// measured fact rather than a story: asking a stack to align on a text
    /// baseline really does make SwiftUI go and resolve one, per child, per
    /// pass, and asking it to align on `.top` really does not.
    func testBaselineAlignmentReallyDoesGenerateExplicitAlignmentTraffic() {
        let onBaseline = alignmentQueries { marker in
            HStack(alignment: .firstTextBaseline) {
                Image(systemName: "gearshape.2.fill")
                marker
                Spacer(minLength: 0)
            }.frame(width: 400)
        }
        let onTop = alignmentQueries { marker in
            HStack(alignment: .top) {
                Image(systemName: "gearshape.2.fill")
                marker
                Spacer(minLength: 0)
            }.frame(width: 400)
        }

        XCTAssertGreaterThan(
            onBaseline, 0,
            "a baseline-aligned stack produced no alignment-guide queries at "
            + "all, so this probe measures nothing and the rule it calibrates "
            + "is unsupported")
        XCTAssertEqual(
            onTop, 0,
            "a `.top`-aligned stack produced \(onTop) baseline queries. The "
            + "distinction the whole rule rests on does not exist, and every "
            + "`.top` in this app was written for a reason that is not real.")
    }

    /// **And the probe is blind to `Label`, which is why the rule is a rule.**
    ///
    /// This is the sixth failed probe, recorded rather than quietly dropped.
    /// The marker sits where a `Label`'s icon goes, so if `Label` asked its
    /// icon for a text baseline this would count it. It counts none — while
    /// the live build with `Label` in it burned a whole core, and the same
    /// build without it read 0.0%. Whatever resolves that baseline is below
    /// the level a hosted view can observe.
    func testTheProbeCannotSeeInsideALabelAndSaysSoRatherThanPassingGreen() {
        let insideALabel = alignmentQueries { marker in
            Label { Text("a sentence long enough to wrap in this pane") } icon: { marker }
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: 300)
        }

        XCTAssertEqual(
            insideALabel, 0,
            "the probe now reads \(insideALabel) baseline queries inside a Label. "
            + "That is GOOD NEWS and this test is the wrong shape: a probe that "
            + "can see the driver beats a source rule, so promote it to one and "
            + "point it at the real views.")
    }

    /// **A `Form` row does resolve a text baseline, and this app still has
    /// four of them.** Measured here, headlessly, unlike everything else in
    /// this class: a row whose label is text and whose value is a control makes
    /// SwiftUI ask for `firstTextBaseline`, and plain content in the same
    /// `Form` makes it ask for nothing.
    ///
    /// Not fixed on this branch and not claimed to be. The `Form`s live in the
    /// settings inspector and two sheets; none was open during the four runs
    /// above, so nothing measured says they cost anything yet. The one live
    /// reading that would settle it is the same hands-off repro with the
    /// inspector open.
    func testAFormRowResolvesATextBaselineForTheRowItDraws() {
        let labelled = alignmentQueries { marker in
            Form { LabeledContent { Text("value") } label: { marker } }.frame(width: 400)
        }
        let plain = alignmentQueries { marker in
            Form { marker }.frame(width: 400)
        }

        XCTAssertGreaterThan(
            labelled, 0,
            "a Form row with a labelled control asks for no text baseline any "
            + "more, so the open suspect written up here is stale and the note "
            + "in this file is misleading whoever reads it next")
        XCTAssertEqual(
            plain, 0,
            "plain content inside a Form now asks for a text baseline too "
            + "(\(plain)), so the reading above says nothing about labelled rows")
    }

    // MARK: THE RULE — nothing in this app lets SwiftUI resolve an alignment

    /// Every construct where SwiftUI, not this repo, decides how an icon lines
    /// up with a sentence. The source is the only place this is visible, so the
    /// source is where it is forbidden.
    private static let forbidden: [(pattern: String, what: String)] = [
        (#"(?<![A-Za-z0-9_.])Label\s*[({]"#,
         "`Label` — it aligns its icon to its title inside SwiftUI, which is "
         + "the measured driver of the 100% CPU spin. Use `FlatLabel`."),
        (#"(?<![A-Za-z0-9_.])LabeledContent\s*[({]"#,
         "`LabeledContent` — measured, in this file, to resolve a "
         + "`firstTextBaseline` for the row it draws."),
        (#"(?<![A-Za-z0-9_.])Toggle\s*\((?!\s*"")"#,
         "a `Toggle` with a label of its own — it lines that label up with the "
         + "switch for you. Precautionary, not measured: pass `\"\"`, add "
         + "`.labelsHidden()`, and draw the words yourself."),
        (#"\.alignmentGuide\("#,
         "an explicit alignment guide — that closure IS the "
         + "`explicitAlignment` query the sample is full of."),
        (#"firstTextBaseline|lastTextBaseline"#,
         "an explicit text baseline. SwiftUI resolves one for a child that has "
         + "none through a hidden NSTextField whose font it sets on every pass."),
    ]

    /// **The detector.** It reads this repo's own view code, comments
    /// stripped, and fails on the whole class rather than on the one site that
    /// spun — judging each site by hand is precisely what let this survive two
    /// fixes.
    ///
    /// Be plain about what it is: it pins **the rule**, not the behaviour. A
    /// green here means nobody wrote a construct that has been measured to
    /// cause this, not that the app is quiet. The measurement that says the app
    /// is quiet is a minute against the real deck with `ps -o pcpu`.
    func testNoViewLetsSwiftUIResolveAnAlignmentForIt() throws {
        var offenders: [String] = []
        for file in try viewSources() {
            offenders += Self.offences(in: file.body).map { "\(file.name):\($0.line) — \($0.what)" }
        }

        XCTAssertEqual(
            offenders, [],
            "SwiftUI is being left to resolve an alignment in:\n"
            + offenders.joined(separator: "\n")
            + "\nMeasured on the owner's Mac: the build with these in it sat at "
            + "99-100% CPU with nobody touching it, and the same build with the "
            + "Labels drawn as plain .top rows read 0.0% for 110 seconds.")
    }

    /// **The rule has to be able to fail, and it has to read code rather than
    /// prose.** Without this, a scanner with a typo in its pattern is a green
    /// tick over the exact defect it was written for.
    func testTheRuleCanActuallyFail() {
        let sample = """
        struct Bad: View {
            // Label("in a comment", systemImage: "x") must not count.
            var body: some View {
                Label("drawn for real", systemImage: "x")
            }
        }
        """
        let stripped = Self.stripComments(sample)
        let flagged = Self.offences(in: stripped)

        XCTAssertEqual(flagged.count, 1, "expected exactly the one real Label: \(flagged)")
        XCTAssertEqual(flagged.first?.line, 4,
                       "the offence was reported on the wrong line: \(flagged)")

        // And the things that merely contain the word are not offences, or the
        // rule would be turned off within a week for crying wolf.
        let innocent = Self.offences(in: """
            FlatLabel(text, systemImage: "x")
                .accessibilityLabel("a spoken sentence")
            LabelledRow(caption: "Runs as", value: spec)
            Toggle("", isOn: binding).labelsHidden()
            """)
        XCTAssertTrue(
            innocent.isEmpty,
            "the rule fires on the replacement, on an accessibility label, on a "
            + "custom row type or on a deliberately unlabelled Toggle: \(innocent)")
    }

    /// **The good signal.** Every check above is an assertion that something is
    /// absent, and absence passes trivially over an empty directory, a renamed
    /// folder or a pane that stopped drawing. This says the replacement is
    /// actually on screen, in more than one place, and reads as one sentence.
    func testTheReplacementIsReallyUsedAcrossTheApp() throws {
        let files = try viewSources()
        let usingFlatLabel = files.filter { $0.body.contains("FlatLabel(") }
        let uses = files.reduce(0) { $0 + $1.body.components(separatedBy: "FlatLabel(").count - 1 }

        XCTAssertGreaterThanOrEqual(
            uses, 10,
            "only \(uses) FlatLabel call sites in the whole app. The icon-and-"
            + "sentence rows have gone somewhere else, and the rule above is "
            + "passing over views that no longer exist rather than over views "
            + "that are safe.")
        XCTAssertGreaterThanOrEqual(
            usingFlatLabel.count, 6,
            "FlatLabel is used in \(usingFlatLabel.count) files "
            + "(\(usingFlatLabel.map(\.name).joined(separator: ", "))). It replaced "
            + "Labels in the conversation, the sidebar, the settings panel and "
            + "the sheets, so a lower number means one of those was dropped.")

        // A `Label` is ONE element to VoiceOver, saying the title and nothing
        // about the glyph. Whatever replaces it has to stay one element with
        // the same words, or this fix trades a hot CPU for a screen reader
        // reading punctuation.
        let flatLabel = try source(of: "Sources/DeckUI/FlatLabel.swift")
        XCTAssertTrue(
            flatLabel.contains(".accessibilityElement(children: .combine)"),
            "FlatLabel no longer reads as a single element, so every one of "
            + "these rows is now two stops for a screen reader where a Label "
            + "was one")
        XCTAssertTrue(
            flatLabel.contains(".accessibilityLabel(text)"),
            "FlatLabel does not say its own words, so VoiceOver falls back to "
            + "whatever SwiftUI makes of an SF Symbol name")
        XCTAssertTrue(
            flatLabel.contains(".accessibilityHidden(true)"),
            "FlatLabel's glyph is not hidden from the combined element, so the "
            + "symbol's name is read out alongside the sentence")
    }

    // MARK: the pane still draws

    /// The conversation still lays out after the whole `Label` sweep, so none
    /// of the replacements above quietly stopped drawing.
    func testTheConversationStillDraws() async {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .working
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(
            threadID: "direct:chief",
            messages: (1...20).map {
                makeMessage("m\($0)", cursor: String(format: "%04d", $0),
                            text: "transcript line \($0), long enough to wrap",
                            thread: "direct:chief")
            },
            isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()

        let probe = Probe(rootView: ThreadView(store: store).frame(width: 900, height: 700))
        NSApplication.shared.setActivationPolicy(.prohibited)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 900, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let height = probe.fittingSize.height
        window.orderOut(nil)
        window.contentView = nil

        XCTAssertGreaterThan(height, 100,
                             "the conversation did not lay out to a real size")
    }

    // MARK: the scanner

    /// Every offence in one file's body, with the line it is on. Static so the
    /// self-test above runs the same code the sweep does — two scanners would
    /// mean the calibrated one is not the one guarding the app.
    private static func offences(in body: String) -> [(line: Int, what: String)] {
        var found: [(line: Int, what: String)] = []
        let lines = body.split(separator: "\n", omittingEmptySubsequences: false)
        for (index, line) in lines.enumerated() {
            for rule in forbidden {
                guard let regex = try? NSRegularExpression(pattern: rule.pattern) else {
                    XCTFail("the pattern `\(rule.pattern)` does not compile, so this "
                            + "rule is silently switched off")
                    continue
                }
                let text = String(line)
                let range = NSRange(text.startIndex..<text.endIndex, in: text)
                if regex.firstMatch(in: text, range: range) != nil {
                    found.append((index + 1, rule.what))
                }
            }
        }
        return found
    }

    private static func stripComments(_ body: String) -> String {
        body.split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces).hasPrefix("//") ? "" : String($0) }
            .joined(separator: "\n")
    }

    /// Every SwiftUI file in the app, comments stripped. Blanked rather than
    /// dropped, so a reported line number is the line in the real file.
    private func viewSources() throws -> [(name: String, body: String)] {
        let directory = repoRoot.appendingPathComponent("Sources/DeckUI")
        let files = try FileManager.default.contentsOfDirectory(at: directory,
                                                                includingPropertiesForKeys: nil)
        let swift = files.filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        XCTAssertGreaterThan(swift.count, 8, "only \(swift.count) view sources found at "
                             + "\(directory.path) — this sweep would pass over almost nothing")
        return try swift.map {
            ($0.lastPathComponent, try source(of: "Sources/DeckUI/\($0.lastPathComponent)"))
        }
    }

    private var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func source(of relativePath: String) throws -> String {
        Self.stripComments(
            try String(contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8))
    }
}
