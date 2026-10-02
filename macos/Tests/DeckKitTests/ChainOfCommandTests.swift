import XCTest
@testable import DeckKit

/// **He manages a chain of command without reading the chain.**
///
/// Held next to xAI's Grok Bot, the owner said: *"we are long we from their
/// experience"*. Four of the six differences he measured off those frames are
/// about one thing — the conversation says **who talked to whom**, and it
/// refuses to make him scroll past machine chatter to find the answer he asked
/// for.
///
/// | Grok draws | Ours drew |
/// |---|---|
/// | `Messaged 🟣 Initech` between turns | bubbles run together, no idea who |
/// | `41 messages with 🔵🟡🟢 3 Bots`, one line | all 41, or nothing |
/// | `Thu, Sep 3 7:10 AM` between days | an undated wall |
/// | a small face after the last bubble of a turn | nothing |
///
/// All four are decided **here**, as values, off the entries the view already
/// receives. Nothing in this file needs a screen, and nothing in the view layer
/// is allowed to decide any of it — that is what keeps `TranscriptList` a
/// value-taking `Equatable` view and keeps the 100% CPU defect shut.
///
/// ## The one place this deliberately does not copy Grok
///
/// Grok rolls up "a run of messages". Ours rolls up a run of **relayed**
/// lines — §6.2 traffic between this desk and another one — and never the
/// desk's own answers to him, however many of them arrive in a row. Collapsing
/// those would hide the reply he is waiting for behind a grey line that reads
/// like machine chatter, which is the one failure worse than the noise.
final class ChainOfCommandTests: XCTestCase {

    // MARK: 1 — attribution between turns

    /// A single piece of relayed traffic is announced before it is read, and
    /// the row names the desk, carries its wire name for the face, and knows
    /// the conversation it came from.
    func testEveryPieceOfRelayedTrafficIsAnnouncedBeforeItIsRead() {
        let items = ThreadTimeline.items(
            [ordinary("m1"), dispatch("m2", to: "initech")], desk: "cos",
            formatting: .fixed)

        guard let line = items.compactMap(\.attributionLine).first else {
            return XCTFail("nothing said who this line went to: \(described(items))")
        }
        XCTAssertEqual(
            line.text, "Messaged Initech",
            "a dispatch to another desk is announced as '\(line.text)'. Grok "
            + "writes 'Messaged Initech' between the turns, and that sentence "
            + "is how he sees his COS talked to a report without reading the "
            + "chain.")
        XCTAssertEqual(
            line.desk, "initech",
            "the attribution row carries '\(line.desk)' as the desk, so the tiny "
            + "face beside it is derived from the wrong name and does not match "
            + "the one in the sidebar")
        XCTAssertEqual(
            line.peerThreadID, "peer:cos|initech",
            "the row does not know which conversation it came from, so there is "
            + "nowhere for a click to go")

        // Immediately before the line it is about, never floating at the top.
        let position = items.firstIndex { $0.attributionLine != nil }
        XCTAssertEqual(
            items[(position ?? 0) + 1].entryID, "said:m2",
            "the attribution row is not directly above the line it attributes: "
            + "\(described(items))")
    }

    func testAReplyFromAnotherDeskSaysItCameFromThatDesk() {
        let items = ThreadTimeline.items(
            [reply("m1", from: "initech-ux")], desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.attributionLine).map(\.text),
            ["Message from Initech UX"],
            "a reply that came back reads as \(described(items)). Grok says "
            + "'Message from Initech UX' — the direction is the whole point, "
            + "because one of them is his desk delegating and the other is a "
            + "report answering.")
    }

    /// The owner's own conversation with his desk carries no attribution at
    /// all. A grey line over every bubble would be noise on the only traffic
    /// that is actually addressed to him.
    func testTheDesksOwnAnswersToHimAreNotAnnouncedAsTraffic() {
        let items = ThreadTimeline.items(
            [ordinary("m1"), ordinary("m2", author: "owner", role: .owner)],
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.attributionLine), [],
            "his own conversation is being labelled as overheard traffic: "
            + "\(described(items))")
    }

    // MARK: 2 — the rollup

    /// **The sweep.** Every run length, not one convenient one. A run of one is
    /// announced and drawn; a run of two or more is one grey line.
    func testEveryRunOfRelayedTrafficIsRolledUpFromTwoUpwards() {
        for length in [1, 2, 3, 4, 7, 41] {
            let run = (1...length).map { dispatch("d\($0)", to: "initech") }
            let items = ThreadTimeline.items(
                [ordinary("m0")] + run + [ordinary("m9")], desk: "cos",
                formatting: .fixed)
            let rollups = items.compactMap(\.rollup)
            let relayedDrawn = items.compactMap(\.entryID).filter { $0.hasPrefix("said:d") }

            if length == 1 {
                XCTAssertEqual(
                    rollups.count, 0,
                    "a single relayed line was rolled up into '\(rollups.first?.text ?? "")'. "
                    + "Grok rolls up 2 and upwards and shows one line as itself — "
                    + "hiding one message behind a summary of one message is "
                    + "strictly worse than showing it.")
                XCTAssertEqual(
                    relayedDrawn, ["said:d1"],
                    "the single relayed line is not on screen at all: \(described(items))")
            } else {
                XCTAssertEqual(
                    rollups.count, 1,
                    "a run of \(length) relayed lines produced \(rollups.count) "
                    + "rollups. He must never scroll past machine chatter: "
                    + "\(described(items))")
                XCTAssertEqual(
                    rollups.first?.count, length,
                    "the rollup miscounts what it is hiding")
                XCTAssertEqual(
                    relayedDrawn, [],
                    "\(relayedDrawn.count) of the \(length) rolled-up lines are "
                    + "still drawn beside the rollup that claims to replace them")
            }

            // Whatever the run does, the conversation either side of it is
            // untouched.
            XCTAssertEqual(
                items.compactMap(\.entryID).filter { $0.hasPrefix("said:m") },
                ["said:m0", "said:m9"],
                "at run length \(length) the ordinary conversation around the "
                + "traffic was disturbed: \(described(items))")
        }
    }

    /// The line names the count and the desks, exactly as Grok's does.
    func testTheRollupNamesTheCountAndTheDesksInIt() {
        let one = ThreadTimeline.items(
            (1...5).map { dispatch("d\($0)", to: "initech") }, desk: "cos",
            formatting: .fixed).compactMap(\.rollup).first
        XCTAssertEqual(
            one?.text, "5 messages with Initech",
            "one desk's traffic reads as '\(one?.text ?? "nothing")'")

        let many = ThreadTimeline.items(
            [dispatch("d1", to: "initech"),
             reply("d2", from: "initech-ux"),
             dispatch("d3", to: "acme"),
             reply("d4", from: "acme")],
            desk: "cos", formatting: .fixed).compactMap(\.rollup).first
        XCTAssertEqual(
            many?.text, "4 messages with 3 Bots",
            "traffic across three desks reads as '\(many?.text ?? "nothing")'. "
            + "Grok writes '41 messages with 3 Bots'.")
        XCTAssertEqual(
            many?.faces, ["initech", "initech-ux", "acme"],
            "the rollup does not carry each desk's wire name, so the tiny faces "
            + "on it cannot match the sidebar")
    }

    /// **Expandable, so nothing is hidden for good.** A rollup that dropped
    /// what it summarised would be the machine-chatter problem solved by
    /// deleting the evidence.
    func testARollupStillHoldsEveryLineItIsStandingInFor() {
        let run = (1...9).map { dispatch("d\($0)", to: "initech") }
        let rollup = ThreadTimeline.items(run, desk: "cos", formatting: .fixed)
            .compactMap(\.rollup).first

        XCTAssertEqual(
            rollup?.hidden.map(\.id),
            run.map(\.id),
            "the rollup is not holding the run it replaced, so opening it can "
            + "only show him nothing")
    }

    /// **THE safety check.** His desk answering him is not machine chatter,
    /// however much of it arrives at once.
    func testAnAnswerHeIsWaitingForIsNeverCollapsed() {
        let answer = (1...20).map { ordinary("a\($0)") }
        let items = ThreadTimeline.items(answer, desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.rollup), [],
            "twenty consecutive replies from the desk he is talking to were "
            + "collapsed into a grey line. That is the reply he asked for, "
            + "hidden behind something that reads like traffic between bots.")
        XCTAssertEqual(
            items.compactMap(\.entryID).filter { $0.hasPrefix("said:a") }.count, 20,
            "some of the desk's own answer is not on screen: \(described(items))")
    }

    // MARK: 3 — date dividers

    func testADayBreakIsDrawnBeforeTheFirstLineAndAtEveryChangeOfDay() {
        let items = ThreadTimeline.items(
            [ordinary("m1", at: Self.day(3, hour: 7, minute: 10)),
             ordinary("m2", at: Self.day(3, hour: 9)),
             ordinary("m3", at: Self.day(4, hour: 0, minute: 54)),
             ordinary("m4", at: Self.day(6, hour: 18))],
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.dayBreak).map { plain($0.text) },
            ["Thu, Sep 3 at 7:10 AM", "Fri, Sep 4 at 12:54 AM", "Sun, Sep 6 at 6:00 PM"],
            "the thread is an undated wall: \(described(items))")
        XCTAssertEqual(
            items.first?.dayBreak.map { plain($0.text) }, "Thu, Sep 3 at 7:10 AM",
            "the conversation does not open with a date, so the oldest thing on "
            + "screen could be from any day at all")
    }

    func testEverythingSaidOnOneDayShareAOneDayBreak() {
        let items = ThreadTimeline.items(
            (1...12).map { ordinary("m\($0)", at: Self.day(3, hour: $0)) },
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.dayBreak).count, 1,
            "twelve messages on one day drew "
            + "\(items.compactMap(\.dayBreak).count) date rows")
    }

    /// A tool call the deck could not stamp sorts last and has no day. It must
    /// not invent one, and it must not swallow the day of the line after it.
    func testAnEntryWithNoTimeOnItDoesNotInventADate() {
        let items = ThreadTimeline.items(
            ThreadTimeline.entries(
                rows: [row(ordinary("m1", at: Self.day(3, hour: 7, minute: 10)))],
                toolCalls: [undatedCard()]),
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.dayBreak).map { plain($0.text) }, ["Thu, Sep 3 at 7:10 AM"],
            "an undated tool call produced a date row: \(described(items))")
        XCTAssertTrue(
            items.compactMap(\.entryID).contains("tool:ask1"),
            "the undated tool call fell out of the conversation entirely")
    }

    // MARK: 6 — the turn's face

    func testTheDeskGetsItsFaceAfterTheLastBubbleOfItsTurn() {
        let items = ThreadTimeline.items(
            [ordinary("m1"), ordinary("m2"), ordinary("m3")],
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.turnFace).map(\.desk), ["cos"],
            "three bubbles from one desk drew "
            + "\(items.compactMap(\.turnFace).count) faces — Grok draws exactly "
            + "one, under the last bubble of the turn")
        XCTAssertEqual(
            items.last?.turnFace?.desk, "cos",
            "the face is not after the final bubble of the turn: \(described(items))")
    }

    func testTwoDesksSpeakingInTurnAreTellableApart() {
        let items = ThreadTimeline.items(
            [ordinary("m1", author: "cos"),
             ordinary("m2", author: "cos"),
             ordinary("m3", author: "acme"),
             ordinary("m4", author: "cos")],
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.turnFace).map(\.desk), ["cos", "acme", "cos"],
            "consecutive turns from different desks are indistinguishable: "
            + "\(described(items))")
    }

    /// He knows which one is him — his bubbles are blue and on the right. A
    /// face under them is noise.
    func testHisOwnLinesGetNoFace() {
        let items = ThreadTimeline.items(
            [ordinary("m1", author: "owner", role: .owner),
             ordinary("m2", author: "owner", role: .owner)],
            desk: "cos", formatting: .fixed)

        XCTAssertEqual(
            items.compactMap(\.turnFace), [],
            "his own messages were given a face: \(described(items))")
    }

    // MARK: the list has to be a stable list

    /// `ForEach` rebuilds every row when identity moves, and this list is the
    /// one that pegged a core. Two builds of the same conversation must be the
    /// same values with the same ids.
    func testTheSameConversationBuildsTheSameListTwice() {
        let entries = [ordinary("m1", at: Self.day(3, hour: 7)),
                       dispatch("d1", to: "initech"),
                       dispatch("d2", to: "acme"),
                       ordinary("m2", at: Self.day(4, hour: 7))]
        let once = ThreadTimeline.items(entries, desk: "cos", formatting: .fixed)
        let twice = ThreadTimeline.items(entries, desk: "cos", formatting: .fixed)

        XCTAssertEqual(once.map(\.id), twice.map(\.id),
                       "the same conversation produced different identities on two "
                       + "builds, so ForEach treats every row as new every time")
        XCTAssertEqual(once, twice, "the items themselves are not stable values")
        XCTAssertEqual(Set(once.map(\.id)).count, once.count,
                       "two rows share an id, so one of them will not be drawn")
    }

    func testAnEmptyConversationDrawsNothingAtAll() {
        XCTAssertEqual(
            ThreadTimeline.items([], desk: "cos", formatting: .fixed), [],
            "an empty thread produced rows, so 'No messages yet' will never show")
    }

    // MARK: harness

    /// Fixed calendar, locale and zone, so the date rows above are the same
    /// sentence on every machine that runs this.
    private static let zone = TimeZone(identifier: "Europe/London")!

    private static func day(_ day: Int, hour: Int, minute: Int = 0) -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = 9
        components.day = day
        components.hour = hour
        components.minute = minute
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar.date(from: components)!
    }

    private func message(
        _ id: String, author: String, role: MessageRole, thread: String, at: Date
    ) -> Message {
        Message(id: id, cursor: id, threadID: thread, author: author, role: role,
                sentAt: at, text: "line \(id)")
    }

    private func ordinary(
        _ id: String, author: String = "cos", role: MessageRole = .agent,
        at: Date = ChainOfCommandTests.day(3, hour: 7, minute: 10)
    ) -> ThreadEntry {
        .said(TranscriptRow(
            message: message(id, author: author, role: role, thread: "direct:cos", at: at),
            attribution: .ordinary))
    }

    /// Wire name in, shown name out — exactly what `RelayLine.attribution`
    /// does with the roster's `displayName`, and the reason the rows carry
    /// **both**: what he reads is the title, what the face is derived from is
    /// the wire name.
    private static let shownNames = [
        "initech": "Initech",
        "initech-ux": "Initech UX",
        "acme": "Acme",
    ]

    private static func shown(_ wire: String) -> String { shownNames[wire] ?? wire }

    private func dispatch(
        _ id: String, to peer: String,
        at: Date = ChainOfCommandTests.day(3, hour: 7, minute: 10)
    ) -> ThreadEntry {
        .said(TranscriptRow(
            message: message(id, author: "cos", role: .agent,
                             thread: "peer:cos|\(peer)", at: at),
            attribution: .dispatch(to: Self.shown(peer), threadID: "peer:cos|\(peer)")))
    }

    private func reply(
        _ id: String, from peer: String,
        at: Date = ChainOfCommandTests.day(3, hour: 7, minute: 10)
    ) -> ThreadEntry {
        .said(TranscriptRow(
            message: message(id, author: peer, role: .agent,
                             thread: "peer:cos|\(peer)", at: at),
            attribution: .reply(from: Self.shown(peer), threadID: "peer:cos|\(peer)")))
    }

    private func row(_ entry: ThreadEntry) -> TranscriptRow {
        guard case .said(let row) = entry else { preconditionFailure("not a said entry") }
        return row
    }

    private func undatedCard() -> ApprovalCard {
        ApprovalCard(
            approvalID: "ask1", title: "gh pr create", tool: "Bash", at: nil,
            runsOn: "Runs on COS's computer", why: "opening the PR",
            disclosureTitle: "Show the details", details: "", options: [],
            status: .waitingOnYou)
    }

    /// **Ordinary spaces.** `Date.FormatStyle` puts a narrow no-break space
    /// (U+202F) in front of AM/PM, which is correct typography and makes a
    /// literal in a test file compare unequal to a string that looks identical.
    /// Compared as plain text so a failure above means what it says.
    private func plain(_ text: String) -> String {
        text.replacingOccurrences(of: "\u{202F}", with: " ")
            .replacingOccurrences(of: "\u{00A0}", with: " ")
    }

    /// What actually came back, in one readable line, so a failure above says
    /// what the list was rather than only what it was not.
    private func described(_ items: [TranscriptItem]) -> String {
        "[" + items.map(\.id).joined(separator: " | ") + "]"
    }
}

/// A calendar, a locale and a zone that do not move, so the date rows above are
/// the same sentence on every machine that runs this. The app itself uses the
/// owner's own — a date row in the wrong locale is worse than none.
private extension TimelineFormatting {
    static let fixed: TimelineFormatting = {
        let zone = TimeZone(identifier: "Europe/London")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return TimelineFormatting(
            calendar: calendar, locale: Locale(identifier: "en_US_POSIX"), timeZone: zone)
    }()
}

private extension TranscriptItem {
    var attributionLine: AttributionLine? {
        if case .attribution(let line) = self { return line }
        return nil
    }
    var rollup: TrafficRollup? {
        if case .rollup(let rollup) = self { return rollup }
        return nil
    }
    var dayBreak: DayBreak? {
        if case .dayBreak(let stamp) = self { return stamp }
        return nil
    }
    var turnFace: TurnFace? {
        if case .turnFace(let face) = self { return face }
        return nil
    }
    var entryID: String? {
        if case .entry(let entry) = self { return entry.id }
        return nil
    }
}
