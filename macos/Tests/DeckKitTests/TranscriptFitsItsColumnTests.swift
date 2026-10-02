import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **His words are running off the right-hand side of the pane.**
///
/// `.grok-reference/ours-04-clipping.jpg`: the transcript's text crosses the
/// edge of the conversation column and disappears under the inspector, cut
/// mid-word. A vertical `ScrollView` does not scroll sideways, so everything
/// past that edge is not "off screen" — it is **gone**.
///
/// ## This is the same fault as the freeze, seen from the other side
///
/// The 98.8% CPU sample is `LazySubviewPlacements.placeSubviews` ->
/// `LazyStack.place` -> `ForEachList.applyNodes`, thirty wrapper nodes deep,
/// never converging. That is SwiftUI **placing subviews whose width it cannot
/// determine**: the row's width comes from its text, the text's wrap comes from
/// the width it is given, and the result feeds back into the container. An
/// undetermined content width is what produces both the spin and the overflow.
///
/// So this file measures **the laid-out width of a real row inside a real
/// column**, and it is the half of the defect a headless test can actually see.
/// The other half — the graph that never settles — needs a visible window on a
/// real display; see `TranscriptSettlesTests` for what that costs and why it is
/// not measured here.
///
/// ## How the measurement works
///
/// A `GeometryReader` behind the row reports the width the row was *actually
/// laid out at*, after a real layout in a real (never-shown) window. If that
/// number is larger than the column has room for, the difference is text he
/// cannot read. Verified against a deliberately-overflowing control below, so
/// this cannot become decoration.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless window
/// parked 40,000pt off every display, never ordered front.
@MainActor
final class TranscriptFitsItsColumnTests: XCTestCase {

    /// The window's own minimum is 900pt (`DeckAppMain`), and the sidebar and
    /// the inspector can each be dragged down to 260pt (`Theme`) — so 380pt is
    /// the narrowest the conversation column can ever be. Everything from there
    /// to a wide window has to hold.
    static let columns: [CGFloat] = [380, 420, 520, 640, 900, 1400]

    // MARK: the good signal — every kind of row fits the column it is drawn in

    func testALongReplyFitsTheColumnAtEveryWidthTheWindowAllows() {
        assertFits(
            { TranscriptRowView(row: self.agentRow(Self.wall), openPeerThread: { _ in }) },
            "a long reply from the desk")
    }

    func testHisOwnLongMessageFitsTheColumnAtEveryWidth() {
        assertFits(
            { TranscriptRowView(row: self.ownerRow(Self.wall), openPeerThread: { _ in }) },
            "his own pasted mandate")
    }

    /// §6.2 traffic is indented, ruled and inset — it has the least room of any
    /// row in the app and is the one that overflows first.
    func testRelayedTrafficFitsTheColumnAtEveryWidth() {
        assertFits(
            { TranscriptRowView(row: self.relayedRow(Self.wall), openPeerThread: { _ in }) },
            "a relayed line from another desk")
    }

    /// **Hebrew and English in one bubble.** Every desk on this Mac writes like
    /// this, and a mixed-direction line is where a width bug shows up as text
    /// vanishing off the *left* instead of the right.
    func testAMixedHebrewAndEnglishLineFitsTheColumnAndStillLaysOut() {
        assertFits(
            { TranscriptRowView(row: self.agentRow(Self.mixedDirection), openPeerThread: { _ in }) },
            "a line written in Hebrew and English")
    }

    /// **The state he was actually in when he screenshotted it.** The
    /// inspector is presented, so the conversation column is the detail column
    /// minus the panel. If the row's width is taken from a container that still
    /// spans the panel, the text runs underneath it — which is exactly what
    /// `ours-04-clipping.jpg` shows.
    func testALongReplyFitsTheColumnWhileTheInspectorIsOpen() {
        let recorder = Recorder()
        let row = TranscriptRowView(row: agentRow(Self.wall), openPeerThread: { _ in })
            .background(WidthReporter(recorder: recorder))
        let inspectorWidth: CGFloat = 280
        let content = NavigationSplitView {
            Color.clear.frame(width: 260)
        } detail: {
            ScrollView {
                LazyVStack(spacing: 8) { row }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
            }
            .inspector(isPresented: .constant(true)) {
                Color.clear.inspectorColumnWidth(inspectorWidth)
            }
        }

        let window: CGFloat = 1200
        drawOffScreen(content, width: window, height: 700)

        // Whatever SwiftUI gives the columns, the row may never be wider than
        // the window minus the two panels beside it.
        let room = window - 260 - inspectorWidth - 32
        XCTAssertGreaterThan(recorder.size.height, 1, "nothing drew with the inspector open")
        XCTAssertLessThanOrEqual(
            recorder.size.width, room + 0.5,
            String(format:
                "with the inspector open, a long reply was laid out %.0fpt wide "
                + "in a column that has at most %.0fpt — %.0fpt of it is under "
                + "the settings panel and unreachable, because a vertical "
                + "ScrollView does not scroll sideways.",
                recorder.size.width, room, recorder.size.width - room))
    }

    // MARK: calibration — the measurement has to be able to fail

    /// A row that deliberately demands more width than the column has. If this
    /// reports "fits", every assertion above is worthless.
    private struct Overflowing: View {
        var body: some View {
            HStack {
                Color.red.frame(width: 900, height: 12)
                Spacer(minLength: 60)
            }
        }
    }

    func testTheMeasurementCanSeeARowThatOverflowsItsColumn() {
        let column: CGFloat = 380
        let measured = laidOut({ Overflowing() }, column: column)

        XCTAssertGreaterThan(
            measured.width, Self.room(in: column),
            String(format:
                "a row demanding 900pt was reported as %.0fpt inside a %.0fpt "
                + "column, so this harness cannot see an overflow at all",
                measured.width, column))
    }

    // MARK: harness

    /// What the row actually has: the column, less the transcript's own
    /// horizontal padding either side.
    static func room(in column: CGFloat) -> CGFloat { column - 32 }

    private func assertFits<V: View>(
        _ row: @escaping () -> V, _ what: String,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        for column in Self.columns {
            let measured = laidOut(row, column: column)
            let room = Self.room(in: column)

            // GOOD SIGNAL FIRST: a row that never drew is inside every column.
            XCTAssertGreaterThan(
                measured.height, 1,
                "\(what) did not lay out at all at a \(Int(column))pt column, so "
                + "the width below is the width of nothing",
                file: file, line: line)

            XCTAssertLessThanOrEqual(
                measured.width, room + 0.5,
                String(format:
                    "%@ was laid out %.0fpt wide in a %.0fpt column that has "
                    + "%.0fpt of room — %.0fpt of what his desk said is off the "
                    + "right-hand side of a view that only scrolls vertically, "
                    + "cut mid-word. That is ours-04-clipping.jpg.",
                    what, measured.width, column, room, measured.width - room),
                file: file, line: line)
        }
    }

    private final class Recorder: @unchecked Sendable {
        var size: CGSize = .zero
    }

    private struct WidthReporter: View {
        let recorder: Recorder
        var body: some View {
            GeometryReader { proxy in
                Color.clear.onAppear {
                    if proxy.size.width > recorder.size.width { recorder.size = proxy.size }
                }
            }
        }
    }

    /// Lays a view out in a real, never-shown window and pumps until it
    /// settles. Nothing is returned: whatever is being measured reports itself
    /// through a `Recorder`.
    private func drawOffScreen<V: View>(_ view: V, width: CGFloat, height: CGFloat) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: width, height: height)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < 0.5 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        window.orderOut(nil)
        window.contentView = nil
    }

    /// The row, drawn inside the same container geometry `TranscriptList` uses,
    /// in a real window, and asked how wide it ended up.
    private func laidOut<V: View>(_ row: @escaping () -> V, column: CGFloat) -> CGSize {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let recorder = Recorder()
        let content = ScrollView {
            LazyVStack(spacing: 8) {
                row().background(WidthReporter(recorder: recorder))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
        }
        .frame(width: column, height: 700)

        let probe = NSHostingView(rootView: content)
        probe.frame = NSRect(x: 0, y: 0, width: column, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: column, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()

        let began = Date()
        while Date().timeIntervalSince(began) < 0.35 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        window.orderOut(nil)
        window.contentView = nil
        return recorder.size
    }

    // MARK: what a desk actually writes

    /// Over `BubbleWidth.pinAboveCharacters` and under the collapse threshold,
    /// so this is the case that is drawn in full at a pinned width — the one in
    /// the screenshot.
    static let wall = String(repeating:
        "The order is set by distance-to-revenue, not by which lane is loudest, "
        + "because the product already exists and the only gap is selling it. ",
        count: 5)

    static let mixedDirection =
        "מוכן ל-E2E של UX #15 — `studio-preview-pr15` עלה, אבל הלייב עדיין קפוא "
        + "אחרי crash-loop. Bake בודק שוב, ואני מעדכן ב-PASS/FAIL מחר ב-21:00. "
        + "The order is set by distance-to-revenue, not by which lane is loudest."

    private func message(_ text: String, author: String, role: MessageRole, thread: String) -> Message {
        Message(id: "m1", cursor: "0001", threadID: thread, author: author,
                role: role, sentAt: Date(timeIntervalSince1970: 1_756_000_000), text: text)
    }

    private func agentRow(_ text: String) -> TranscriptRow {
        TranscriptRow(
            message: message(text, author: "chief", role: .agent, thread: "direct:chief"),
            attribution: .ordinary)
    }

    private func ownerRow(_ text: String) -> TranscriptRow {
        TranscriptRow(
            message: message(text, author: DeckOwner.name, role: .owner, thread: "direct:chief"),
            attribution: .ordinary)
    }

    private func relayedRow(_ text: String) -> TranscriptRow {
        TranscriptRow(
            message: message(text, author: "chief", role: .agent, thread: "peer:chief|acme"),
            attribution: .dispatch(to: "Acme", threadID: "peer:chief|acme"))
    }
}
