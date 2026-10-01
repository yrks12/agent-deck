import XCTest
@testable import DeckKit

/// **`read` / `write` / `list` keep MB1's caps and never leave half a file.**
/// A binary read as text is `not_text` (not mojibake); a page that ends inside
/// a UTF-8 character is not mistaken for binary; `create` never clobbers; a
/// directory listing is capped at 1 000.
final class MacFileOpsTests: XCTestCase {
    var dir: String!

    override func setUpWithError() throws {
        dir = MacPaths.realpathOrSelf(NSTemporaryDirectory()) + "/mac-files-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(atPath: dir) }

    private func reason<T>(_ r: Result<T, MacOpError>) -> String? {
        if case let .failure(e) = r { return e.reason }
        return nil
    }

    func testReadTextPaged() throws {
        try "hello world".write(toFile: dir + "/a.txt", atomically: true, encoding: .utf8)
        let r = try MacFileOps.read(dir + "/a.txt", offset: 6, length: 3).get()
        XCTAssertEqual(r.text, "wor")
        XCTAssertEqual(r.size, 11)
        XCTAssertEqual(r.returned, 3)
        XCTAssertTrue(r.truncated)
    }

    func testAPageEndingInsideACharacterIsStillText() throws {
        try "aé".write(toFile: dir + "/u.txt", atomically: true, encoding: .utf8)   // 61 C3 A9
        let r = try MacFileOps.read(dir + "/u.txt", offset: 0, length: 2).get()
        XCTAssertEqual(r.text, "a")
        XCTAssertEqual(r.returned, 1)
    }

    func testBinaryAsTextIsNotTextAndBase64Works() throws {
        try Data([0x89, 0x50, 0x00, 0xff]).write(to: URL(fileURLWithPath: dir + "/b.bin"))
        XCTAssertEqual(reason(MacFileOps.read(dir + "/b.bin")), "not_text")
        XCTAssertEqual(try MacFileOps.read(dir + "/b.bin", encoding: "base64").get().base64, "iVAA/w==")
    }

    func testReadRefusals() throws {
        XCTAssertEqual(reason(MacFileOps.read(dir + "/missing")), "no_such_path")
        XCTAssertEqual(reason(MacFileOps.read(dir)), "not_a_file")
        XCTAssertEqual(reason(MacFileOps.read(dir + "/x", length: 64_001)), "bad_input")
    }

    func testWriteIsAtomicAndKeepsPermissions() throws {
        let p = dir + "/w.sh"
        try "old".write(toFile: p, atomically: true, encoding: .utf8)
        chmod(p, 0o755)
        XCTAssertEqual(try MacFileOps.write(p, content: "new").get().bytes, 3)
        XCTAssertEqual(try String(contentsOfFile: p, encoding: .utf8), "new")
        XCTAssertEqual((try FileManager.default.attributesOfItem(atPath: p)[.posixPermissions] as? Int), 0o755)
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir).filter { $0.contains("agentdeck") }
        XCTAssertEqual(leftovers, [])
    }

    func testAppendCreateAndMakeDirs() throws {
        let p = dir + "/a/b/log.txt"
        XCTAssertEqual(reason(MacFileOps.write(p, content: "1")), "no_such_path")
        _ = try MacFileOps.write(p, content: "1", makeDirs: true).get()
        _ = try MacFileOps.write(p, content: "2", mode: "append").get()
        XCTAssertEqual(try String(contentsOfFile: p, encoding: .utf8), "12")
        XCTAssertEqual(reason(MacFileOps.write(p, content: "x", mode: "create")), "bad_input")
        XCTAssertEqual(try String(contentsOfFile: p, encoding: .utf8), "12", "create must never clobber")
    }

    func testWriteCaps() {
        let big = String(repeating: "x", count: (5 << 20) + 1)
        XCTAssertEqual(reason(MacFileOps.write(dir + "/big", content: big)), "too_large")
        XCTAssertEqual(reason(MacFileOps.write(dir + "/b64", content: "***", encoding: "base64")), "bad_input")
    }

    func testListOneLevelHiddenAndCap() throws {
        try FileManager.default.createDirectory(atPath: dir + "/sub", withIntermediateDirectories: true)
        try "x".write(toFile: dir + "/f.txt", atomically: true, encoding: .utf8)
        try "x".write(toFile: dir + "/.hidden", atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(atPath: dir + "/ln", withDestinationPath: dir + "/f.txt")
        let r = try MacFileOps.list(dir).get()
        XCTAssertEqual(r.entries.map(\.name), ["f.txt", "ln", "sub"])
        XCTAssertEqual(r.entries.map(\.kind), [.file, .link, .dir])
        XCTAssertEqual(try MacFileOps.list(dir, hidden: true).get().entries.count, 4)
        XCTAssertEqual(reason(MacFileOps.list(dir + "/f.txt")), "not_a_directory")
    }

    func testListIsCappedAtAThousand() throws {
        let d = dir + "/many"
        try FileManager.default.createDirectory(atPath: d, withIntermediateDirectories: true)
        for i in 0..<1_001 { FileManager.default.createFile(atPath: d + "/\(i)", contents: nil) }
        let r = try MacFileOps.list(d).get()
        XCTAssertEqual(r.entries.count, 1_000)
        XCTAssertTrue(r.truncated)
    }

    func testEPERMUnderATccFolderIsTccDenied() {
        XCTAssertEqual(MacFileOps.errnoError(EPERM, "/Users/y/Documents/a", home: "/Users/y").reason, "tcc_denied")
        XCTAssertEqual(MacFileOps.errnoError(EACCES, "/etc/x", home: "/Users/y").reason, "bad_path")
    }
}
