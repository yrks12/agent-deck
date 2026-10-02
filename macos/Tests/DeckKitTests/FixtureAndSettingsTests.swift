import XCTest
@testable import DeckKit

/// The whole UI is built against `FixtureDeckClient` while the server contract
/// is still being written, so the fixture has to contain every shape the views
/// must handle. If it stops doing that the UI silently loses coverage.
final class FixtureTests: XCTestCase {

    func testTheFixtureRosterHasTheTwoObservedSections() async throws {
        let payload = try await FixtureDeckClient().roster()

        XCTAssertEqual(payload.sectionOrder, ["Work", "Personal"])
        XCTAssertTrue(payload.agents.contains { $0.section == "Work" })
        XCTAssertTrue(payload.agents.contains { $0.section == "Personal" })
    }

    func testTheFixtureCarriesTheObservedTitleChips() async throws {
        let titles = Set(try await FixtureDeckClient().roster().agents.map(\.title))

        for expected in ["Researcher", "Designer", "Email", "The Builder", "Travel Scout"] {
            XCTAssertTrue(titles.contains(expected), "missing title chip \(expected)")
        }
    }

    func testTheFixtureHasPinnedFavouritesAndUnreadRows() async throws {
        let payload = try await FixtureDeckClient().roster()
        let snapshot = SidebarSnapshot.build(from: payload)

        XCTAssertGreaterThanOrEqual(snapshot.pinned.count, 2, "the favourites row needs 2-3 tiles")
        XCTAssertGreaterThan(snapshot.totalUnread, 0, "unread styling needs something to style")
    }

    func testTheFixtureHasAgentToAgentPreviewLines() async throws {
        let payload = try await FixtureDeckClient().roster()

        XCTAssertTrue(
            payload.threads.contains { $0.preview.line.hasPrefix("Messaged ") },
            "the sidebar preview is often relay traffic, not the user's own words"
        )
        XCTAssertTrue(payload.threads.contains { $0.preview.line.hasPrefix("Message from ") })
    }

    func testTheFixtureHasAReadOnlyPeerThread() async throws {
        let client = FixtureDeckClient()
        let threads = try await client.roster().threads
        let peer = try XCTUnwrap(threads.first { $0.isReadOnly })

        let page = try await client.messages(threadID: peer.id, since: nil, limit: 50)

        XCTAssertTrue(page.isReadOnly)
        XCTAssertEqual(page.participants.count, 2, "a peer thread names both agents")
    }

    func testTheFixtureThreadHasALinkAPathAndAnImage() async throws {
        let client = FixtureDeckClient()
        let page = try await client.messages(threadID: "direct:hemingway", since: nil, limit: 50)
        let spans = page.messages.flatMap(\.spans)

        XCTAssertTrue(spans.contains { if case .link = $0 { return true } else { return false } })
        XCTAssertTrue(spans.contains { if case .path = $0 { return true } else { return false } })
        XCTAssertTrue(page.messages.contains { $0.attachments.contains { $0.kind == .image } })
    }

    func testTheFixtureThreadHasBothSidesOfTheConversation() async throws {
        let page = try await FixtureDeckClient()
            .messages(threadID: "direct:hemingway", since: nil, limit: 50)

        XCTAssertTrue(page.messages.contains(where: \.isFromUser), "the user's bubbles sit right")
        XCTAssertTrue(page.messages.contains { !$0.isFromUser })
    }

    /// The card cannot be reviewed offline unless the fixture carries a real
    /// one, and the shape that is easiest to get wrong is the option the deck
    /// refuses to offer.
    func testTheFixtureHasApprovalsIncludingOneThatMustAskEveryTime() async throws {
        let page = try await FixtureDeckClient().approvals()

        XCTAssertEqual(page.unreadable, 0, "the fixture must not ship a payload the client drops")
        XCTAssertGreaterThanOrEqual(page.approvals.count, 2)
        let options = page.approvals.flatMap(\.options)
        XCTAssertTrue(options.allSatisfy { !$0.ruleText.isEmpty }, "every button states its rule")
        XCTAssertTrue(
            options.contains { $0.reply == .always && !$0.isAvailable },
            "credentials, payments and irreversible calls ask every time — the disabled option needs a fixture"
        )
        XCTAssertTrue(options.contains { $0.reply == .always && $0.isAvailable })
    }

    func testAnsweringAFixtureApprovalKeepsItAnswered() async throws {
        let client = FixtureDeckClient()
        let waiting = try await client.approvals().approvals
        let first = try XCTUnwrap(waiting.first)
        let once = try XCTUnwrap(first.options.first { $0.reply == .once })

        try await client.decideApproval(id: first.id, option: once)

        let left = try await client.approvals().approvals.map(\.id)
        XCTAssertFalse(left.contains(first.id))
    }

    func testTheFixtureRoutinesCoverTheSchedulesThePanelPhrases() async throws {
        let routines = try await FixtureDeckClient().routines()

        let schedules = Set(routines.map(\.scheduleText))
        XCTAssertTrue(schedules.contains("Weekdays at 8:00 AM"), "\(schedules)")
        XCTAssertTrue(schedules.contains("Every 3 hours on weekdays"), "\(schedules)")
        XCTAssertTrue(routines.contains { !$0.isEnabled }, "the paused row needs a fixture too")
    }
}

/// The per-agent settings panel: name, title, description, notifications.
final class AgentSettingsTests: XCTestCase {

    func testTogglingNotificationsSendsExactlyOneUpdateAndKeepsTheServersAnswer() async throws {
        let client = ScriptedDeckClient()
        var chief = makeAgent("Chief", title: "The Builder")
        chief.notificationsEnabled = true
        client.rosterPayload = RosterPayload(agents: [chief], threads: [], sectionOrder: ["Work"])
        let panel = AgentSettingsModel(agent: chief, client: client)

        await panel.setNotifications(false)

        let updated = await panel.agent
        XCTAssertEqual(client.calls, [.updateAgent("Chief")])
        XCTAssertFalse(updated.notificationsEnabled)
    }

    func testEditingTheProfileFieldsSendsOneUpdateCarryingAllThree() async throws {
        let client = ScriptedDeckClient()
        let panel = AgentSettingsModel(agent: makeAgent("Chief"), client: client)

        await panel.save(name: "Chief", title: "The Builder", detail: "Runs the board.")

        XCTAssertEqual(client.calls, [.updateAgent("Chief")])
        let saved = await panel.agent
        XCTAssertEqual(saved.title, "The Builder")
        XCTAssertEqual(saved.detail, "Runs the board.")
    }

    func testTheNotificationCopyIsTheObservedOne() {
        XCTAssertEqual(
            AgentSettingsModel.notificationsCaption,
            "Get notified when this Bot finishes or needs input"
        )
    }

    /// **The sentence under the switch has to follow the product, not a
    /// literal.**
    ///
    /// It said, hard-coded: *"Saved on the deck, but nothing delivers
    /// notifications yet — there is no push service."* That was true when it
    /// was written and it is being made false right now by the per-desk phone
    /// pushes another branch is wiring. A hard-coded reassurance is a lie the
    /// day the product moves under it, and this one is about **whether he will
    /// be told his agent is stuck** — the exact thing he is complaining about.
    ///
    /// So both halves are asserted against one source of truth: with nothing
    /// delivering it says so plainly, and with a channel that really delivers
    /// it names the channel and stops claiming there is none.
    func testWithNothingDeliveringTheSwitchSaysSoPlainly() {
        let nothing = NotificationDelivery(channels: [])

        XCTAssertFalse(nothing.deliversAnything)
        XCTAssertEqual(
            nothing.caveat,
            "Saved on the deck, but nothing delivers notifications yet — there is no push service.",
            "the honest sentence for today's product is the one that was on screen")
    }

    func testWhenSomethingReallyDeliversTheSwitchStopsSayingNothingDoes() {
        let phone = NotificationDelivery(channels: ["your phone"])

        XCTAssertTrue(phone.deliversAnything)
        XCTAssertTrue(
            phone.caveat.contains("your phone"),
            "delivery exists and the panel does not name where it goes: \(phone.caveat)")
        XCTAssertFalse(
            phone.caveat.contains("nothing delivers"),
            "the panel is still telling him nothing will reach him while a push "
            + "service is delivering: \(phone.caveat)")
    }

    /// **The sentence the app puts on screen today, against what the product
    /// really does.**
    ///
    /// `ours-01.jpg` shows, under this switch: *"Saved on the deck, but nothing
    /// delivers notifications yet — there is no push service."* That is now a
    /// lie. The deck pushes per desk over WhatsApp on start, milestone, blocked
    /// and finished; the volume is capped; and this switch is the mute that
    /// silences one. Telling him nothing will reach him, under a switch about
    /// being told his agent is stuck, is the app being wrong about the one
    /// question he asked.
    func testTheSentenceOnScreenTodaySaysHisPhoneIsToldRatherThanThatNothingIs() {
        let onScreen = NotificationDelivery.current

        XCTAssertTrue(
            onScreen.deliversAnything,
            "per-desk WhatsApp pushes have shipped and the app still models "
            + "this build as delivering nothing")
        XCTAssertFalse(
            onScreen.caveat.contains("nothing delivers"),
            "the panel is telling him nothing will reach him while his phone is "
            + "being buzzed on start, milestone, blocked and finished: \(onScreen.caveat)")
        XCTAssertTrue(
            onScreen.caveat.contains("phone"),
            "it has to say where it goes, or it is reassurance with no object: "
            + "\(onScreen.caveat)")
        XCTAssertTrue(
            onScreen.caveat.contains("blocked"),
            "blocked is the moment he is complaining about — a desk stopped and "
            + "no sign of it. The line has to name it: \(onScreen.caveat)")
    }

    /// And the panel reads that, rather than carrying its own copy of the
    /// sentence — one source of truth, or the two drift apart silently.
    func testTheInspectorTakesThatSentenceFromTheOneSourceRatherThanASpelledOutOne() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let panel = try String(
            contentsOf: root.appendingPathComponent("Sources/DeckUI/SettingsPanelView.swift"),
            encoding: .utf8)

        XCTAssertTrue(
            panel.contains("delivery.caveat"),
            "the inspector does not draw the sentence the one source derives, so "
            + "whatever it does draw cannot follow the product")
        XCTAssertFalse(
            panel.contains("notificationsCaveat"),
            "the inspector is still drawing the frozen literal, which said "
            + "nothing delivers notifications on the day push started delivering "
            + "them")
        XCTAssertFalse(
            panel.contains("there is no push service"),
            "the inspector spells the no-delivery sentence out itself, so wiring "
            + "push leaves a lie on screen")
    }
}
