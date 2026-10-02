import XCTest
@testable import DeckKit

/// **What a row has to say without being opened.**
///
/// Measured against xAI's Grok Bot beside this app
/// (`.grok-reference/grok-04-transcript-full.jpg`). Theirs: every row carries a
/// coloured dot on its face, green while that desk works and orange while it
/// waits on him; a stuck desk reads "Waiting for you: …" in orange rather than
/// in the same grey as everything else; every row has a stamp on the right; and
/// the one desk he talks to sits in its own block above the roster instead of
/// being mixed into it.
///
/// Ours said none of that. He manages by exception and the exception looked
/// identical to the idle.
///
/// Every claim here is on the **model**, because the model is what decides. The
/// view's job is to pick a colour for `RowAttention` and draw it, and
/// `testEveryAttentionACanReachHasAColourAndAWord` is what stops a new case
/// being drawn as nothing at all.
final class SidebarRowSpeaksTests: XCTestCase {

    private func agent(
        _ name: String,
        state: AgentState = .idle,
        blocked: Blocked? = nil,
        boss: String? = "atlas",
        reports: [String] = []
    ) -> Agent {
        var made = makeAgent(name)
        made.state = state
        made.blocked = blocked
        made.boss = boss
        made.reports = reports
        return made
    }

    private func snapshot(
        agents: [Agent],
        threads: [ThreadSummary] = [],
        now: Date = Date(timeIntervalSince1970: 1_700_000_000)
    ) -> SidebarSnapshot {
        SidebarSnapshot.build(
            from: RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work"]),
            now: now)
    }

    private func row(_ snapshot: SidebarSnapshot, _ name: String) -> SidebarRow? {
        snapshot.sections.flatMap(\.rows).first { $0.id == name }
    }

    // MARK: 1 — status on the face

    /// A working roster and a dead one looked the same. They cannot.
    func testAWorkingDeskAndAStuckDeskAreDistinguishableOnTheRowItself() {
        let made = snapshot(agents: [
            agent("busy", state: .working),
            agent("stuck", state: .needsYou),
            agent("quiet", state: .idle),
        ])

        XCTAssertEqual(row(made, "busy")?.attention, .working)
        XCTAssertEqual(row(made, "stuck")?.attention, .waitingForYou)
        XCTAssertEqual(
            row(made, "quiet")?.attention, RowAttention.quiet,
            "an idle desk must draw no dot at all — a roster where every face is "
            + "marked is a roster where no face is")
    }

    /// **Sweeping the class, not the two cases the screenshot happened to
    /// show.** Every state the wire can carry has to land somewhere deliberate,
    /// and a state added later must fail here rather than silently drawing grey.
    func testEveryAgentStateLandsOnADeliberateAttention() {
        var undecided: [String] = []
        for state in AgentState.allCases {
            let made = snapshot(agents: [agent("desk", state: state)])
            guard let attention = row(made, "desk")?.attention else {
                undecided.append("\(state.rawValue) produced no row")
                continue
            }
            let expected: RowAttention
            switch state {
            case .working: expected = .working
            case .needsYou: expected = .waitingForYou
            // DONE, IDLE, SHELL, DEAD and OFFLINE are all "nothing is moving
            // and nothing is stuck". The unread badge already says whether
            // there is something to read; the avatar dims for OFFLINE. An
            // alarm on all five is an alarm on none.
            // ASLEEP (K3) rests until a message wakes it: quiet, not an alarm.
            case .done, .idle, .shell, .dead, .offline, .asleep: expected = .quiet
            }
            if attention != expected {
                undecided.append("\(state.rawValue) drew \(attention) not \(expected)")
            }
        }
        XCTAssertEqual(undecided, [], undecided.joined(separator: "; "))
    }

    /// `blocked` is the one the contract is loudest about: it arrives on a
    /// **seated** desk too (`dialog_unrelayed`), so gating the alarm on `state`
    /// drops the exact stall he most needs to see. Any state, blocked, is
    /// orange.
    func testABlockedDeskIsWaitingForHimWhateverStateItIsIn() {
        var missed: [String] = []
        let stuck = Blocked(what: "Sign in to Google", reason: "dialog_unrelayed")
        for state in AgentState.allCases {
            let made = snapshot(agents: [agent("desk", state: state, blocked: stuck)])
            if row(made, "desk")?.attention != .waitingForYou {
                missed.append(state.rawValue)
            }
        }
        XCTAssertEqual(
            missed, [],
            "a blocked desk drew no alarm in these states: \(missed.joined(separator: ", "))")
    }

    // MARK: 2 — "waiting for you" says so, in words

    func testAStuckDeskSaysWhatItIsWaitingForRatherThanItsLastMessage() {
        let made = snapshot(
            agents: [agent("northwind", blocked: Blocked(what: "Sign in to Google", reason: "lock_busy"))],
            threads: [makeThread("direct:northwind", agent: "northwind",
                                 preview: ThreadPreview(text: "some old chatter"))])

        XCTAssertEqual(row(made, "northwind")?.previewText, "Waiting for you: Sign in to Google")
    }

    /// The deck's `what` is optional on the wire. A stuck desk with nothing
    /// said about it must still read as stuck, never fall back to its last
    /// message — which is the sentence that made this look idle.
    func testAStuckDeskWithNothingSaidAboutItStillReadsAsStuck() {
        let made = snapshot(
            agents: [agent("northwind", blocked: Blocked(what: "", reason: "who_knows"))],
            threads: [makeThread("direct:northwind", agent: "northwind",
                                 preview: ThreadPreview(text: "some old chatter"))])

        let text = row(made, "northwind")?.previewText ?? ""
        XCTAssertTrue(
            text.hasPrefix("Waiting for you: "),
            "a stuck desk read \u{201c}\(text)\u{201d} — its last message, in the same "
            + "grey as every desk that is fine")
        XCTAssertFalse(text.contains("some old chatter"))
    }

    /// The good signal: a desk that is **not** stuck still shows its
    /// conversation. A row that says "waiting for you" about everything is the
    /// same failure in the other direction.
    func testADeskThatIsFineStillShowsItsLastMessage() {
        let made = snapshot(
            agents: [agent("northwind")],
            threads: [makeThread("direct:northwind", agent: "northwind",
                                 preview: ThreadPreview(text: "shipped the thing"))])

        XCTAssertEqual(row(made, "northwind")?.previewText, "shipped the thing")
        XCTAssertEqual(row(made, "northwind")?.attention, RowAttention.quiet)
    }

    // MARK: 3 — a stamp on every row

    /// Ours showed a stamp on the rows with a thread and nothing on the rest,
    /// so the right-hand column was ragged and a desk with no traffic looked
    /// like a rendering failure.
    func testEveryRowCarriesAStampWhateverItsTrafficLooksLike() {
        // Real "now", because the wording turns on Calendar.isDateInToday and
        // a fixed epoch in 2023 is never today.
        let now = Date()
        var quiet = agent("never-spoke")
        quiet.lastActivityAt = nil
        var seenOnce = agent("spoke-once")
        seenOnce.lastActivityAt = now.addingTimeInterval(-1_000)

        let made = snapshot(
            agents: [agent("chatty"), quiet, seenOnce],
            threads: [makeThread("direct:chatty", agent: "chatty", at: -60)],
            now: now)

        var blank: [String] = []
        for row in made.sections.flatMap(\.rows) where (row.timestampLabel ?? "").isEmpty {
            blank.append(row.id)
        }
        XCTAssertEqual(
            blank, [],
            "these rows had no stamp at all: \(blank.joined(separator: ", "))")

        XCTAssertEqual(
            row(made, "spoke-once")?.timestampLabel, "16m",
            "a desk with no thread but a last-seen time on the roster must use it")

        // **Minutes are minutes, whichever side of midnight they fell on.**
        // FOUND by this test at 00:15 on 2026-09-07: `spoke-once` last spoke
        // 1,000 seconds earlier — 23:58 the previous evening — and the row
        // read "Yesterday". Sixteen minutes is not yesterday to a man deciding
        // whether a desk has stalled; "Yesterday" is what he would read as a
        // dead desk, and it is what every desk on his board says for the first
        // hour of every day. The rollover only exposed it; the rule was always
        // wrong, because it asked which CALENDAR DAY a moment fell in before
        // asking how long ago it was.
        let justBeforeMidnight = Calendar.current.date(
            from: DateComponents(year: 2026, month: 9, day: 6,
                                 hour: 23, minute: 58))!
        let justAfterMidnight = Calendar.current.date(
            from: DateComponents(year: 2026, month: 9, day: 7,
                                 hour: 0, minute: 14))!
        XCTAssertEqual(
            SidebarTimestamp.short(justBeforeMidnight, now: justAfterMidnight), "16m",
            "a desk that spoke sixteen minutes ago reads as yesterday's news "
            + "because the clock crossed midnight in between")
        // The paired positive, so the fix cannot be "never say Yesterday".
        let realYesterday = Calendar.current.date(
            from: DateComponents(year: 2026, month: 9, day: 6, hour: 9))!
        XCTAssertEqual(
            SidebarTimestamp.short(realYesterday, now: justAfterMidnight), "Yesterday",
            "a desk that genuinely last spoke yesterday morning must still say so")
        XCTAssertEqual(
            row(made, "never-spoke")?.timestampLabel, SidebarTimestamp.never,
            "a desk that has genuinely never been heard from needs a mark, not a gap")
    }

    // MARK: 4 — the chief of staff, above the roster

    /// The deck knows: the chief is the desk that reports to nobody but him.
    func testTheDeskEveryoneReportsThroughIsPinnedAboveTheRoster() {
        let made = snapshot(agents: [
            agent("atlas", boss: nil, reports: ["northwind", "venture"]),
            agent("northwind"),
            agent("venture"),
        ])

        XCTAssertEqual(made.chief?.id, "atlas")
        XCTAssertNil(
            row(made, "atlas"),
            "the chief was pinned above the roster and left in the list as well — "
            + "he would see it twice")
        XCTAssertEqual(
            made.sections.flatMap(\.rows).map(\.id).sorted(), ["northwind", "venture"])
    }

    /// A desk whose boss is the owner himself is the same relationship said the
    /// other way round, and the deck writes it both ways.
    func testADeskReportingStraightToHimCountsAsTheChief() {
        let made = snapshot(agents: [
            agent("atlas", boss: DeckOwner.name, reports: ["northwind"]),
            agent("northwind"),
        ])
        XCTAssertEqual(made.chief?.id, "atlas")
    }

    /// **Do not guess.** Two desks reporting to nobody is not a chief of staff,
    /// it is an org chart this client cannot read, and picking one would put a
    /// stranger in the seat he talks to.
    func testAnAmbiguousOrgChartPinsNobodyAndLeavesEveryDeskInTheList() {
        let made = snapshot(agents: [
            agent("atlas", boss: nil, reports: ["northwind"]),
            agent("orion", boss: nil, reports: ["venture"]),
            agent("northwind"),
            agent("venture", boss: "orion"),
        ])

        XCTAssertNil(
            made.chief,
            "two desks report to nobody and both have desks under them, so there is "
            + "no chief to pin — this client must not choose one")
        XCTAssertEqual(
            made.sections.flatMap(\.rows).count, 4,
            "nothing was pinned, so nothing may have been removed from the list")
    }

    /// One report-to-nobody desk beside one that merely has no boss recorded is
    /// still readable: the one with people under it is the chief.
    func testTheDeskWithPeopleUnderItWinsATie() {
        let made = snapshot(agents: [
            agent("atlas", boss: nil, reports: ["northwind"]),
            agent("stray", boss: nil),
            agent("northwind"),
        ])
        XCTAssertEqual(made.chief?.id, "atlas")
    }

    /// Pinning the chief took it out of `sections`, which is what everything
    /// that *looks a desk up* reads. Two things do: search, and the list's
    /// selection binding. Making the one desk he talks to the one desk he
    /// cannot find would be a worse bug than the one this slice set out to fix.
    func testTheChiefIsStillSearchableAndStillSelectableOnceItIsPinned() {
        let payload = RosterPayload(
            agents: [
                agent("atlas", boss: nil, reports: ["northwind"]),
                agent("northwind"),
            ],
            threads: [], sectionOrder: ["Work"])

        let found = SidebarSearch.result(payload: payload, query: "atlas")
        XCTAssertEqual(found.matchCount, 1, "searching for the chief by name found nothing")
        XCTAssertEqual(found.snapshot.chief?.id, "atlas")

        let all = SidebarSnapshot.build(from: payload).allRows.map(\.id)
        XCTAssertEqual(
            all.sorted(), ["atlas", "northwind"],
            "the selection binding reads allRows — the chief must be in it or "
            + "clicking it selects nothing")
    }

    /// The other direction: a search that does not name the chief must not
    /// leave it on screen as if it had matched.
    func testASearchThatDoesNotNameTheChiefDropsItLikeAnyOtherDesk() {
        let payload = RosterPayload(
            agents: [
                agent("atlas", boss: nil, reports: ["northwind"]),
                agent("northwind"),
            ],
            threads: [], sectionOrder: ["Work"])

        let found = SidebarSearch.result(payload: payload, query: "northwind")
        XCTAssertNil(found.snapshot.chief)
        XCTAssertEqual(found.matchCount, 1)
    }

    // MARK: 5 — a thread with several desks in it shows several faces

    func testARowForAThreadWithSeveralDesksShowsTheirFaces() {
        let made = snapshot(
            agents: [agent("venture")],
            threads: [makeThread(
                "peer:venture|craft", agent: "venture",
                participants: ["venture", "craft", "product", "growth"])])

        let row = row(made, "venture")
        XCTAssertEqual(
            row?.participantLooks.map(\.initials), ["V", "C"],
            "a crowded thread must show more than one face")
        XCTAssertEqual(row?.extraParticipantCount, 2, "the rest are counted, not dropped")
    }

    /// The good signal, and the common case: a 1:1 with him is not a crowd and
    /// must keep drawing the one face it always drew.
    func testAnOrdinaryDirectThreadStillDrawsOneFace() {
        let made = snapshot(
            agents: [agent("northwind")],
            threads: [makeThread("direct:northwind", agent: "northwind",
                                 participants: [DeckOwner.name, "northwind"])])

        XCTAssertEqual(
            row(made, "northwind")?.participantLooks, [],
            "a 1:1 was drawn as a group — he is not one of the bots")
        XCTAssertEqual(row(made, "northwind")?.extraParticipantCount, 0)
    }
}
