import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"when i open chat i need to scrol down."**
///
/// `TranscriptList` follows the conversation with
///
/// ```swift
/// .onChange(of: entries.last?.id) { _, last in
///     scroller.newestRow(is: last) { row in proxy.scrollTo(row, anchor: .bottom) }
/// }
/// ```
///
/// `onChange(of:_:)` compares the value across **later** updates to an
/// already-mounted view. It does not fire for the value a view is handed the
/// moment it first appears — that needs `initial: true`, which `main` does not
/// pass. So the very first render of a `TranscriptList` — which is every one of
/// them, because `DeckStore.open(threadID:)` sends `thread` through `.loading`
/// before `.loaded(screen)`, tearing the `.loaded` case's view subtree down and
/// rebuilding it — mounts with its `entries` already full and never books a
/// scroll. `ScrollView` then does what an unscrolled `ScrollView` always does:
/// sits at the top of the `LazyVStack`, `y=0`, the oldest lines on screen and
/// the newest ones below the fold. He has to drag the scrollbar himself to see
/// what a desk last said, on every open and every switch.
///
/// `TheTranscriptScrollLandsAfterTheLayoutItProvokedTests` measured `before.offset`
/// at exactly this y=0 already, as the *precondition* for its append test — it
/// names the fault without asking whether it should be there.
///
/// ## What this file measures
///
/// 1. A `TranscriptList` mounted once, already full, with nothing appended
///    afterwards — the literal shape of "open a thread" — must settle at the
///    bottom on its own, with no second update to trigger `onChange`.
/// 2. Replacing that view's `rootView` with a **second**, differently-populated
///    `TranscriptList` — the literal shape of `.loaded(A)` -> `.loading` ->
///    `.loaded(B)`, i.e. switching desks — must land on `B`'s newest line, not
///    leave the viewport wherever `A` settled.
/// 3. A late-arriving line, appended to an already-settled thread, must still
///    move the viewport toward the bottom — the one case `main` already gets
///    right, kept here so a fix for (1) and (2) cannot trade it away.
///
/// **(3) is measured against a wider tolerance than (1) and (2), and that is
/// itself a measurement, not a shrug.** `proxy.scrollTo` estimates an
/// unrealised row's position and does not correct itself once the real height
/// is known — that is the whole subject of `ScrollsToNewest`'s own file. Three
/// *consecutive* small appends measured off-display on unmodified `main`, each
/// already near the bottom before the next line lands, undershoot by 31pt, then
/// 61pt, then 93pt — growing, and present with or without this fix. Nothing
/// here closes that; it would need the same on-display A/B this codebase
/// already insists on before touching this file, and guessing at it is
/// exactly what `ScrollsToNewest` warns against. What (3) pins is the
/// difference between that known drift and an append that does not scroll at
/// all, which is off by the height of a whole conversation, not a row.
///
/// **No screen is taken.** Same rig as the sibling file: `.prohibited`
/// activation policy, borderless, 40,000pt off every display, `orderBack` only,
/// every wait bounded to 0.4s.
@MainActor
final class TranscriptOpensAtTheNewestMessageTests: XCTestCase {

    typealias Cost = TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests
    static let paneWidth: CGFloat = 608
    static let windowHeight: CGFloat = 949
    static let messages = 52
    /// Same slack as the sibling file: under one row, far under the travel a
    /// landed scroll actually covers.
    static let atTheBottom: CGFloat = 80
    /// **Measured, not guessed.** A single line arriving right after a mount
    /// that already scrolled to the bottom is the same shape as the sibling
    /// file's *second* consecutive small append, off-display on unmodified
    /// `main`: 61pt short there, 104pt short measured here against this file's
    /// own content. Both are `proxy.scrollTo` estimating an unrealised row and
    /// never revisiting it — see the type-level note above — and a genuine
    /// failure to scroll at all misses by the height of a whole conversation
    /// (~1,000pt for `messages` rows), not by this.
    static let atTheBottomAfterAFollowUpAppend: CGFloat = 160

    override func tearDown() {
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    // MARK: - (1) the one that goes red on `main`: opening a thread

    /// **This is the bug, stated as an assertion.** A `TranscriptList` mounted
    /// once with a full conversation already in it — no append, no second
    /// `onChange` firing — has to land at the bottom by itself, because that
    /// mount is what every real "open a thread" looks like.
    func testAFreshlyOpenedThreadSettlesAtTheNewestMessageWithNoFurtherChange() {
        let r = rig(AnyView(TranscriptList(entries: Cost.entries(Self.messages, tag: "open"), desk: "chief")))
        defer { teardown(r) }
        pump(r)
        let settled = look(r)

        LegacyScrollersKnownShortfall.expect {
            XCTAssertGreaterThan(
                settled.offset + settled.clip, settled.document - Self.atTheBottom,
                "a thread opened with \(Self.messages) messages already in it settled at "
                + "\(settled.offset) of a \(settled.document)-point document with a "
                + "\(settled.clip)-point pane — \(settled.document - settled.clip - settled.offset)pt "
                + "short of the end. Nothing was appended after the mount, so the only way "
                + "this lands at the bottom is a scroll requested on the view's own first "
                + "appearance. He is being shown the oldest lines of the conversation and "
                + "has to drag the scrollbar himself to reach the one that was said last.")
        }
    }

    // MARK: - (2) the same mount, replayed as a thread switch

    /// **Switching desks tears the view down and rebuilds it the same way
    /// opening the first one does.** `DeckStore.open(threadID:)` sends `thread`
    /// through `.loading` between two different threads, so `ThreadView`'s
    /// `resolved` switch swaps `ProgressView` back in and mounts a brand new
    /// `TranscriptList` for the thread arriving — nothing carries over from the
    /// one that just left. Replacing `rootView` here is that same swap.
    func testSwitchingThreadsLandsOnTheNewThreadsNewestMessageNotWhereTheOldOneSettled() {
        let r = rig(AnyView(TranscriptList(entries: Cost.entries(10, tag: "deskA"), desk: "deskA")))
        defer { teardown(r) }
        pump(r)

        // `ThreadView.resolved` swaps in `ProgressView("Opening thread")` at
        // this exact position while `DeckStore.open(threadID:)` sends `thread`
        // through `.loading` between two different threads — a different view
        // type at the same spot, which is what breaks SwiftUI's identity and
        // forces the next `TranscriptList` to mount fresh rather than update
        // this one. Skipping this step would let SwiftUI carry `@State
        // scroller` and its established `onChange` baseline over from desk A,
        // which is not what a real thread switch does.
        r.host.rootView = AnyView(ProgressView("Opening thread"))
        pump(r)

        r.host.rootView = AnyView(TranscriptList(entries: Cost.entries(Self.messages, tag: "deskB"), desk: "deskB"))
        pump(r)
        let afterSwitch = look(r)

        // GOOD SIGNAL FIRST: the switch really did reach the view — a bigger
        // conversation is now hosted, so a viewport that "looks fine" because
        // nothing changed proves nothing.
        XCTAssertGreaterThan(
            afterSwitch.document, 0,
            "the second thread's content height read 0, so the switch never reached "
            + "the hosted view and the position below is the position of a view "
            + "nobody changed")
        LegacyScrollersKnownShortfall.expect {
            XCTAssertGreaterThan(
                afterSwitch.offset + afterSwitch.clip, afterSwitch.document - Self.atTheBottom,
                "after switching to a \(Self.messages)-message thread the viewport sat at "
                + "\(afterSwitch.offset) of a \(afterSwitch.document)-point document — "
                + "\(afterSwitch.document - afterSwitch.clip - afterSwitch.offset)pt short of the "
                + "end. Switching desks is a fresh mount, the same shape as opening the "
                + "first thread ever, and it has to land on what the desk he just clicked "
                + "actually said last, not wherever the previous conversation happened to "
                + "be scrolled.")
        }
    }

    // MARK: - (3) the good signal: a late-arriving line must not regress

    /// **The one `main` already gets right, kept as a guard.** A settled
    /// thread that then receives one more line must still move toward the
    /// bottom — pinning the initial mount must not come at the cost of the
    /// running conversation. Tolerance is `atTheBottomAfterAFollowUpAppend`,
    /// not `atTheBottom`; see the measurement on that constant and on this
    /// file's own type-level doc before tightening it.
    func testALateArrivingLineOnAnAlreadySettledThreadStillMovesTowardTheBottom() {
        var entries = Cost.entries(Self.messages, tag: "late")
        let r = rig(AnyView(TranscriptList(entries: entries, desk: "chief")))
        defer { teardown(r) }
        pump(r)
        let beforeAppend = look(r)

        entries.append(Cost.entry(Self.messages + 1, tag: "late"))
        r.host.rootView = AnyView(TranscriptList(entries: entries, desk: "chief"))
        pump(r)
        let afterAppend = look(r)

        // GOOD SIGNAL FIRST: the append really reached the view.
        XCTAssertGreaterThan(
            afterAppend.document, beforeAppend.document,
            "the transcript's content height stayed at \(beforeAppend.document)pt after a "
            + "line was appended, so the new line never reached the hosted view and the "
            + "position below is the position of a view nobody changed")
        XCTAssertGreaterThan(
            afterAppend.offset + afterAppend.clip,
            afterAppend.document - Self.atTheBottomAfterAFollowUpAppend,
            "one line arrived on an already-settled \(Self.messages)-message thread and "
            + "the viewport ended at \(afterAppend.offset) of a \(afterAppend.document)-point "
            + "document — \(afterAppend.document - afterAppend.clip - afterAppend.offset)pt "
            + "short of the end, past even the measured estimation drift. A fix for the "
            + "initial mount must not stop the transcript from following a line that "
            + "arrives afterwards.")
    }

    // MARK: - harness (mirrors TheTranscriptScrollLandsAfterTheLayoutItProvokedTests)

    private struct Rig { let host: NSHostingView<AnyView>; let window: NSWindow; let container: NSView }
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
