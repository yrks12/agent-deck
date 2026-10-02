import XCTest
@testable import DeckKit

/// **`MacBridgeState` is everything the UI draws about the Mac.** The in-use
/// bar reads like the plan ("Atlas is using your Mac — `npm test` · 0:14"),
/// one grant card per desk however many jobs it sent, the transport guard
/// shows its sentence before anything connects, and the recent list is
/// bounded.
final class MacPolicyPresentationStateTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_788_222_702)

    func testTheInUseLine() {
        var s = MacBridgeState()
        XCTAssertNil(s.inUseLine(now: t0))
        XCTAssertFalse(s.isInUse)
        s.started(MacRunningJob(id: "mj_1", desk: "atlas", summary: "npm test", startedAt: t0))
        XCTAssertTrue(s.isInUse)
        XCTAssertEqual(s.inUseLine(now: t0.addingTimeInterval(14)), "Atlas is using your Mac — `npm test` · 0:14")
        s.started(MacRunningJob(id: "mj_2", desk: "nova", summary: "ls", startedAt: t0.addingTimeInterval(5)))
        XCTAssertEqual(s.inUseLine(now: t0.addingTimeInterval(75)), "Atlas and Nova are using your Mac — 2 jobs · 1:15")
        s.finished(jobId: "mj_1")
        s.finished(jobId: "mj_2")
        XCTAssertNil(s.inUseLine(now: t0))
    }

    func testElapsed() {
        XCTAssertEqual(MacBridgeState.elapsed(14), "0:14")
        XCTAssertEqual(MacBridgeState.elapsed(723), "12:03")
        XCTAssertEqual(MacBridgeState.elapsed(3723), "1:02:03")
    }

    func testOneGrantCardPerDesk() {
        var s = MacBridgeState()
        let a = MacGrantRequest(jobId: "mj_1", desk: "atlas", summary: "run sw_vers", folders: [], unconfined: false)
        s.asked(a)
        s.asked(MacGrantRequest(jobId: "mj_2", desk: "atlas", summary: "read x", folders: [], unconfined: false))
        s.asked(MacGrantRequest(jobId: "mj_3", desk: "nova", summary: "ls", folders: [], unconfined: false))
        XCTAssertEqual(s.pendingGrants.map(\.jobId), ["mj_1", "mj_3"])
        s.answered(desk: "atlas")
        XCTAssertEqual(s.pendingGrants.map(\.desk), ["nova"])
    }

    func testTheTransportGuardShowsItsSentenceFirst() {
        XCTAssertEqual(MacBridgeState.initialConnection(mode: .ask, deck: URL(string: "http://1.2.3.4:7789")),
                       .refused(MacTransport.refusal))
        XCTAssertEqual(MacBridgeState.initialConnection(mode: .ask, deck: URL(string: "http://10.99.0.1:7789")), .connecting)
        XCTAssertEqual(MacBridgeState.initialConnection(mode: .off, deck: URL(string: "http://10.99.0.1:7789")), .off)
        XCTAssertEqual(MacBridgeState.initialConnection(mode: .paused, deck: URL(string: "http://10.99.0.1:7789")), .paused)
    }

    func testRecentIsNewestFirstAndBounded() {
        var s = MacBridgeState()
        for i in 0..<(MacBridgeState.recentMax + 5) {
            s.logged(MacActivityRow(ts: Double(i), jobId: "mj_\(i)", desk: "atlas", kind: "run", summary: "", decision: .ran))
        }
        XCTAssertEqual(s.recent.count, MacBridgeState.recentMax)
        XCTAssertEqual(s.recent.first?.jobId, "mj_\(MacBridgeState.recentMax + 4)")
    }
}
