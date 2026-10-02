import XCTest
@testable import DeckKit

/// **A-4: the Ask-mode profile is enforced by the kernel, not by us.** Real
/// `/usr/bin/sandbox-exec`, a temp HOME: a read under HOME outside the folders
/// is EPERM, a write outside the folders is EPERM, a write inside works, a
/// `.env` inside a folder is still denied unless excepted, and `git --version`
/// still runs.
final class MacSeatbeltTests: XCTestCase {
    var home: String!
    var paths: MacPaths!

    override func setUpWithError() throws {
        try XCTSkipUnless(MacSeatbelt.isAvailable, "sandbox-exec unavailable — the policy fallback applies")
        home = NSTemporaryDirectory() + "mac-sbx-\(UUID().uuidString)"
        for d in ["/w", "/private-stuff"] {
            try FileManager.default.createDirectory(atPath: home + d, withIntermediateDirectories: true)
        }
        try "secret".write(toFile: home + "/private-stuff/s.txt", atomically: true, encoding: .utf8)
        try "K=1".write(toFile: home + "/w/.env", atomically: true, encoding: .utf8)
        try "K=2".write(toFile: home + "/w/.env.local", atomically: true, encoding: .utf8)
        paths = MacPaths(home: home)
    }

    override func tearDownWithError() throws { if let home { try? FileManager.default.removeItem(atPath: home) } }

    private func profile(exceptions: [String] = []) -> MacSeatbeltProfile {
        MacSeatbelt.profile(paths: paths, folders: [paths.home + "/w"], neverTouch: MacPolicyConfig.defaultNeverTouch,
                            exceptions: exceptions, tempDirs: ["/private/tmp"])
    }

    /// Runs `/bin/sh -c script` confined; returns (status, stderr).
    private func sh(_ script: String, _ profile: MacSeatbeltProfile) throws -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: MacSeatbelt.executable)
        p.arguments = ["-p", profile.text, "/bin/sh", "-c", script]
        p.currentDirectoryURL = URL(fileURLWithPath: paths.home + "/w")
        let err = Pipe()
        p.standardError = err
        p.standardOutput = FileHandle.nullDevice
        try p.run()
        let data = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    func testTheProfileCompiles() throws {
        XCTAssertEqual(try sh("true", profile()).0, 0)
    }

    func testReadUnderHomeOutsideFoldersIsEPERM() throws {
        let (status, err) = try sh("cat \(paths.home)/private-stuff/s.txt", profile())
        XCTAssertNotEqual(status, 0)
        XCTAssertTrue(err.contains("Operation not permitted"), err)
    }

    func testWriteOutsideFoldersIsEPERM() throws {
        let (status, err) = try sh("echo x > \(paths.home)/private-stuff/new.txt", profile())
        XCTAssertNotEqual(status, 0)
        XCTAssertTrue(err.contains("Operation not permitted"), err)
        XCTAssertFalse(FileManager.default.fileExists(atPath: home + "/private-stuff/new.txt"))
    }

    func testWriteInsideAFolderAndTmpWork() throws {
        let tmp = "/private/tmp/mac-sbx-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: tmp) }
        XCTAssertEqual(try sh("echo x > \(paths.home)/w/ok.txt && echo y > \(tmp)", profile()).0, 0)
        XCTAssertTrue(FileManager.default.fileExists(atPath: home + "/w/ok.txt"))
    }

    func testNeverTouchInsideAFolderIsDeniedBothWays() throws {
        XCTAssertNotEqual(try sh("cat \(paths.home)/w/.env", profile()).0, 0)
        XCTAssertNotEqual(try sh("echo x > \(paths.home)/w/.env.local", profile()).0, 0)
    }

    func testAnExceptionReopensOnlyInsideAFolder() throws {
        XCTAssertEqual(try sh("cat \(paths.home)/w/.env", profile(exceptions: ["~/w/.env"])).0, 0)
        XCTAssertNotEqual(try sh("cat \(paths.home)/w/.env.local", profile(exceptions: ["~/w/.env"])).0, 0)
    }

    func testGitStillRuns() throws {
        XCTAssertEqual(try sh("/usr/bin/git --version", profile()).0, 0)
    }

    func testQuotesAndBackslashesAreEscaped() {
        XCTAssertEqual(MacSeatbelt.quote(#"/a"b\c"#), #""/a\"b\\c""#)
    }
}
