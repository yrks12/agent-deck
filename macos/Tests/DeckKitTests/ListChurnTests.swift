import XCTest
import AppKit
import SwiftUI
import Combine
@testable import DeckKit
@testable import DeckUI

/// **The transcript must not be rebuilt because something else changed.**
///
/// Five rounds of this defect were spent making rows cheaper — a spinner, a
/// `Label`, the window chrome, text selection, a wall of pasted text. Every one
/// of those was a real cost and none of them was the fault. The sample taken
/// while the owner was actually using the app says why:
///
/// ```
/// 119 swift::RefCounts<…>            46 ForEachState.item(      <- NEW
/// 117 _swift_getGenericMetadata(     35 Array<A>.motionVectors( <- NEW
///  97 ViewLayoutEngine.sizeThatFits(
/// ```
///
/// `getGenericMetadata` and `RefCounts` at the top is **view-tree
/// reconstruction**, not layout of a stable tree. `ForEachState.item` is the
/// list re-evaluating its items. `motionVectors` is SwiftUI computing a
/// transition for them. Together they say the list is being treated as
/// **changed, over and over** — and every per-row cost anyone measured was just
/// the price of a rebuild that should not have been happening.
///
/// The split that proves it: hands-off, with the collapse fix in, the app reads
/// **0.0% for three minutes** with every one of those chains at zero frames.
/// Touch it — open a desk, let it work — and it pegs. His words: *"when its
/// working its stuck on my ui"*. A desk that is working is a desk sending
/// `agent_state` frames, and each one used to be a full publish of the store.
///
/// ## Three things had to be true, and all three are checked here
///
/// 1. **A publish that changes nothing must not happen at all.** `@Published`
///    fires on every `set`, equal or not.
/// 2. **A publish that changes something *else* must not reach the
///    transcript.** One store drives the sidebar and the conversation, so a
///    desk's state moving in the sidebar re-evaluated `ThreadView.body` and
///    took the whole `ForEach` with it. That one is not fixable by equality in
///    the store — the sidebar really did change — so the transcript is
///    `Equatable` and SwiftUI skips it.
/// 3. **Nothing about the list may be animated.** An animated transaction over
///    a list is what `motionVectors` is, and this app has already learned once
///    that a permanent animation drives the display cycle at the refresh rate.
///
/// **This is the first detector in the whole hunt that can actually see the
/// fault**, because publish counts and body counts are observable without a
/// display cycle. It still cannot say the app is quiet — only
/// `YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh <deck-url>` can do that, and only
/// while somebody is using it: a hands-off reading now means nothing.
@MainActor
final class ListChurnTests: XCTestCase {

    // MARK: 1 — a publish that changes nothing

    /// The roster arriving again, unchanged, must be invisible above the store.
    /// A desk that is working sends these continuously.
    func testARosterThatHasNotChangedPublishesNothingAtAll() async {
        let client = deck()
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()

        var rebuilds = 0
        let watching = store.objectWillChange.sink { _ in rebuilds += 1 }
        for _ in 0..<5 { await store.loadRoster() }
        watching.cancel()

        XCTAssertEqual(
            rebuilds, 0,
            "\(rebuilds) rebuilds of the whole pane for five roster loads that "
            + "found nothing new. Every one of them re-evaluates the ForEach over "
            + "the transcript and re-measures every row.")
    }

    /// And the same for a state frame that repeats what the app already knows.
    func testAStateFrameThatSaysWhatIsAlreadyKnownPublishesNothing() async {
        let client = deck()
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()

        // One warm-up first. The very first frame may legitimately settle
        // something — an unread count clearing because he is reading the
        // thread — and the property worth pinning is the steady state: the deck
        // re-saying what it has already said must cost nothing at all.
        await store.applyRename(to: store.agents["chief"]!, replacing: "chief")

        var rebuilds = 0
        let watching = store.objectWillChange.sink { _ in rebuilds += 1 }
        for _ in 0..<10 {
            await store.applyRename(to: store.agents["chief"]!, replacing: "chief")
        }
        watching.cancel()

        XCTAssertEqual(
            rebuilds, 0,
            "\(rebuilds) rebuilds for ten frames that said nothing new about the "
            + "desk he is reading")
    }

    // MARK: 2 — a publish that changes something else

    /// **THE structural check.** The sidebar really did change; the
    /// conversation did not. The pane may re-evaluate — it draws the desk's
    /// state — but the transcript underneath it must be skipped.
    func testTheTranscriptIsNotRebuiltWhenOnlyTheSidebarChanges() async {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        defer { Diagnostics.isEnabled = false; Diagnostics.reset() }

        let client = deck()
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.settle()

        let window = host(ThreadView(store: store).frame(width: 900, height: 700))
        defer { teardown(window) }
        pump(0.3)

        let before = Diagnostics.snapshot()
        XCTAssertGreaterThan(
            before["transcript.body"] ?? 0, 0,
            "the transcript never drew at all, so the count below cannot fall: "
            + "\(before)")

        // A desk goes to work. That is a real change to the sidebar row and to
        // the strip above the composer — and no change whatsoever to anything
        // in the conversation.
        var working = makeAgent("chief", title: "Chief of staff")
        working.state = .working
        working.preview = "on it"
        client.rosterPayload = RosterPayload(
            agents: [working],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        await store.loadRoster()
        pump(0.3)

        let after = Diagnostics.snapshot()
        XCTAssertGreaterThan(
            after["store.publishRoster"] ?? 0, before["store.publishRoster"] ?? 0,
            "the roster never republished, so nothing was actually exercised")
        XCTAssertEqual(
            after["transcript.body"], before["transcript.body"],
            "the transcript was rebuilt \((after["transcript.body"] ?? 0) - (before["transcript.body"] ?? 0)) "
            + "more times because a desk started working. Nothing in the "
            + "conversation changed. That rebuild re-evaluates every item of the "
            + "ForEach and re-measures every row — and a working desk sends these "
            + "continuously, which is 'when its working its stuck on my ui'.")
    }

    /// **The good signal, and it matters more than the one above.** A
    /// transcript that is never rebuilt is also a transcript that never shows
    /// him a reply. Skipping is only correct while the list is genuinely the
    /// same list.
    func testTheTranscriptIsStillRebuiltWhenSomethingIsActuallySaid() {
        let first = TranscriptList(entries: entries(["m1", "m2"]), openPeerThread: { _ in })
        let same = TranscriptList(entries: entries(["m1", "m2"]), openPeerThread: { _ in })
        let extra = TranscriptList(entries: entries(["m1", "m2", "m3"]), openPeerThread: { _ in })

        XCTAssertTrue(
            first == same,
            "two transcripts holding the same conversation compare unequal, so "
            + "SwiftUI rebuilds the list on every publish and the fix does nothing")
        XCTAssertFalse(
            first == extra,
            "a transcript with a new message in it compares EQUAL to one without "
            + "— his desk's reply would never appear on screen. That is worse "
            + "than the spin.")
    }

    /// Identity has to be stable across publishes too, or `ForEach` rebuilds
    /// every row even when the array compares equal.
    func testAnEntryKeepsItsIdentityAcrossRepublishes() {
        let once = ThreadTimeline.entries(rows: rows(["m1", "m2"]), toolCalls: [])
        let twice = ThreadTimeline.entries(rows: rows(["m1", "m2"]), toolCalls: [])

        XCTAssertEqual(
            once.map(\.id), twice.map(\.id),
            "the same conversation produced different identities on two builds, "
            + "so ForEach treats every row as new every time")
        XCTAssertEqual(once, twice, "the entries themselves are not stable values")
    }

    // MARK: 3 — nothing about the list is animated

    /// `Array<A>.motionVectors` is SwiftUI computing a transition across a
    /// list. The transcript used to animate its scroll to the bottom on every
    /// arriving message — and while a desk is streaming, messages arrive faster
    /// than the animation lasts, which makes it permanent. This app has already
    /// cost the owner a day for a permanent animation once.
    func testTheTranscriptDoesNotAnimateItsWayToTheBottom() throws {
        let source = try threadViewSource()

        XCTAssertFalse(
            source.contains("withAnimation"),
            "the conversation animates. `Array<A>.motionVectors` is in the live "
            + "sample at 35 frames, and an animated transaction over the "
            + "transcript re-measures every row of the LazyVStack for as long as "
            + "it runs. A desk mid-reply sends lines faster than that, so it "
            + "never stops.")
        XCTAssertFalse(
            source.contains(".animation("),
            "the conversation carries an implicit animation, which applies to "
            + "every list change including the ones nobody asked to see")
    }

    /// And it still goes to the newest line — a transcript that stops following
    /// the conversation is a transcript he has to scroll by hand all day.
    func testTheTranscriptStillFollowsTheConversationToTheNewestLine() throws {
        let source = try threadViewSource()

        XCTAssertTrue(
            source.contains("scrollTo"),
            "nothing scrolls to the newest message any more, so his desk's reply "
            + "arrives off the bottom of a pane he is looking at")
    }

    // MARK: harness

    private func deck() -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief", title: "Chief of staff")],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(
            threadID: "direct:chief",
            messages: (1...6).map {
                makeMessage("m\($0)", cursor: String(format: "%04d", $0),
                            text: "line \($0)", thread: "direct:chief")
            },
            isReadOnly: false, participants: [DeckOwner.name, "chief"])]
        client.feeds = [.emitThenFinish([])]
        return client
    }

    private func rows(_ ids: [String]) -> [TranscriptRow] {
        ids.enumerated().map { index, id in
            TranscriptRow(
                message: makeMessage(id, cursor: String(format: "%04d", index),
                                     text: "line \(id)", thread: "direct:chief"),
                attribution: .ordinary)
        }
    }

    private func entries(_ ids: [String]) -> [ThreadEntry] {
        ThreadTimeline.entries(rows: rows(ids), toolCalls: [])
    }

    private func host<V: View>(_ view: V) -> NSWindow {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: view)
        probe.frame = NSRect(x: 0, y: 0, width: 900, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 900, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        return window
    }

    private func teardown(_ window: NSWindow) {
        window.orderOut(nil)
        window.contentView = nil
    }

    private func pump(_ seconds: TimeInterval) {
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
    }

    /// The list's own source. It moved out of `ThreadView` when it became
    /// `Equatable`, and a rule that reads the wrong file is a rule that passes
    /// over the thing it was written for.
    private func threadViewSource() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI/TranscriptList.swift")
        return try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
    }
}
