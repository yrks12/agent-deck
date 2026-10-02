import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The conversation pane writes nothing into the window's chrome.**
///
/// This is the rule the outage actually bought, and it was bought twice
/// because the first answer was wrong.
///
/// Four switch runs pointed at `Label`, and `Label` was cut everywhere in
/// `Sources/DeckUI`. Then the fixed build was measured against the real deck,
/// hands-off, no switches:
///
/// ```
/// t= 15s   0.4%    t= 30s   0.0%    t= 45s 100.1%    t= 60s  99.2%
/// t= 75s 100.0%    t= 90s  99.8%    t=105s  99.5%    t=120s 100.0%   STAT RN
/// sample: FallbackAlignment / setFont / _invalidateEffectiveFont = 56
/// ```
///
/// **Still 56.** The same build with `DECK_X_NO_CHROME=1` — which removed
/// `.navigationSubtitle` and the toolbar item, and nothing else — read
/// `1.2 / 1.2 / 0.0 / 3.4 / 0.0` over 100 seconds and talked to the deck 23
/// times in 50s. So the driver is the chrome, and the earlier `Label` verdict
/// was a coincidence of state: all four sites `DECK_X_PLAIN_LABELS` switched
/// are conditional — an approval notice, an approval problem, a read-only
/// footer, a pretrust banner — and on a run where none of them is on screen
/// that switch changes nothing at all.
///
/// ## What the loop is
///
/// `.navigationSubtitle` is an `NSTextField` in the titlebar and a toolbar item
/// is an `NSControl`. Writing either from a view's `body` closes a circuit:
/// `setFont:` -> `_invalidateEffectiveFont` -> `invalidateIntrinsicContentSize`
/// -> `setNeedsUpdateConstraints` -> `NSWindow._postWindowNeedsUpdateConstraints`
/// -> layout -> `body` -> write it again. It never converges.
///
/// The shape of the measurement says which write starts it: quiet for 30-45
/// seconds, then pegged. The subtitle's text does not change while a thread is
/// open — the desk is the same desk — but `screen.connection` does, exactly
/// once the stream settles or first drops. That is the state change that
/// reaches an `NSControl`, and it arrives at the time the peg arrives.
///
/// ## Why both writes are gone rather than the likelier one
///
/// Cutting one end of this loop and shipping it has now failed once, on this
/// same defect, at a cost of a day. The subtitle was the weaker suspect but it
/// is not free: it is rebuilt (`compactMap`, `filter`, `joined`) and pushed at
/// AppKit on every body evaluation of the pane, and whether SwiftUI drops an
/// equal push into a titlebar text field is not observable from any test in
/// this repo. Both pieces of information are worth more inside the pane
/// anyway — the desk's name and whether the stream is up are things he reads
/// while looking at the conversation, not at the titlebar — so `ThreadHeader`
/// draws them in ordinary SwiftUI, where a layout pass ends.
///
/// The window title is untouched and still says which deck this is. It is set
/// once, in `DeckApp`, from a launch-time constant — never from view state.
///
/// ## What these checks are worth
///
/// **They pin a rule. They cannot see the loop.** Seven probes have now been
/// built to catch this class headlessly and every one is blind; a test hosts a
/// borderless window with no titlebar and no toolbar, so the second half of
/// that chain does not exist in the harness. A green here means nobody wrote
/// the construct that was measured to cause this — not that the app is quiet.
/// The only thing that says the app is quiet is
/// `YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh <deck-url>`, and it takes a
/// minute.
@MainActor
final class WindowChromeTests: XCTestCase {

    // MARK: the rule

    /// Nothing in the app's views writes the window's chrome.
    func testNoViewWritesTheWindowsChrome() throws {
        var offenders: [String] = []
        for file in try viewSources() {
            for (index, line) in file.body.split(separator: "\n", omittingEmptySubsequences: false)
                .enumerated()
            where line.contains(".navigationSubtitle(") || line.contains(".navigationTitle(") {
                offenders.append("\(file.name):\(index + 1)")
            }
        }

        XCTAssertEqual(
            offenders, [],
            "these write the window's titlebar from a view body: "
            + "\(offenders.joined(separator: ", ")). The titlebar subtitle is an "
            + "NSTextField; setting it invalidates its intrinsic size, dirties "
            + "the window's constraints and schedules the layout pass that sets "
            + "it again. Measured on the owner's Mac at 100% CPU, hands-off, "
            + "within 45 seconds of launch. Draw it in the pane instead.")
    }

    /// And no model state reaches an `NSToolbar`.
    ///
    /// The settings button stays: its content is a fixed glyph, and the run
    /// that read `1.2 / 1.2 / 0.0 / 3.4 / 0.0` still had it on screen. What is
    /// forbidden is a toolbar item whose *content* is rebuilt from the store —
    /// that is an `NSControl` being handed a new view every time the deck says
    /// anything.
    func testNoModelStateReachesTheToolbar() throws {
        var offenders: [String] = []
        for file in try viewSources() {
            for block in Self.toolbarBlocks(in: file.body) {
                for banned in ["ConnectionIndicator", "screen.", "store."]
                where block.contains(banned) {
                    offenders.append("\(file.name): toolbar item built from `\(banned)`")
                }
            }
        }

        XCTAssertEqual(
            offenders, [],
            "\(offenders.joined(separator: ", ")). A toolbar item is an NSControl. "
            + "Rebuilding one from state that the deck changes — the connection, "
            + "the open screen — is the write that starts the layout loop, and "
            + "the 30-45 second delay before the app pegs is how long it takes "
            + "the stream to change it for the first time.")
    }

    /// **The rule has to be able to fail.** Two scanners that disagree would
    /// mean the calibrated one is not the one guarding the app, so this runs
    /// the same block extractor the sweep does.
    func testTheToolbarRuleCanActuallyFail() {
        let bad = """
        .toolbar {
            ToolbarItem(placement: .principal) {
                ConnectionIndicator(state: screen.connection)
            }
        }
        """
        let good = """
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { showInspector.toggle() } label: {
                    Image(systemName: "sidebar.right")
                }
            }
        }
        """

        let badBlocks = Self.toolbarBlocks(in: bad)
        XCTAssertEqual(badBlocks.count, 1, "the extractor found \(badBlocks.count) toolbar "
                       + "blocks in a source that has exactly one")
        XCTAssertTrue(badBlocks.first?.contains("ConnectionIndicator") ?? false,
                      "the extractor cut the block short and would miss the offence")

        let goodBlocks = Self.toolbarBlocks(in: good)
        XCTAssertEqual(goodBlocks.count, 1)
        for banned in ["ConnectionIndicator", "screen.", "store."] {
            XCTAssertFalse(
                goodBlocks.first?.contains(banned) ?? true,
                "the rule fires on the settings button, which is a fixed glyph and "
                + "was on screen for the run that measured flat")
        }
    }

    // MARK: the good signal — the information is still on screen

    /// Every check above is an assertion that something is **absent**, and the
    /// cheapest way to satisfy all of them is to delete the desk's name and the
    /// connection dot. He asked for that dot. It has moved, not gone.
    func testTheDeskAndTheConnectionAreStillDrawnSomewhere() throws {
        let files = try viewSources()
        let drawsTheDot = files.filter { $0.body.contains("ConnectionIndicator(") }
        let drawsTheDesk = files.filter { $0.body.contains("headerTitle") }

        XCTAssertFalse(
            drawsTheDot.isEmpty,
            "nothing in the app draws ConnectionIndicator any more. The rule "
            + "above passes by deleting the one thing that says whether the "
            + "stream is up, which he asked for by name.")
        XCTAssertFalse(
            drawsTheDesk.isEmpty,
            "nothing draws the desk's name. It came out of the titlebar and did "
            + "not go anywhere, so the pane no longer says who he is talking to.")
    }

    /// And it draws to a real size, in every connection state, at the **same**
    /// height each time.
    ///
    /// This one is behaviour, not a rule. A header whose height moved when the
    /// dot changed would re-measure the whole transcript underneath it on every
    /// connection event — which is the `LazyVStack.measureEstimates` cost this
    /// project has already paid once, for the same reason, with a spinner.
    func testTheHeaderIsTheSameHeightWhateverTheConnectionIsDoing() {
        let states: [ConnectionState] = [.live, .idle, .connecting, .reconnecting(attempt: 4)]
        let heights = states.map {
            fittingHeight(ThreadHeader(title: "Chief", subtitle: "Chief of staff", connection: $0))
        }

        XCTAssertGreaterThan(
            heights.first ?? 0, 1,
            "the header never laid out to a real size (\(heights)), so the "
            + "equality below is the equality of four blank rows")
        XCTAssertEqual(
            Set(heights).count, 1,
            "the header is \(heights) points tall across "
            + "\(states.map(\.label)) — so every time the stream drops or comes "
            + "back, everything under it is re-measured.")
    }

    /// A desk with no subtitle must not collapse the row either, for the same
    /// reason: selecting one agent then another would resize the transcript.
    func testTheHeaderIsTheSameHeightWithOrWithoutASubtitle() {
        let withSubtitle = fittingHeight(
            ThreadHeader(title: "Chief", subtitle: "Chief of staff", connection: .live))
        let without = fittingHeight(
            ThreadHeader(title: "Chief", subtitle: nil, connection: .live))

        XCTAssertGreaterThan(withSubtitle, 1, "the header did not lay out")
        XCTAssertEqual(
            withSubtitle, without,
            "the header is \(withSubtitle) points with a subtitle and \(without) "
            + "without one, so switching between two desks re-measures the "
            + "transcript")
    }

    // MARK: harness

    private func fittingHeight<V: View>(_ view: V) -> CGFloat {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: view.frame(width: 700))
        probe.frame = NSRect(x: 0, y: 0, width: 700, height: 200)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 700, height: 200),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let height = probe.fittingSize.height
        window.orderOut(nil)
        window.contentView = nil
        return height
    }

    /// The body of every `.toolbar { … }` in a source, brace-matched.
    ///
    /// Brace counting is only as good as the source it reads — a `{` inside a
    /// string literal would throw it off. There is none in this app's toolbars,
    /// and `testTheToolbarRuleCanActuallyFail` is what notices if the extractor
    /// starts cutting blocks short.
    private static func toolbarBlocks(in body: String) -> [String] {
        var blocks: [String] = []
        var search = body.startIndex
        while let marker = body.range(of: ".toolbar {", range: search..<body.endIndex) {
            var depth = 0
            var index = body.index(before: marker.upperBound)   // the opening brace
            var end = body.endIndex
            while index < body.endIndex {
                if body[index] == "{" { depth += 1 }
                if body[index] == "}" {
                    depth -= 1
                    if depth == 0 { end = body.index(after: index); break }
                }
                index = body.index(after: index)
            }
            blocks.append(String(body[marker.lowerBound..<end]))
            search = end
        }
        return blocks
    }

    /// Every SwiftUI file in the app, comments stripped — a rule that read its
    /// own explanation would fail on the sentence describing the fix.
    private func viewSources() throws -> [(name: String, body: String)] {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let directory = root.appendingPathComponent("Sources/DeckUI")
        let swift = try FileManager.default
            .contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        XCTAssertGreaterThan(swift.count, 8, "only \(swift.count) view sources found at "
                             + "\(directory.path) — this sweep would pass over almost nothing")
        return try swift.map { url in
            let body = try String(contentsOf: url, encoding: .utf8)
                .split(separator: "\n", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces).hasPrefix("//") ? "" : String($0) }
                .joined(separator: "\n")
            return (url.lastPathComponent, body)
        }
    }
}
