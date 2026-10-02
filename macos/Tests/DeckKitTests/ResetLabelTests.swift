import XCTest
@testable import DeckKit

final class ResetLabelTests: XCTestCase {
    private var cal: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Europe/London")!
        return c
    }
    private func date(_ s: String) -> Date { ISO8601DateFormatter().date(from: s)! }

    func testWithin24HoursShowsClockTime() {
        let now = date("2026-10-02T00:30:00Z")   // 01:30 BST
        XCTAssertEqual(ResetLabel.text(now: now, resetsAt: date("2026-10-02T02:40:00Z"), calendar: cal), "resets 03:40")
    }

    func testBeyond24HoursShowsWeekdayAndTime() {
        let now = date("2026-10-02T00:30:00Z")   // Fri
        XCTAssertEqual(ResetLabel.text(now: now, resetsAt: date("2026-10-05T13:00:00Z"), calendar: cal), "resets Mon 14:00")
    }

    func testPastOrMissingShowsNothing() {
        let now = date("2026-10-02T00:30:00Z")
        XCTAssertNil(ResetLabel.text(now: now, resetsAt: date("2026-10-01T00:00:00Z"), calendar: cal))
        XCTAssertNil(ResetLabel.text(now: now, resetsAt: nil, calendar: cal))
    }

    func testLiveTwoAccountPayloadWindowsCarryResetTimes() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/usage-two-accounts-live.json")
        let usage = try DeckCoding.decoder.decode(ClaudeUsage.self, from: Data(contentsOf: url))
        XCTAssertNotNil(usage.accounts[0].windows[0].resetsAt)
        XCTAssertNotNil(AccountsPresentation.meters(usage)?[0].windows[0].resetsAt)
    }
}
