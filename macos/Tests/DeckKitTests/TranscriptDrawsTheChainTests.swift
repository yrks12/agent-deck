import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The transcript actually draws the chain of command — and opening a rollup
/// gives him back exactly what it was hiding.**
///
/// `ChainOfCommandTests` pins the four Grok differences as values. This pins
/// that the pane on screen is built out of those values and not out of a bare
/// list of messages: the drawing order is a function `TranscriptList` computes
/// from what it was handed, so it can be read here with no screen at all —
/// which is the same property that lets the pane be `Equatable` and skipped
/// whole. A row that worked anything out for itself would fail both.
///
/// The one thing that only exists at this layer is **the rollup he has
/// opened**: which rollups are open is about what he is looking at, not about
/// the conversation, so it lives in the view and the spreading-out is asserted
/// here.
///
/// **No screen is taken.** The laid-out check at the bottom hosts the pane in a
/// borderless window parked 40,000pt off every display, never ordered front,
/// with activation policy `.prohibited`.
@MainActor
final class TranscriptDrawsTheChainTests: XCTestCase {

    // MARK: the pane is built out of the timeline, not out of bare messages

    /// One conversation with all four of them in it: two days, a run of
    /// traffic, a single relayed line, and turns from two different desks.
    func testThePaneDrawsAttributionRowsRollupsDateDividersAndTheTurnsFace() {
        let pane = TranscriptList(entries: Self.aDaysWork, desk: "cos")
        let drawn = pane.timeline(opened: [])

        XCTAssertEqual(
            drawn.compactMap(\.dayBreakText).count, 2,
            "the conversation spans two days and drew "
            + "\(drawn.compactMap(\.dayBreakText).count) date rows: \(Self.described(drawn))")
        XCTAssertEqual(
            drawn.compactMap(\.attributionText), ["Messaged Initech"],
            "the single relayed line is not announced before it is read: "
            + "\(Self.described(drawn))")
        XCTAssertEqual(
            drawn.compactMap(\.rollupText), ["3 messages with Acme"],
            "the run of three relayed lines was not collapsed into one row. He "
            + "manages outcomes and must never scroll machine chatter: "
            + "\(Self.described(drawn))")
        // Four turns, not three: the run of traffic between COS's line on the
        // 4th and its line after it ends one turn and opens another, which is
        // the whole reason the face exists — those two are not one speech.
        XCTAssertEqual(
            drawn.compactMap(\.faceDesk), ["cos", "acme", "cos", "cos"],
            "consecutive turns from different desks are indistinguishable: "
            + "\(Self.described(drawn))")
    }

    /// **The dangerous half.** A rollup that could not be opened would have
    /// solved the noise by deleting the evidence.
    func testOpeningARollupPutsBackEveryLineItWasStandingInForWithItsAttribution() {
        let pane = TranscriptList(entries: Self.aDaysWork, desk: "cos")
        let closed = pane.timeline(opened: [])
        guard let rollup = closed.compactMap(\.rollupID).first else {
            return XCTFail("nothing was rolled up at all: \(Self.described(closed))")
        }

        let open = pane.timeline(opened: [rollup])

        XCTAssertEqual(
            open.compactMap(\.entryID).filter { $0.hasPrefix("said:t") },
            ["said:t1", "said:t2", "said:t3"],
            "opening the rollup showed \(Self.described(open)) — the three lines "
            + "it claimed to be standing in for are not there")
        XCTAssertEqual(
            open.compactMap(\.attributionText).filter { $0.hasPrefix("Messaged Acme") }.count, 3,
            "the opened lines arrived with no attribution on them, so he is "
            + "reading traffic with no idea who it went to: \(Self.described(open))")
        XCTAssertTrue(
            open.compactMap(\.rollupID).contains(rollup),
            "the rollup row went away when it was opened, so there is nothing "
            + "left to click to close it again")
        XCTAssertEqual(
            closed.compactMap(\.entryID).filter { $0.hasPrefix("said:t") }, [],
            "the lines were on screen while the rollup was closed as well, so "
            + "the rollup is a label over the noise rather than instead of it")
    }

    /// Two builds of the same conversation are the same rows with the same
    /// ids — the property `ForEach` rebuilds everything without, and the one
    /// this pane already cost him a day for.
    func testTheSameConversationDrawsTheSameRowsTwice() {
        let pane = TranscriptList(entries: Self.aDaysWork, desk: "cos")

        XCTAssertEqual(
            pane.timeline(opened: []).map(\.id), pane.timeline(opened: []).map(\.id),
            "the same conversation produced different row identities on two "
            + "builds, so ForEach treats every row as new every time")
        XCTAssertEqual(
            Set(pane.timeline(opened: []).map(\.id)).count,
            pane.timeline(opened: []).count,
            "two rows share an id, so one of them will not be drawn")
    }

    /// Opening a rollup must not disturb the conversation around it, or the
    /// list re-identifies and every row is rebuilt.
    func testOpeningARollupLeavesEveryOtherRowWhereItWas() {
        let pane = TranscriptList(entries: Self.aDaysWork, desk: "cos")
        let closed = pane.timeline(opened: [])
        let rollup = closed.compactMap(\.rollupID).first ?? ""
        let open = pane.timeline(opened: [rollup])

        let untouched = open.map(\.id).filter { !$0.hasPrefix("said:t") && !$0.hasPrefix("to:t") }
        XCTAssertEqual(
            untouched, closed.map(\.id),
            "opening one rollup rearranged the rest of the conversation:\n"
            + "closed \(Self.described(closed))\nopen   \(Self.described(open))")
    }

    /// A conversation with no traffic in it draws no rollup and no attribution
    /// — a grey line over every bubble would be noise on the only traffic that
    /// is actually addressed to him.
    func testHisOwnConversationWithHisDeskDrawsNoTrafficRowsAtAll() {
        let plain = (1...6).map { Self.said("m\($0)", author: $0 % 2 == 0 ? "owner" : "cos",
                                            role: $0 % 2 == 0 ? .owner : .agent) }
        let drawn = TranscriptList(entries: plain, desk: "cos").timeline(opened: [])

        XCTAssertEqual(
            drawn.compactMap(\.rollupText), [],
            "his own conversation was rolled up: \(Self.described(drawn))")
        XCTAssertEqual(
            drawn.compactMap(\.attributionText), [],
            "his own conversation was labelled as overheard traffic: "
            + "\(Self.described(drawn))")
        XCTAssertEqual(
            drawn.compactMap(\.entryID).count, 6,
            "some of the conversation is not on screen: \(Self.described(drawn))")
    }

    // MARK: it has to survive an actual layout

    /// Values are not pixels. The pane with every kind of row in it is hosted
    /// in a real window at the narrowest column the app allows, and has to lay
    /// out to a real size — including a bubble carrying Hebrew and English,
    /// which every desk on this Mac writes.
    func testThePaneWithEveryKindOfRowInItLaysOutAtTheNarrowestColumn() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var entries = Self.aDaysWork
        entries.append(Self.said(
            "mixed", author: "cos", role: .agent,
            text: "מוכן ל-E2E של UX #15 — `studio-preview-pr15` עלה, אבל הלייב "
                + "עדיין קפוא אחרי crash-loop. The order is set by "
                + "distance-to-revenue, not by which lane is loudest."))

        let probe = NSHostingView(
            rootView: TranscriptList(entries: entries, desk: "cos").equatable())
        probe.frame = NSRect(x: 0, y: 0, width: 380, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 380, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let began = Date()
        while Date().timeIntervalSince(began) < 0.4 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        // The scrolled content, not `fittingSize`: a column is forbidden to ask
        // for more height than the window has, so a healthy transcript now has
        // an ideal height of zero and only what it *placed* can say it drew.
        // See `laidOutSize` and `AColumnFitsTheWindowItIsInTests`.
        let laidOut = probe.laidOutSize()
        window.orderOut(nil)
        window.contentView = nil

        XCTAssertGreaterThan(
            laidOut.height, 100,
            "the conversation with date rows, attribution rows, a rollup, faces "
            + "and a mixed Hebrew and English bubble in it laid out to "
            + "\(laidOut) — that is a blank pane")
    }

    // MARK: the conversation under test

    private static let day3 = date(3, hour: 7)
    private static let day4 = date(4, hour: 9)

    /// Two days, a single dispatch, a run of three, and turns from two desks.
    private static let aDaysWork: [ThreadEntry] = [
        said("m1", author: "cos", role: .agent, at: day3),
        said("m2", author: "cos", role: .agent, at: day3),
        relayed("d1", to: "initech", at: day3),
        said("m3", author: "acme", role: .agent, at: day3),
        said("m4", author: "cos", role: .agent, at: day4),
        relayed("t1", to: "acme", at: day4),
        relayed("t2", to: "acme", at: day4),
        relayed("t3", to: "acme", at: day4),
        said("m5", author: "cos", role: .agent, at: day4),
    ]

    private static func date(_ day: Int, hour: Int) -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = 9
        components.day = day
        components.hour = hour
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Europe/London")!
        return calendar.date(from: components)!
    }

    private static func said(
        _ id: String, author: String, role: MessageRole,
        at: Date = day3, text: String = "a line of the conversation"
    ) -> ThreadEntry {
        .said(TranscriptRow(
            message: Message(id: id, cursor: id, threadID: "direct:cos", author: author,
                             role: role, sentAt: at, text: text),
            attribution: .ordinary))
    }

    private static func relayed(_ id: String, to peer: String, at: Date) -> ThreadEntry {
        .said(TranscriptRow(
            message: Message(id: id, cursor: id, threadID: "peer:cos|\(peer)", author: "cos",
                             role: .agent, sentAt: at, text: "dense counts and ids"),
            attribution: .dispatch(to: peer.capitalized, threadID: "peer:cos|\(peer)")))
    }

    private static func described(_ items: [TranscriptItem]) -> String {
        "[" + items.map(\.id).joined(separator: " | ") + "]"
    }
}

private extension TranscriptItem {
    var dayBreakText: String? {
        if case .dayBreak(let stamp) = self { return stamp.text }
        return nil
    }
    var attributionText: String? {
        if case .attribution(let line) = self { return line.text }
        return nil
    }
    var rollupText: String? {
        if case .rollup(let rollup) = self { return rollup.text }
        return nil
    }
    var rollupID: String? {
        if case .rollup(let rollup) = self { return rollup.id }
        return nil
    }
    var faceDesk: String? {
        if case .turnFace(let face) = self { return face.desk }
        return nil
    }
    var entryID: String? {
        if case .entry(let entry) = self { return entry.id }
        return nil
    }
}
