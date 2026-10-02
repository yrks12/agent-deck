import XCTest
@testable import DeckKit

/// Failure mode being pinned: the sidebar asks the server one question per
/// agent to work out a timestamp, an unread count or a preview line. With a
/// roster of a few hundred desks that is a stalled window.
///
/// The rule: one roster call, one pass over the payload, everything derived.
final class SidebarTests: XCTestCase {

    private func largeRoster(_ count: Int) -> RosterPayload {
        var agents: [Agent] = []
        var threads: [ThreadSummary] = []
        for index in 0..<count {
            let name = "Agent\(index)"
            agents.append(makeAgent(name, section: index.isMultiple(of: 2) ? "Work" : "Personal"))
            threads.append(
                makeThread(
                    "t\(index)",
                    agent: name,
                    unread: index.isMultiple(of: 3) ? 2 : 0,
                    at: Double(index),
                    preview: ThreadPreview(text: "line \(index)")
                )
            )
        }
        return RosterPayload(agents: agents, threads: threads, sectionOrder: ["Work", "Personal"])
    }

    func testBuildingTheSidebarForALargeRosterIssuesExactlyOneRequest() async throws {
        let client = ScriptedDeckClient()
        client.rosterPayload = largeRoster(500)
        let model = RosterModel(client: client)

        try await model.load()

        let snapshot = await model.snapshot
        XCTAssertEqual(client.calls, [.roster], "one roster call serves the whole sidebar")
        XCTAssertEqual(snapshot.sections.flatMap(\.rows).count, 500)
    }

    func testTheSnapshotIsBuiltFromThePayloadAloneWithNoClientInReach() {
        // `build` takes no client. If a per-agent request were needed it could
        // not be made from here, which is the point.
        let snapshot = SidebarSnapshot.build(from: largeRoster(2_000))

        XCTAssertEqual(snapshot.sections.flatMap(\.rows).count, 2_000)
        XCTAssertEqual(snapshot.totalUnread, 1_334, "667 agents carry 2 unread apiece")
    }

    func testRowsAreOrderedMostRecentFirstWithinASection() {
        let payload = RosterPayload(
            agents: [makeAgent("Chief"), makeAgent("Seeker"), makeAgent("Hemingway")],
            threads: [
                makeThread("t1", agent: "Chief", at: 10),
                makeThread("t2", agent: "Seeker", at: 90),
                makeThread("t3", agent: "Hemingway", at: 50),
            ],
            sectionOrder: ["Work"]
        )

        let snapshot = SidebarSnapshot.build(from: payload)

        XCTAssertEqual(snapshot.sections.map(\.name), ["Work"])
        XCTAssertEqual(snapshot.sections[0].rows.map(\.agent.name), ["Seeker", "Hemingway", "Chief"])
    }

    func testSectionsAppearInTheOrderTheRosterDeclares() {
        let payload = RosterPayload(
            agents: [makeAgent("Chief", section: "Work"), makeAgent("Travel Scout", section: "Personal")],
            threads: [makeThread("t1", agent: "Chief"), makeThread("t2", agent: "Travel Scout")],
            sectionOrder: ["Work", "Personal"]
        )

        XCTAssertEqual(SidebarSnapshot.build(from: payload).sections.map(\.name), ["Work", "Personal"])
    }

    func testEachRowCarriesItsUnreadCountAndTimestampFromTheOnePass() {
        let payload = RosterPayload(
            agents: [makeAgent("Chief")],
            threads: [makeThread("t1", agent: "Chief", unread: 4, at: 77)],
            sectionOrder: ["Work"]
        )

        let row = SidebarSnapshot.build(from: payload).sections[0].rows[0]

        XCTAssertEqual(row.unreadCount, 4)
        XCTAssertTrue(row.isUnread)
        XCTAssertEqual(row.threadID, "t1")
        XCTAssertEqual(row.timestamp, Date(timeIntervalSince1970: 1_700_000_000 + 77))
    }

    func testAgentToAgentTrafficIsWhatThePreviewLineShows() {
        let payload = RosterPayload(
            agents: [makeAgent("Hemingway"), makeAgent("Seeker")],
            threads: [
                makeThread("t1", agent: "Hemingway", at: 20,
                           preview: ThreadPreview(text: "draft is ready", relay: .messaged("Chief"))),
                makeThread("t2", agent: "Seeker", at: 10,
                           preview: ThreadPreview(text: "found three flights", relay: .received("Seeker"))),
            ],
            sectionOrder: ["Work"]
        )

        let rows = SidebarSnapshot.build(from: payload).sections[0].rows

        XCTAssertEqual(rows[0].preview.line, "Messaged Chief: draft is ready")
        XCTAssertEqual(rows[1].preview.line, "Message from Seeker: found three flights")
    }

    func testAPlainPreviewHasNoRelayPrefix() {
        XCTAssertEqual(ThreadPreview(text: "on it").line, "on it")
    }

    func testUnreadsBelowTheFoldRollUpIntoTheMoreUnreadsPill() {
        let payload = RosterPayload(
            agents: (0..<6).map { makeAgent("A\($0)") },
            threads: (0..<6).map { makeThread("t\($0)", agent: "A\($0)", unread: 3, at: Double(10 - $0)) },
            sectionOrder: ["Work"]
        )

        let section = SidebarSnapshot.build(from: payload, visibleRowsPerSection: 4).sections[0]

        XCTAssertEqual(section.rows.count, 4, "only the visible rows are handed to the list")
        XCTAssertEqual(section.overflowUnreadCount, 6, "two hidden rows, 3 unread each")
        XCTAssertEqual(section.overflowLabel, "+ 6 more unreads")
    }

    func testPinnedAgentsAreLiftedIntoTheFavouritesRow() {
        var chief = makeAgent("Chief")
        chief.isPinned = true
        var scout = makeAgent("Travel Scout", section: "Personal")
        scout.isPinned = true
        let payload = RosterPayload(
            agents: [chief, makeAgent("Hemingway"), scout],
            threads: [
                makeThread("t1", agent: "Chief", at: 5),
                makeThread("t2", agent: "Hemingway", at: 9),
                makeThread("t3", agent: "Travel Scout", at: 1),
            ],
            sectionOrder: ["Work", "Personal"]
        )

        let snapshot = SidebarSnapshot.build(from: payload)

        XCTAssertEqual(snapshot.pinned.map(\.agent.name), ["Chief", "Travel Scout"])
        XCTAssertEqual(
            snapshot.sections.flatMap(\.rows).map(\.agent.name).sorted(),
            ["Chief", "Hemingway", "Travel Scout"],
            "pinning promotes an agent, it does not remove it from its section"
        )
    }

    func testAnAgentWithNoThreadStillGetsARowSoTheRosterIsComplete() {
        let payload = RosterPayload(
            agents: [makeAgent("Chief"), makeAgent("Newcomer")],
            threads: [makeThread("t1", agent: "Chief", at: 5)],
            sectionOrder: ["Work"]
        )

        let names = SidebarSnapshot.build(from: payload).sections[0].rows.map(\.agent.name)

        XCTAssertEqual(names, ["Chief", "Newcomer"], "a desk with no traffic sorts last, it does not vanish")
    }
}
