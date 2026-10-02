import XCTest
@testable import DeckKit

/// **The Mac keeps its own record of what desks did on it.** Newest first,
/// summary capped at 500, last 4 KiB of output per job, rotation at the size
/// cap without losing the previous generation, nothing older than 30 days,
/// and a forged job id cannot write outside the output directory.
final class MacActivityLogTests: XCTestCase {
    var dir: String!
    let now: Double = 1_788_222_702

    override func setUp() { dir = NSTemporaryDirectory() + "mac-log-\(UUID().uuidString)" }
    override func tearDown() { try? FileManager.default.removeItem(atPath: dir) }

    private func row(_ id: String, ts: Double? = nil, summary: String = "run sw_vers") -> MacActivityRow {
        MacActivityRow(ts: ts ?? now, jobId: id, desk: "atlas", kind: "run", summary: summary, decision: .ran,
                       state: "done", exit: 0, durationMs: 812, outBytes: 20, sandboxed: true)
    }

    func testRowsComeBackNewestFirstWithThePlansKeys() throws {
        let log = MacActivityLog(directory: dir)
        log.append(row("mj_1"))
        log.append(row("mj_2"))
        XCTAssertEqual(log.recent(now: now).map(\.jobId), ["mj_2", "mj_1"])
        let line = try String(contentsOf: log.fileURL, encoding: .utf8).split(separator: "\n")[0]
        let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
        for key in ["ts", "job_id", "desk", "kind", "summary", "decision", "state", "exit", "duration_ms", "out_bytes", "sandboxed"] {
            XCTAssertNotNil(obj[key], key)
        }
    }

    func testSummaryIsCappedAt500() {
        XCTAssertEqual(row("mj_1", summary: String(repeating: "x", count: 900)).summary.count, 500)
    }

    func testRotationKeepsThePreviousGeneration() {
        let log = MacActivityLog(directory: dir, maxBytes: 600)
        for i in 0..<10 { log.append(row("mj_\(i)")) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: log.rotatedURL.path))
        let size = (try? FileManager.default.attributesOfItem(atPath: log.fileURL.path)[.size] as? Int) ?? 0
        XCTAssertLessThanOrEqual(size, 600)
        XCTAssertEqual(log.recent(now: now).first?.jobId, "mj_9")
    }

    func testRowsOlderThanThirtyDaysAreNotShown() {
        let log = MacActivityLog(directory: dir)
        log.append(row("mj_old", ts: now - 31 * 86_400))
        log.append(row("mj_new"))
        XCTAssertEqual(log.recent(now: now).map(\.jobId), ["mj_new"])
    }

    func testOutputKeepsTheLast4KiB() {
        let log = MacActivityLog(directory: dir)
        log.recordOutput(jobId: "mj_1", Data(repeating: 0x61, count: 3000))
        log.recordOutput(jobId: "mj_1", Data(repeating: 0x62, count: 3000))
        let out = log.output(jobId: "mj_1")
        XCTAssertEqual(out.count, 4096)
        XCTAssertEqual(out.last, 0x62)
        XCTAssertEqual(out.first, 0x61)
    }

    func testAForgedJobIdCannotEscape() {
        let log = MacActivityLog(directory: dir)
        log.recordOutput(jobId: "../../evil", Data("x".utf8))
        XCTAssertNil(log.outputURL("../../evil"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir + "/evil.log"))
    }

    func testPruneDropsOldOutput() throws {
        let log = MacActivityLog(directory: dir)
        log.recordOutput(jobId: "mj_1", Data("x".utf8))
        log.prune(now: Date().timeIntervalSince1970 + 31 * 86_400)
        XCTAssertEqual(log.output(jobId: "mj_1"), Data())
    }
}
