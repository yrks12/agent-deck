import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The owner signed in the second account and the Mac drew one meter.**
/// `Fixtures/usage-two-accounts-live.json` is the real `GET /v1/usage` from the
/// box on 2026-10-01 (desk names replaced), not a payload written to suit the
/// decoder: `unofficial`, `breakdown`, `other`, scoped windows with `scope`,
/// `resets_at` as ISO text with microseconds, a null `resets_at`, a plan
/// object, `extra_usage`, `failover_order`, `api_account`.
final class TheLiveTwoAccountPayloadTests: XCTestCase {

    private func live() throws -> ClaudeUsage {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/usage-two-accounts-live.json")
        return try DeckCoding.decoder.decode(ClaudeUsage.self, from: Data(contentsOf: url))
    }

    func testTheRealPayloadDecodesToTwoAccountsAndAPolicy() throws {
        let usage = try live()
        XCTAssertEqual(usage.accounts.map(\.id), ["main", "work"])
        XCTAssertEqual(usage.accounts.map(\.label), ["Main", "Work"])
        XCTAssertEqual(usage.accounts.map(\.plan), ["Max 20×", "Max 20×"])
        XCTAssertEqual(usage.accounts[0].windows.map(\.key), ["session", "weekly_all", "weekly_scoped:Fable"])
        XCTAssertEqual(usage.accounts[0].windows.map(\.percent), [48, 83, 0])
        XCTAssertEqual(usage.accounts[1].windows.map(\.percent), [0, 0, 0])
        XCTAssertNil(usage.accounts[1].windows[0].resetsAt, "a null reset time is unknown, not a failure")
        XCTAssertNotNil(usage.accounts[0].windows[0].resetsAt)
        XCTAssertNotNil(usage.accounts[1].refreshExpiresAt)
        XCTAssertEqual(usage.policy?.mode, "failover")
        XCTAssertEqual(usage.policy?.thresholdPct, 90)
        XCTAssertEqual(usage.policy?.defaultAccount, "main")
        XCTAssertEqual(usage.plan, "Max 20×")
    }

    func testTheRealPayloadDrawsTwoMetersEachWithBars() throws {
        let meters = try XCTUnwrap(AccountsPresentation.meters(try live()))
        XCTAssertEqual(meters.map(\.label), ["Main", "Work"])
        XCTAssertEqual(meters.map { $0.windows.count }, [2, 2])
        XCTAssertEqual(meters.map(\.note), [nil, nil], "available accounts with 0% are drawn at 0%, not as unavailable")
    }

    func testAnAccountWhoseWindowsAreNullIsStillDrawnAsASlotNotDropped() throws {
        // The owner's description: work had `windows: null` before its first read.
        let usage = try DeckCoding.decoder.decode(ClaudeUsage.self, from: Data(#"""
        {"available":true,"windows":[],"policy":{"mode":"failover"},
         "accounts":[{"id":"main","label":"Main","available":true,"windows":[{"key":"session","label":"5h","percent":48}]},
                     {"id":"work","label":"Work","available":true,"windows":null}]}
        """#.utf8))
        let meters = try XCTUnwrap(AccountsPresentation.meters(usage))
        XCTAssertEqual(meters.map(\.label), ["Main", "Work"], "two accounts, two meters")
        XCTAssertNotNil(meters[1].note, "and the second says why it has no bars")
    }

    func testAsteadyFirstAccountsMeterIsNotHiddenByAnOldSingleMeterOnTheMac() throws {
        // The Mac's sidebar must draw `meters`, and must be given the new payload:
        // a usage that gained an account is a different value, so it is published.
        let one = ClaudeUsage(available: true, windows: [], accounts: [AccountUsage(id: "main")])
        let two = try live()
        XCTAssertNotEqual(one, two)
    }

    // MARK: the app asks again when he comes back to it

    /// A Mac app stays open for days. The meter used to be asked for on a
    /// minute's timer alone, so a second account signed in elsewhere could sit
    /// unseen; coming to the front now asks at once, bypassing the minute.
    @MainActor
    func testComingToTheFrontAsksForTheMeterAtOnce() async {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        XCTAssertNil(store.usage)

        await store.appBecameActive()
        XCTAssertNotNil(store.usage)
    }

    func testTheWindowAsksWhenTheAppBecomesActive() throws {
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let root = try String(contentsOf: repo.appendingPathComponent("macos/Sources/DeckUI/DeckRootView.swift"), encoding: .utf8)
        XCTAssertTrue(root.contains("didBecomeActiveNotification"))
        XCTAssertTrue(root.contains("appBecameActive"))
    }
}
