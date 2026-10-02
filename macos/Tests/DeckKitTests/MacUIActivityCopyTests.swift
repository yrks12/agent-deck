import XCTest
import AppKit
@testable import DeckKit
@testable import DeckUI

/// **The activity list reads as sentences, not as log fields.** Decision,
/// exit, duration and whether the command was fenced in — the plan's row.
final class MacUIActivityCopyTests: XCTestCase {
    private func row(_ decision: MacActivityRow.Decision, kind: String = "run", reason: String? = nil,
                     exit: Int32? = nil, ms: Int? = nil, sandboxed: Bool? = nil) -> MacActivityRow {
        MacActivityRow(ts: 1_788_222_702, jobId: "j", desk: "atlas", kind: kind, summary: "npm test",
                       decision: decision, reason: reason, exit: exit, durationMs: ms, sandboxed: sandboxed)
    }

    func testDecisionsInPlainWords() {
        XCTAssertEqual(MacActivityCopy.decision(row(.ran)), "Ran")
        XCTAssertEqual(MacActivityCopy.decision(row(.asked)), "Asked you")
        XCTAssertEqual(MacActivityCopy.decision(row(.granted)), "Allowed")
        XCTAssertEqual(MacActivityCopy.decision(row(.denied)), "Denied")
        XCTAssertEqual(MacActivityCopy.decision(row(.refused, reason: "out_of_scope")), "Refused — out of scope")
        XCTAssertEqual(MacActivityCopy.decision(row(.refused)), "Refused")
    }

    func testTheDetailLine() {
        XCTAssertEqual(MacActivityCopy.detail(row(.ran, exit: 0, ms: 2100, sandboxed: true)),
                       "exit 0 · 2.1 s · fenced in")
        XCTAssertEqual(MacActivityCopy.detail(row(.ran, exit: 1, ms: 450, sandboxed: false)),
                       "exit 1 · 450 ms · not fenced in")
        XCTAssertEqual(MacActivityCopy.detail(row(.ran, exit: 0, ms: 125_000)), "exit 0 · 2:05")
        XCTAssertEqual(MacActivityCopy.detail(row(.asked)), "")
    }

    func testTimeIsTheClockTodayAndTheDayBefore() {
        var utc = Calendar(identifier: .gregorian); utc.timeZone = TimeZone(identifier: "UTC")!
        let ts = utc.date(from: DateComponents(year: 2026, month: 9, day: 30, hour: 14, minute: 2, second: 11))!
        let loc = Locale(identifier: "en_GB")
        XCTAssertEqual(MacActivityCopy.time(ts.timeIntervalSince1970, now: ts.addingTimeInterval(600), calendar: utc, locale: loc), "14:02:11")
        XCTAssertEqual(MacActivityCopy.time(ts.timeIntervalSince1970, now: ts.addingTimeInterval(86_400 * 2), calendar: utc, locale: loc), "30 Sep at 14:02")
    }

    func testEveryKindHasASymbolThatExists() {
        for kind in MacJobKind.allCases {
            let symbol = MacActivityCopy.symbol(kind: kind.rawValue)
            XCTAssertNotNil(NSImage(systemSymbolName: symbol, accessibilityDescription: nil), "\(kind): '\(symbol)'")
        }
        XCTAssertNotNil(NSImage(systemSymbolName: MacActivityCopy.symbol(kind: "mystery"), accessibilityDescription: nil))
    }

    func testAnEmptyListSaysWhatWillAppear() {
        XCTAssertTrue(MacActivityCopy.empty.contains("desk"))
    }
}
