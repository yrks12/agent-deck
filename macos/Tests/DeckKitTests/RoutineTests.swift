import XCTest
@testable import DeckKit

/// Routines in the inspector.
///
/// The rule that matters on the wire: **the client sends a cron spec and a
/// timezone, never a next-run time.** The deck owns the cron maths — it is the
/// thing that has to still be right after a restart, a DST change or a laptop
/// that was asleep — and a client that posted its own `next_run_at` would be
/// quietly overriding it.
final class RoutineTests: XCTestCase {

    private func decodeRoutines(_ json: String) throws -> [Routine] {
        try DeckCoding.decoder.decode(RoutinesResponse.self, from: Data(json.utf8)).routines
    }

    // MARK: schedules a person can read

    func testWeekdayMornings() {
        XCTAssertEqual(CronSchedule.describe("0 8 * * 1-5"), "Weekdays at 8:00 AM")
    }

    func testEveryFewHoursOnWeekdays() {
        XCTAssertEqual(CronSchedule.describe("0 */3 * * 1-5"), "Every 3 hours on weekdays")
    }

    func testEveryDayAtATime() {
        XCTAssertEqual(CronSchedule.describe("30 9 * * *"), "Every day at 9:30 AM")
        XCTAssertEqual(CronSchedule.describe("0 18 * * *"), "Every day at 6:00 PM")
        XCTAssertEqual(CronSchedule.describe("0 0 * * *"), "Every day at 12:00 AM")
    }

    func testWeekendsAndSingleDays() {
        XCTAssertEqual(CronSchedule.describe("0 10 * * 0,6"), "Weekends at 10:00 AM")
        XCTAssertEqual(CronSchedule.describe("0 9 * * 1"), "Mondays at 9:00 AM")
        XCTAssertEqual(CronSchedule.describe("15 17 * * 5"), "Fridays at 5:15 PM")
    }

    func testEveryFewMinutesAndHours() {
        XCTAssertEqual(CronSchedule.describe("*/15 * * * *"), "Every 15 minutes")
        XCTAssertEqual(CronSchedule.describe("0 */6 * * *"), "Every 6 hours")
    }

    /// A schedule this client cannot phrase is shown as itself. Guessing at
    /// prose for "the 1st of every month at midnight" and getting it wrong is
    /// worse than showing the spec.
    func testASpecItCannotPhraseIsShownVerbatimRatherThanGuessedAt() {
        XCTAssertEqual(CronSchedule.describe("0 0 1 * *"), "Custom schedule (0 0 1 * *)")
        XCTAssertEqual(CronSchedule.describe("nonsense"), "Custom schedule (nonsense)")
    }

    // MARK: the create body

    func testCreatingARoutineSendsSpecAndTimezoneAndNeverANextRunTime() throws {
        let draft = RoutineDraft(
            agentName: "chief",
            prompt: "Summarise what moved yesterday.",
            spec: "0 8 * * 1-5",
            timezone: "Europe/London"
        )

        let body = try draft.encodedBody()
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])

        XCTAssertEqual(json.keys.sorted(), ["agent", "enabled", "prompt", "trigger"])
        let trigger = try XCTUnwrap(json["trigger"] as? [String: Any])
        // §13: a trigger is {kind, spec, tz}. Any other kind is stored and
        // never scheduled, so the kind has to be stated rather than implied.
        XCTAssertEqual(trigger.keys.sorted(), ["kind", "spec", "tz"])
        XCTAssertEqual(trigger["kind"] as? String, "cron")
        XCTAssertEqual(trigger["spec"] as? String, "0 8 * * 1-5")
        XCTAssertEqual(trigger["tz"] as? String, "Europe/London")
        XCTAssertEqual(json["agent"] as? String, "chief")
        XCTAssertEqual(json["prompt"] as? String, "Summarise what moved yesterday.")
        XCTAssertFalse(
            String(decoding: body, as: UTF8.self).contains("next_run_at"),
            "the deck computes when this fires; the client must not send one"
        )
    }

    func testADraftDefaultsToThisMacsTimezoneRatherThanAHardCodedOne() {
        let draft = RoutineDraft(agentName: "chief", prompt: "Check the queue.", spec: "0 8 * * *")

        XCTAssertEqual(draft.timezone, TimeZone.current.identifier)
    }

    func testADraftWithNothingToSayOrNoScheduleIsRefusedBeforeTheNetwork() {
        var draft = RoutineDraft(agentName: "chief", prompt: "  ", spec: "0 8 * * *")
        XCTAssertNotNil(draft.problem)

        draft = RoutineDraft(agentName: "chief", prompt: "Do the thing.", spec: "0 8 *")
        XCTAssertEqual(
            draft.problem,
            "A schedule is five cron fields: minute, hour, day of month, month, day of week."
        )

        draft = RoutineDraft(agentName: "chief", prompt: "Do the thing.", spec: "0 8 * * 1-5")
        XCTAssertNil(draft.problem)
    }

    // MARK: what comes back

    func testARoutineReadsItsScheduleAndTheDecksNextRunTime() throws {
        let routines = try decodeRoutines("""
        {"routines":[{"id":"r1","agent":"chief","prompt":"Morning sweep",
          "trigger":{"spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":true}]}
        """)

        let routine = try XCTUnwrap(routines.first)
        XCTAssertEqual(routine.scheduleText, "Weekdays at 8:00 AM")
        XCTAssertEqual(routine.nextRunAt, Date(timeIntervalSince1970: 1_756_100_000))
        XCTAssertTrue(routine.isEnabled)
        XCTAssertTrue(routine.nextRunText.hasPrefix("Next run "), routine.nextRunText)
    }

    func testADisabledRoutineSaysPausedRatherThanShowingAStaleNextRun() throws {
        let routines = try decodeRoutines("""
        {"routines":[{"id":"r1","agent":"chief","prompt":"Morning sweep",
          "trigger":{"spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":false}]}
        """)

        XCTAssertEqual(try XCTUnwrap(routines.first).nextRunText, "Paused")
    }

    func testAnEnabledRoutineWithNoNextRunSaysSoInsteadOfLookingScheduled() throws {
        let routines = try decodeRoutines("""
        {"routines":[{"id":"r1","agent":"chief","prompt":"One-off",
          "trigger":{"spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":null,"enabled":true}]}
        """)

        XCTAssertEqual(
            try XCTUnwrap(routines.first).nextRunText,
            "The deck has not worked out when this runs next"
        )
    }

    /// §13 sends the last three runs so the panel can answer "is this thing
    /// working". A routine that has been failing every morning must not look
    /// identical to one that has been working.
    func testARoutineSaysHowItsLastRunWent() throws {
        let failing = try decodeRoutines("""
        {"routines":[{"id":"r1","agent":"chief","prompt":"Morning sweep",
          "trigger":{"kind":"cron","spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":true,
          "runs":[{"ts":1756000000.0,"ok":false,"detail":"no session"},
                  {"ts":1755900000.0,"ok":true,"detail":"delivered"}]}]}
        """)
        let working = try decodeRoutines("""
        {"routines":[{"id":"r2","agent":"chief","prompt":"Morning sweep",
          "trigger":{"kind":"cron","spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":true,
          "runs":[{"ts":1756000000.0,"ok":true,"detail":"queued"}]}]}
        """)
        let never = try decodeRoutines("""
        {"routines":[{"id":"r3","agent":"chief","prompt":"Morning sweep",
          "trigger":{"kind":"cron","spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":true,"runs":[]}]}
        """)

        XCTAssertEqual(try XCTUnwrap(failing.first).lastRunText, "Last run failed — no session")
        // "queued" is waiting for the agent's next turn, not a failure.
        XCTAssertEqual(try XCTUnwrap(working.first).lastRunText, "Last run queued")
        XCTAssertEqual(try XCTUnwrap(never.first).lastRunText, "Has not run yet")
    }

    // MARK: the routes

    func testTheRoutineRoutesAreTheFourTheInspectorNeeds() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/routines"] = Data(#"{"routines":[]}"#.utf8)
        performer.bodies["/v1/routines/r1"] = Data("""
        {"routine":{"id":"r1","agent":"chief","prompt":"p",
          "trigger":{"spec":"0 8 * * *","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":false}}
        """.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)

        _ = try await client.routines()
        let paused = try await client.setRoutine(id: "r1", enabled: false)
        try await client.deleteRoutine(id: "r1")

        XCTAssertEqual(performer.paths, ["/v1/routines", "/v1/routines/r1", "/v1/routines/r1"])
        XCTAssertEqual(performer.requests.map(\.httpMethod), ["GET", "PATCH", "DELETE"])
        XCTAssertFalse(paused.isEnabled, "the server's answer wins, not the tap")

        let patch = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests[1].httpBody))
                as? [String: Any]
        )
        XCTAssertEqual(patch.keys.sorted(), ["enabled"], "a pause is not a rewrite of the schedule")
        XCTAssertEqual(patch["enabled"] as? Bool, false)
    }

    // MARK: the panel's four states

    func testADeckWithNoRoutinesSaysSoRatherThanShowingAnEmptyBox() async {
        let client = ScriptedDeckClient()
        let model = RoutinesModel(client: client)

        await model.load()

        let empty = await model.emptyMessage
        XCTAssertEqual(
            empty,
            "No routines yet. A routine sends this agent the same prompt on a schedule."
        )
    }

    func testAFailedLoadIsQuotedRatherThanLookingLikeNoRoutines() async {
        let client = ScriptedDeckClient()
        client.routinesError = .transport("no route to host")
        let model = RoutinesModel(client: client)

        await model.load()

        let problem = await model.problem
        let empty = await model.emptyMessage
        XCTAssertEqual(problem, DeckError.transport("no route to host").userFacingText)
        XCTAssertNil(empty, "a failure is not an empty list")
    }

    func testTheModelKeepsWhatTheDeckAnsweredAfterACreate() async throws {
        let client = ScriptedDeckClient()
        client.routineToReturn = try XCTUnwrap(decodeRoutines("""
        {"routines":[{"id":"r9","agent":"chief","prompt":"Morning sweep",
          "trigger":{"spec":"0 8 * * 1-5","tz":"Europe/London"},
          "next_run_at":1756100000.0,"enabled":true}]}
        """).first)
        let model = RoutinesModel(client: client)

        let created = await model.create(
            RoutineDraft(agentName: "chief", prompt: "Morning sweep", spec: "0 8 * * 1-5")
        )

        XCTAssertTrue(created)
        let listed = await model.routines(forAgent: "chief")
        XCTAssertEqual(listed.map(\.id), ["r9"])
        XCTAssertEqual(listed.first?.nextRunAt, Date(timeIntervalSince1970: 1_756_100_000),
                       "the deck's next_run_at is the one shown")
    }
}
