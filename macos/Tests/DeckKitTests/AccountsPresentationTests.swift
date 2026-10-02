import XCTest
@testable import DeckKit

/// **Two Claude accounts on screen (S12b), as decisions, without a window.**
///
/// What the Mac sidebar and the iPhone roster draw is decided here, in
/// `AccountsPresentation`, so both apps say the same thing. The rule that
/// matters most: a deck with fewer than two accounts shows exactly what it
/// showed before. Nothing about accounts appears, nothing is greyed out.
final class AccountsPresentationTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 1_790_000_000)
    private let day: TimeInterval = 86_400

    private func window(_ key: String, _ pct: Double) -> ClaudeUsage.Window {
        ClaudeUsage.Window(key: key, label: key == "session" ? "5-hour" : "Weekly", percent: pct)
    }

    private var personal: AccountUsage {
        AccountUsage(id: "main", label: "Personal", plan: "Max 20×",
                     windows: [window("weekly_all", 72), window("session", 38), window("opus", 5)],
                     desks: ["chief"])
    }
    private var work: AccountUsage {
        AccountUsage(id: "work", label: "Work", plan: "Pro", windows: [window("session", 91)])
    }

    private func usage(_ accounts: [AccountUsage], policy: AccountPolicy? = nil) -> ClaudeUsage {
        ClaudeUsage(available: true, plan: "Max 20×", windows: [window("session", 38)],
                    accounts: accounts, policy: policy)
    }

    // MARK: meters

    func testTwoAccountsGetTwoMetersEachWithItsLabelAndPlan() throws {
        let meters = try XCTUnwrap(AccountsPresentation.meters(usage([personal, work])))
        XCTAssertEqual(meters.map(\.label), ["Personal", "Work"])
        XCTAssertEqual(meters.map(\.plan), ["Max 20×", "Pro"])
        XCTAssertEqual(meters[0].windows.map(\.key), ["session", "weekly_all"],
                       "the 5-hour window leads and two windows fit the card")
        XCTAssertEqual(meters[1].windows.first?.percent, 91)
    }

    func testOneAccountOrNoneKeepsTheSingleMeterTheDeckAlwaysHad() {
        XCTAssertNil(AccountsPresentation.meters(usage([])), "an old deck")
        XCTAssertNil(AccountsPresentation.meters(usage([personal])), "one account is the old meter")
    }

    func testAnAccountThatCannotBeReadSaysSoInsteadOfDrawingZero() throws {
        let broken = AccountUsage(id: "work", label: "Work", plan: "Pro", available: false, stale: true,
                                  reason: "idle_token", windows: [])
        let meters = try XCTUnwrap(AccountsPresentation.meters(usage([personal, broken])))
        XCTAssertTrue(meters[1].windows.isEmpty)
        XCTAssertEqual(meters[1].note, "Unavailable (idle token)")
        XCTAssertTrue(meters[1].isStale)
        XCTAssertNil(meters[0].note)
    }

    // MARK: badge

    private func agent(_ state: AgentState = .idle, account: String? = nil, running: String? = nil) -> Agent {
        Agent(name: "scout", title: "Scout", state: state, account: account, runningAccount: running)
    }

    func testTheBadgeNamesTheAccountADeskRunsOn() {
        let two = [personal, work]
        XCTAssertEqual(AccountsPresentation.badge(for: agent(account: "work"), accounts: two), "Work")
        XCTAssertEqual(AccountsPresentation.badge(for: agent(), accounts: two), "Personal",
                       "no account means the default, which is the first")
        XCTAssertEqual(AccountsPresentation.badge(for: agent(account: "work", running: "main"), accounts: two),
                       "Personal", "the account it is actually running on wins")
        XCTAssertEqual(AccountsPresentation.badge(for: agent(account: "gone"), accounts: two), "gone",
                       "an account this client has not heard of shows its id, not nothing")
    }

    func testTheBadgeUsesThePoliciesDefaultWhenThereIsOne() {
        let policy = AccountPolicy(mode: "failover", defaultAccount: "work")
        XCTAssertEqual(AccountsPresentation.badge(for: agent(), accounts: [personal, work], defaultID: policy.defaultAccount),
                       "Work")
    }

    func testThereIsNoBadgeUnlessThereIsAChoice() {
        XCTAssertNil(AccountsPresentation.badge(for: agent(account: "main"), accounts: []))
        XCTAssertNil(AccountsPresentation.badge(for: agent(account: "main"), accounts: [personal]))
    }

    // MARK: Move to account…

    func testAnIdleDeskCanMoveToTheOtherAccountAndNotToItsOwn() {
        let options = AccountsPresentation.moveOptions(for: agent(account: "main"), accounts: [personal, work])
        XCTAssertEqual(options.map(\.label), ["Personal", "Work"])
        XCTAssertEqual(options.map(\.isEnabled), [false, true])
        XCTAssertEqual(options[0].disabledReason, "Already on Personal")
        XCTAssertNil(options[1].disabledReason)
    }

    func testABusyDeskCannotMoveAndTheMenuSaysWhy() {
        for (state, word) in [(AgentState.working, "working"), (.needsYou, "waiting for you")] {
            let options = AccountsPresentation.moveOptions(for: agent(state, account: "main"), accounts: [personal, work])
            XCTAssertEqual(options.map(\.isEnabled), [false, false], "\(state)")
            XCTAssertTrue(options[1].disabledReason?.lowercased().contains(word) == true,
                          "\(state): \(options[1].disabledReason ?? "nil")")
        }
        XCTAssertNotNil(AccountsPresentation.moveDisabledReason(for: agent(.dead)))
        XCTAssertNotNil(AccountsPresentation.moveDisabledReason(for: agent(.offline)))
    }

    func testIdleDoneAndAsleepDesksAreMovable() {
        for state in [AgentState.idle, .done, .asleep] {
            XCTAssertNil(AccountsPresentation.moveDisabledReason(for: agent(state)), "\(state)")
        }
    }

    func testThereIsNothingToMoveToOnADeckWithOneAccount() {
        XCTAssertTrue(AccountsPresentation.moveOptions(for: agent(), accounts: [personal]).isEmpty)
        XCTAssertTrue(AccountsPresentation.moveOptions(for: agent(), accounts: []).isEmpty)
    }

    // MARK: expiry banner

    func testALoginRunningOutWithinThreeDaysGetsABannerPerAccount() {
        let soon = AccountUsage(id: "main", label: "Personal", refreshExpiresAt: now.addingTimeInterval(2 * day))
        let lapsed = AccountUsage(id: "work", label: "Work", refreshExpiresAt: now.addingTimeInterval(-3_600))
        let fine = AccountUsage(id: "api", label: "Api", refreshExpiresAt: now.addingTimeInterval(20 * day))
        let unknown = AccountUsage(id: "x", label: "X", refreshExpiresAt: nil)
        let warnings = AccountsPresentation.expiryWarnings([fine, soon, unknown, lapsed], now: now)
        XCTAssertEqual(warnings.map(\.accountID), ["work", "main"], "the lapsed one first, then soonest")
        XCTAssertTrue(warnings[0].text.contains("Work"))
        XCTAssertTrue(warnings[0].text.lowercased().contains("expired"), warnings[0].text)
        XCTAssertTrue(warnings[1].text.contains("Personal"))
        XCTAssertTrue(warnings[1].text.contains("2 days"), warnings[1].text)
    }

    func testTheBannerStartsExactlyAtThreeDays() {
        let edge = AccountUsage(id: "a", label: "A", refreshExpiresAt: now.addingTimeInterval(3 * day))
        let past = AccountUsage(id: "b", label: "B", refreshExpiresAt: now.addingTimeInterval(3 * day + 60))
        XCTAssertEqual(AccountsPresentation.expiryWarnings([edge, past], now: now).map(\.accountID), ["a"])
    }

    func testAnHourLeftReadsInHoursNotAsZeroDays() {
        let hour = AccountUsage(id: "a", label: "A", refreshExpiresAt: now.addingTimeInterval(5_400))
        XCTAssertTrue(AccountsPresentation.expiryWarnings([hour], now: now)[0].text.contains("hour"))
    }

    // MARK: auto-switch (read-only)

    func testTheAutoSwitchRowIsReadOnlyAndHiddenOnADeckWithoutAPolicy() throws {
        XCTAssertNil(AccountsPresentation.autoSwitch(nil))
        let on = try XCTUnwrap(AccountsPresentation.autoSwitch(AccountPolicy(mode: "failover", thresholdPct: 90)))
        XCTAssertTrue(on.isOn)
        XCTAssertTrue(on.caption.contains("90%"), on.caption)
        XCTAssertFalse(on.isEditable, "the deck has no route to change the policy")
        let off = try XCTUnwrap(AccountsPresentation.autoSwitch(AccountPolicy(mode: "fixed")))
        XCTAssertFalse(off.isOn)
    }
}
