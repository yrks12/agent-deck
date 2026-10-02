import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The panel that goes where the raw workspace path is.**
///
/// The reference product draws a live thumbnail of the agent's browser in the
/// inspector, captioned `COS's screen`, and reveals an **Open** button on
/// hover. This app draws
/// `Working in /home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17`.
///
/// This file holds the panel to three rules the rest of the app already lives
/// under, because a picture that refreshes once a second in an always-open
/// inspector is the single easiest way to undo the CPU work:
///
/// 1. **It is a value, and it is quiet.** `DeckStore.composerDraft` is
///    `@Published`, so one character typed into the composer is one publish on
///    the store. `InspectorIsQuietTests` counts 21 body evaluations across 20
///    keystrokes for a view that observes the store and 1 for an equatable
///    one. This panel is counted the same way, in the same harness.
/// 2. **Nothing in it animates while it waits.** The reference draws a
///    spinner for its `Connecting` state (`grok-03`). This app deleted every
///    indeterminate `ProgressView` after four measured runs: an AppKit
///    animation drives the window's display cycle and re-measures everything
///    sharing that window on every frame. The word is kept; the spin is not.
/// 3. **Every state draws, and reaches a size.** Absence is not a picture —
///    "no frame was shown" is also what a view that failed to lay out looks
///    like. Each of the five is hosted for real here.
@MainActor
final class AgentScreenPanelTests: XCTestCase {

    // MARK: harness (the same two probes InspectorIsQuietTests uses)

    private final class LayoutProbe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() { passes += 1; super.layout() }
    }

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

    @discardableResult
    private func bodyEvaluations<V: View>(
        of view: V, whileTyping publishes: Int, into store: DeckStore, counter: BodyCounter
    ) -> Int {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 320, height: 400)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 320, height: 400),
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

    private func settling<V: View>(
        _ view: V, settle: TimeInterval = 0.4, watch: TimeInterval = 0.4
    ) -> (passes: Int, fitting: CGSize) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = LayoutProbe(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 320, height: 320)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 320, height: 320),
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

    // MARK: 1 — a keystroke in the composer must not redraw the picture

    /// The calibration and the rule in one run, exactly as
    /// `InspectorIsQuietTests` does it: the observing shape has to read high
    /// or this probe measures nothing.
    func testAKeystrokeDoesNotRebuildTheScreenPanel() async {
        let store = await loadedStore()

        let observed = BodyCounter()
        let observing = bodyEvaluations(
            of: StoreObservingScreenProbe(store: store, counter: observed),
            whileTyping: 20, into: store, counter: observed)
        XCTAssertGreaterThan(
            observing, 15,
            "a view that observes the store re-evaluated its body only \(observing) "
            + "times across 20 keystrokes, so this probe cannot see the defect and "
            + "the count below is worthless")

        // **The panel must be measured where it actually lives: underneath a
        // view that observes the store.** A panel hosted on its own is quiet no
        // matter what its `==` says, because nothing above it ever invalidates
        // — an earlier version of this test did exactly that and went on
        // passing with `==` hardcoded to `false`. `ScreenPanelHost` is the
        // inspector's shape: it holds the store, so a keystroke re-runs *it*
        // 21 times, and whether the panel underneath re-runs with it is the
        // thing being measured.
        //
        // The counted view wraps the real panel and delegates `==` to the real
        // comparison, so what is measured is the shipped skip — no test-only
        // hook is compiled into the app, and the panel's own body can only run
        // when this one does.
        // **The rule is that the cost does not grow with what he types**, so it
        // is measured as a slope and not as a magic number: the same panel,
        // under twice as many keystrokes, must run its body the same number of
        // times. Pinning a single constant would pin whatever set-up passes
        // this harness happens to make today; a slope of zero is the property.
        let shot = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                    receivedAt: Date())
        let panel = AgentScreenPanelBody(
            presentation: AgentScreenPresentation(desk: "Atlas", state: .live(shot)),
            isHovering: false, workspace: Self.workspace, onOpen: {})

        // **Best of three, because this is a measurement and not a decision.**
        // The harness stands a real window up and pumps a run loop; under the
        // whole suite the machine sometimes fails to settle inside the pump and
        // a sample comes back as though nothing were skipped at all. That is
        // interference, not a rebuild — the same test alone is clean every
        // time. The least-interfered sample is the honest reading of a cost,
        // and it cannot hide the defect: when the comparison is genuinely
        // broken *every* sample is one-per-character, so the minimum is 21 of
        // 20 and 41 of 40.
        func evaluations(underKeystrokes count: Int) -> Int {
            (0..<3).map { _ in
                let counter = BodyCounter()
                return bodyEvaluations(
                    of: ScreenPanelHost(store: store, panel: panel, counter: counter),
                    whileTyping: count, into: store, counter: counter)
            }.min() ?? .max
        }

        let twenty = evaluations(underKeystrokes: 20)
        let forty = evaluations(underKeystrokes: 40)

        // **The bound is a fraction of the keystrokes, not a constant.** The
        // harness brings a real window up and pumps a run loop, so a handful of
        // set-up passes land here and how many varies with what else the
        // machine is doing — an earlier constant of 3 read 2 alone and 8 under
        // the full suite. What may never happen is the count *tracking* what he
        // typed: the defect is one rebuild per character, so anything under one
        // per two characters cannot be it, and the broken shape lands at 21 of
        // 20 and 41 of 40 rather than anywhere near the line.
        for (keystrokes, count) in [(20, twenty), (40, forty)] {
            XCTAssertLessThan(
                count, keystrokes / 2,
                "the screen panel ran its body \(count) times across \(keystrokes) "
                + "keystrokes. Against \(observing) for the observing shape over 20, "
                + "that is a rebuild per character — and this panel holds a JPEG and "
                + "is drawn in the always-open inspector, so it is the CPU defect "
                + "coming straight back.")
        }
    }

    /// The comparison has to see everything the panel draws, or the fix above
    /// becomes a worse defect: a panel that never updates its picture.
    func testThePanelsComparisonSeesEveryFactItDraws() {
        let now = Date(timeIntervalSince1970: 6_000_000)
        let first = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                     receivedAt: now)
        let second = AgentScreenFrame(jpeg: Self.otherJPEG, serverAge: 0.2,
                                      display: ":99", receivedAt: now)

        func panel(_ state: AgentScreenState, hovering: Bool = false) -> AgentScreenPanelBody {
            AgentScreenPanelBody(
                presentation: AgentScreenPresentation(desk: "Atlas", state: state),
                isHovering: hovering, workspace: Self.workspace, onOpen: {})
        }

        XCTAssertEqual(panel(.live(first)), panel(.live(first)),
                       "the same panel does not compare equal to itself, so "
                       + "`.equatable()` never skips anything")
        XCTAssertNotEqual(panel(.live(first)), panel(.live(second)),
                          "a NEW FRAME compares equal to the old one, so the picture "
                          + "would freeze on whatever arrived first")
        XCTAssertNotEqual(panel(.live(first)), panel(.stale(first, age: 120)),
                          "a frame going stale is not seen, so a dead feed keeps "
                          + "drawing as live")
        XCTAssertNotEqual(panel(.live(first)), panel(.noComputer),
                          "the machine going away is not seen")
        XCTAssertNotEqual(panel(.live(first)), panel(.connecting),
                          "the wait is not seen")
        XCTAssertNotEqual(panel(.live(first)), panel(.live(first), hovering: true),
                          "hovering does not change the panel, so the Open button "
                          + "never appears")
    }

    // MARK: 2 — the way in is offered only over something to open

    func testHoveringRevealsOpenOverAFrameAndNotOverAnEmptySlot() {
        let shot = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                    receivedAt: Date())

        XCTAssertTrue(AgentScreenPresentation(desk: "Atlas", state: .live(shot)).showsOpen)
        XCTAssertTrue(AgentScreenPresentation(desk: "Atlas",
                                              state: .stale(shot, age: 90)).showsOpen,
                      "a stale picture is still a running machine worth taking over — "
                      + "that is the whole reason he is looking")
        XCTAssertFalse(AgentScreenPresentation(desk: "Atlas", state: .noComputer).showsOpen)
        XCTAssertFalse(AgentScreenPresentation(desk: "Atlas", state: .connecting).showsOpen)
    }

    /// **The way in is drawn, and the pointer only decorates it.**
    ///
    /// This rule used to be the opposite one: it required the literal `"Open"`
    /// and treated the hover reveal as the feature. That reveal *was* the
    /// defect — *"i need to be able to controll the google chrom"* against a
    /// take-over that had shipped weeks earlier. A control that exists only
    /// while the pointer rests on a 300-point picture, below the fold of the
    /// narrowest column in the window, is a control nobody finds.
    ///
    /// So hover stays — it is what a pointer expects, and `isHovering` has to
    /// remain a compared value or the panel's `==` is a lie — but it may never
    /// be what *creates* the way in. The measured half of this is
    /// `TheTakeOverIsOneClickFromWhereHeIsNeededTests`, which lays the panel out
    /// with the pointer nowhere near it.
    func testTheWayIntoTheTakeOverIsDrawnWithoutTheHover() throws {
        let source = try Self.source(of: "Sources/DeckUI/AgentScreenPanel.swift")
        XCTAssertTrue(source.contains(".onHover"),
                      "nothing in the panel reacts to the pointer at all, so there is "
                      + "no affordance saying the picture can be pressed")
        XCTAssertTrue(source.contains("Takeover.screenTitle"),
                      "the panel draws no named way into the take-over")
        XCTAssertTrue(source.contains("isHovering"),
                      "the hover state is not a value the body draws from, so it "
                      + "cannot be compared and cannot be tested")
        XCTAssertFalse(
            source.contains("showsOpen && isHovering"),
            "the way into the take-over is gated on the pointer being on top of "
            + "it again. He reported the take-over as missing for exactly this "
            + "reason: nothing on screen said it was there.")
    }

    // MARK: 2b — the raw path this panel replaces is still reachable

    /// **`Working in /home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17`
    /// is a developer's string in a manager's product**, and it is what the
    /// inspector draws in the slot this panel is for. Taking it off the face of
    /// the panel must not delete it: the folder is the one fact a person
    /// debugging a desk actually needs, and it is two clicks from here rather
    /// than a line he reads every time he opens the inspector.
    ///
    /// So it is carried, it is copyable by name, and it is in the tooltip —
    /// and the headline the panel says out loud is the *screen*, not the path.
    func testTheWorkspacePathIsCarriedAndCopyableRatherThanDrawnAsALine() {
        let shot = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                    receivedAt: Date())
        let path = "~/.claude/agent-bus/workspaces/new-hire-77ec17"
        let panel = AgentScreenPanelBody(
            presentation: AgentScreenPresentation(desk: "Atlas", state: .live(shot)),
            isHovering: false, workspace: path, onOpen: {})

        XCTAssertEqual(panel.workspace, path,
                       "the panel dropped the workspace path altogether, so the one "
                       + "fact the old line carried is now nowhere in the product")
        XCTAssertTrue(panel.helpText.contains(path),
                      "the path is not in the tooltip, so nothing on this panel can "
                      + "tell him where the desk is working: \(panel.helpText)")
        XCTAssertTrue(panel.helpText.hasPrefix(panel.presentation.spokenLabel),
                      "the tooltip leads with the folder instead of with what the "
                      + "picture is showing: \(panel.helpText)")
        XCTAssertEqual(panel.presentation.spokenLabel,
                       "Atlas's screen, live. Captured just now.",
                       "the panel announces itself as a folder rather than as a screen")
        XCTAssertEqual(AgentScreenPanelBody.copyWorkspaceAction, "Copy workspace path",
                       "the copy action has no name, so it cannot be reached by "
                       + "keyboard or read by a screen reader")
    }

    /// A desk the deck reported no workspace for says nothing about one, rather
    /// than offering an action that copies an empty string.
    func testAPanelWithNoWorkspaceOffersNoPathAtAll() {
        let panel = AgentScreenPanelBody(
            presentation: AgentScreenPresentation(desk: "Atlas", state: .noComputer),
            isHovering: false, workspace: nil, onOpen: {})
        XCTAssertEqual(panel.helpText, panel.presentation.spokenLabel,
                       "a desk with no workspace still had something appended to its "
                       + "tooltip: \(panel.helpText)")
    }

    /// The path is a drawn fact, so the comparison has to see it change — a
    /// desk swapped in the inspector must not keep the previous one's folder.
    func testTheComparisonSeesTheWorkspaceChange() {
        func panel(_ workspace: String?) -> AgentScreenPanelBody {
            AgentScreenPanelBody(
                presentation: AgentScreenPresentation(desk: "Atlas", state: .connecting),
                isHovering: false, workspace: workspace, onOpen: {})
        }
        XCTAssertEqual(panel("~/a"), panel("~/a"))
        XCTAssertNotEqual(panel("~/a"), panel("~/b"),
                          "the panel kept the previous desk's workspace path")
        XCTAssertNotEqual(panel("~/a"), panel(nil),
                          "a desk that lost its workspace still shows the old one")
    }

    // MARK: 3 — nothing here animates while it waits on the deck

    /// The reference's Connecting slot is a spinner. This app's is not, for the
    /// reason written up in `ConnectionIndicator` and `InspectorIsQuietTests`:
    /// an indeterminate `ProgressView` drives the window's display cycle for as
    /// long as the deck stays quiet, and a request on `URLSession.shared` has
    /// the default 60-second timeout.
    func testTheConnectingStateIsAStillPictureAndNotASpinner() throws {
        let source = try Self.source(of: "Sources/DeckUI/AgentScreenPanel.swift")
        let regex = try NSRegularExpression(
            pattern: #"(?<![A-Za-z0-9_.])ProgressView\s*\((?!\s*value)"#)
        var lines: [Int] = []
        for (index, line) in source.split(separator: "\n",
                                          omittingEmptySubsequences: false).enumerated() {
            let text = String(line)
            if regex.firstMatch(in: text,
                                range: NSRange(text.startIndex..., in: text)) != nil {
                lines.append(index + 1)
            }
        }
        XCTAssertEqual(lines, [],
                       "an indeterminate progress indicator is drawn at "
                       + "AgentScreenPanel.swift:\(lines). This panel sits in the "
                       + "always-open inspector; a spinner there animates the window's "
                       + "display cycle for the whole time the deck is quiet.")

        // The good signal: the wait still says it is waiting.
        XCTAssertEqual(
            AgentScreenPresentation(desk: "Atlas", state: .connecting).statusLine,
            "Connecting",
            "removing the spinner removed the only sign that anything is happening")
        XCTAssertNotNil(
            AgentScreenPresentation(desk: "Atlas", state: .connecting).symbol,
            "the wait has no still symbol beside it")
    }

    // MARK: 4 — and all of it draws

    func testEveryScreenStateDrawsAndThenGoesQuiet() {
        let shot = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                    receivedAt: Date())
        let states: [(String, AgentScreenState)] = [
            ("connecting", .connecting),
            ("live", .live(shot)),
            ("stale", .stale(shot, age: 240)),
            ("age unknown", .ageUnknown(shot)),
            ("no computer", .noComputer),
            ("unavailable", .unavailable(.dockerUnavailable("Docker is not answering"))),
        ]

        for (name, state) in states {
            let panel = AgentScreenPanelBody(
                presentation: AgentScreenPresentation(desk: "Atlas", state: state),
                isHovering: false, workspace: Self.workspace, onOpen: {})
            let result = settling(panel.equatable().frame(width: 300))
            XCTAssertGreaterThan(result.fitting.height, 40,
                                 "the \(name) state never laid out to a real size")
            XCTAssertLessThan(result.passes, 10,
                              "the \(name) state was laid out \(result.passes) times in "
                              + "0.4s with nothing happening")
        }
    }

    // MARK: helpers

    /// The smallest thing `NSImage` will accept as a JPEG, so no picture of
    /// anybody's signed-in browser is ever committed to this repo. A captured
    /// frame is a photograph of a logged-in session; it does not go in a
    /// fixture, a test or a commit.
    static let jpeg: Data = makeJPEG(grey: 0.2)
    static let otherJPEG: Data = makeJPEG(grey: 0.8)

    /// The very string this panel is replacing on his screen, home-relative the
    /// way `Agent.shortWorkspace` writes it.
    static let workspace = "~/.claude/agent-bus/workspaces/new-hire-77ec17"

    private static func makeJPEG(grey: CGFloat) -> Data {
        let image = NSImage(size: NSSize(width: 8, height: 5))
        image.lockFocus()
        NSColor(white: grey, alpha: 1).drawSwatch(in: NSRect(x: 0, y: 0, width: 8, height: 5))
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let jpeg = rep.representation(using: .jpeg, properties: [:]) else {
            return Data([0xFF, 0xD8, 0xFF, 0xE0])
        }
        return jpeg
    }

    private static func source(of relativePath: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(relativePath),
                          encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces).hasPrefix("//") ? "" : String($0) }
            .joined(separator: "\n")
    }
}

/// **The inspector's shape, with the real panel inside it.**
///
/// The host observes the store, so every character typed into the composer
/// re-runs *this* body — that is the pressure the panel has to survive. The
/// panel is handed values and given to SwiftUI behind `.equatable()`, exactly
/// as `AgentScreenPanel` ships it, so an unrelated publish stops at the wrapper
/// instead of rebuilding a view holding a JPEG.
///
/// Without a store observer above it, a hosted panel is quiet whatever its `==`
/// returns, and the measurement is worth nothing.
private struct ScreenPanelHost: View {
    @ObservedObject var store: DeckStore
    let panel: AgentScreenPanelBody
    let counter: BodyCounter

    var body: some View {
        // Read something off the store so this body genuinely depends on it.
        VStack {
            Text(store.settingsAgent?.name ?? "-")
            CountedPanel(inner: panel, counter: counter).equatable()
        }
        .frame(width: 300)
    }
}

/// The real panel, with a count around it. `==` is the panel's own, so the
/// skip being measured is the shipped one.
private struct CountedPanel: View, Equatable {
    let inner: AgentScreenPanelBody
    let counter: BodyCounter
    static func == (a: CountedPanel, b: CountedPanel) -> Bool { a.inner == b.inner }
    var body: some View {
        counter.bump()
        return inner
    }
}

/// The shape the panel must not have: it holds the store, so every character
/// typed into the composer re-runs it.
private struct StoreObservingScreenProbe: View {
    @ObservedObject var store: DeckStore
    let counter: BodyCounter
    var body: some View {
        counter.bump()
        return Text(store.settingsAgent?.name ?? "-").frame(width: 300, height: 200)
    }
}
