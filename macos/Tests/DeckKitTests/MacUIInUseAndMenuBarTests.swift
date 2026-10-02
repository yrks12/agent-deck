import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The in-use bar and the menu-bar extra**, and the two rules that keep them
/// quiet: idle draws nothing, and every symbol they name exists.
@MainActor
final class MacUIInUseAndMenuBarTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_788_222_702)
    private func job(_ id: String, _ desk: String, _ cmd: String, at: Double = 0) -> MacRunningJob {
        MacRunningJob(id: id, desk: desk, summary: cmd, startedAt: t0.addingTimeInterval(at))
    }

    func testTheBarIsInThreePartsSoTheCommandCanBeSetInMonospace() throws {
        var s = MacBridgeState(mode: .ask, connection: .online)
        XCTAssertNil(MacInUseParts(s, now: t0))
        s.started(job("1", "atlas", "npm test"))
        let p = try XCTUnwrap(MacInUseParts(s, now: t0.addingTimeInterval(14)))
        XCTAssertEqual(p.lead, "Atlas is using your Mac — ")
        XCTAssertEqual(p.command, "npm test")
        XCTAssertEqual(p.trail, " · 0:14")
        XCTAssertEqual(p.stopLabel, "Stop")
        XCTAssertEqual(p.stopDesks, ["atlas"])
    }

    func testSeveralJobsReadAsOneSentenceAndStopEveryDesk() throws {
        var s = MacBridgeState()
        s.started(job("1", "atlas", "npm test"))
        s.started(job("2", "nova", "ls", at: 5))
        let p = try XCTUnwrap(MacInUseParts(s, now: t0.addingTimeInterval(75)))
        XCTAssertNil(p.command)
        XCTAssertEqual(p.lead + p.trail, "Atlas and Nova are using your Mac — 2 jobs · 1:15")
        XCTAssertEqual(p.stopLabel, "Stop all")
        XCTAssertEqual(Set(p.stopDesks), ["atlas", "nova"])
    }

    func testAnIdleBarTakesNoRoomAtAll() {
        let host = NSHostingView(rootView: MacInUseBar(state: MacBridgeState(), onStop: { _ in }))
        host.frame = NSRect(x: 0, y: 0, width: 600, height: 400)
        XCTAssertEqual(host.fittingSize.height, 0, accuracy: 0.5,
                       "the bar sits above the conversation: idle, it must not steal a pixel")
    }

    func testTheMenuBarIconFillsWhileAJobRunsAndEveryStateHasASymbol() {
        var s = MacBridgeState(mode: .ask, connection: .online)
        let idle = MacMenuBarModel(state: s).symbol
        s.started(job("1", "atlas", "ls"))
        let busy = MacMenuBarModel(state: s).symbol
        XCTAssertNotEqual(idle, busy)
        XCTAssertTrue(busy.hasSuffix(".fill") || busy.contains("fill"), "it fills while a job runs: \(busy)")
        let states: [MacBridgeState] = [
            MacBridgeState(mode: .off, connection: .off), MacBridgeState(mode: .paused, connection: .paused),
            MacBridgeState(mode: .ask, connection: .connecting), MacBridgeState(mode: .full, connection: .online),
            MacBridgeState(mode: .ask, connection: .refused("x")), s,
        ]
        for st in states {
            let symbol = MacMenuBarModel(state: st).symbol
            XCTAssertNotNil(NSImage(systemSymbolName: symbol, accessibilityDescription: nil),
                            "'\(symbol)' is not an SF Symbol on this macOS")
        }
    }

    func testTheMenuListsEachJobWithItsOwnStopAndThePlansThreeActions() {
        var s = MacBridgeState(mode: .ask, connection: .online)
        s.started(job("1", "atlas", "npm test"))
        s.started(job("2", "nova", "ls", at: 3))
        let m = MacMenuBarModel(state: s)
        XCTAssertEqual(m.jobs.map(\.id), ["1", "2"])
        XCTAssertEqual(m.jobs.first?.title, "Atlas — npm test")
        XCTAssertEqual(m.pauseTitle, "Pause Mac access")
        XCTAssertEqual(m.pauseAction, .pause)
        XCTAssertEqual(m.openActivityTitle, "Open Activity")
        XCTAssertEqual(m.quitTitle, "Quit Shaliach")
    }

    func testPausedOffersResumeAndOffOffersNothingToPause() {
        let paused = MacMenuBarModel(state: MacBridgeState(mode: .paused, connection: .paused))
        XCTAssertEqual(paused.pauseTitle, "Resume Mac access")
        XCTAssertEqual(paused.pauseAction, .resume)
        let off = MacMenuBarModel(state: MacBridgeState(mode: .off, connection: .off))
        XCTAssertEqual(off.pauseAction, .none, "nothing to pause when access is already off")
    }

    func testTheStatusLineSaysWhereTheBridgeStands() {
        XCTAssertEqual(MacMenuBarModel(state: MacBridgeState(mode: .ask, connection: .online)).status,
                       "Ask me · connected")
        XCTAssertEqual(MacMenuBarModel(state: MacBridgeState(mode: .full, connection: .online)).status,
                       "Full access · connected")
        XCTAssertEqual(MacMenuBarModel(state: MacBridgeState(mode: .paused, connection: .paused)).status,
                       "Paused — desks can't reach this Mac")
        XCTAssertEqual(MacMenuBarModel(state: MacBridgeState(mode: .off, connection: .off)).status,
                       "Off")
        XCTAssertEqual(MacMenuBarModel(state: MacBridgeState(mode: .ask, connection: .refused(MacTransport.refusal))).status,
                       "Not connected")
    }
}
